"""Offline IPC adapter for hosts rejecting AF_UNIX socketpair.send with EPERM.

Test-only selector wakeup transport. Uses event-driven os.pipe, no polling,
timer, extra key, business callback or production asyncio replacement.
"""
import asyncio
import os


class PipeWakeupLoop(asyncio.SelectorEventLoop):
    def _make_self_pipe(self):
        self._wakeup_read, self._wakeup_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        from types import SimpleNamespace
        self._csock = SimpleNamespace(fileno=lambda: self._wakeup_write)
        self._internal_fds += 1
        self._add_reader(self._wakeup_read, self._read_from_self)
    def _read_from_self(self):
        try:
            while data := os.read(self._wakeup_read, 4096):
                self._process_self_data(data)
        except BlockingIOError:
            pass
    def _write_to_self(self):
        try:
            os.write(self._wakeup_write, b'\0')
        except (BlockingIOError, OSError):
            pass
    def _close_self_pipe(self):
        self._remove_reader(self._wakeup_read)
        os.close(self._wakeup_read)
        os.close(self._wakeup_write)
        self._csock = None
        self._internal_fds -= 1


def install_pipe_wakeup():
    class Policy(asyncio.DefaultEventLoopPolicy):
        _loop_factory = PipeWakeupLoop
    asyncio.set_event_loop_policy(Policy())


def canonical_business_wire(value, root):
    """Keep full payloads and ID equality relationships, remove named noise only."""
    import json
    import re
    identities = {}
    def identity(kind, raw):
        group = identities.setdefault(kind, {})
        if raw not in group: group[raw] = f'<{kind}:{len(group)+1}>'
        return group[raw]
    def walk(value, key=''):
        if key in {'started_at', 'finished_at', 'elapsed_seconds', 'duration_seconds',
                   'output_idle_seconds', 'last_output_monotonic', 'heartbeat_at', 'process_started_at',
                   'process_start_ticks'}:
            return '<clock>' if value is not None else None
        if key in {'pid', 'launcher_pid', 'pgid'}:
            return identity('pid', str(value))
        if key == 'next_cursor' and isinstance(value, str) and value:
            # Current owner uses a per-run random MAC. Preserve its actual
            # decoded run/byte offsets and equality relationships, not the nonce.
            import base64
            encoded, signature = value.split('.')
            assert re.fullmatch(r'[0-9a-f]{24}', signature)
            body = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            return {'cursor_body': walk(body), 'authenticator': identity('cursor_mac', signature)}
        if key == 'arguments_hash':
            assert re.fullmatch(r'[0-9a-f]{64}', value)
            return identity('arguments_hash', value)
        if isinstance(value, dict): return {k:walk(v,k) for k,v in value.items()}
        if isinstance(value, list): return [walk(v) for v in value]
        if not isinstance(value, str): return value
        value = value.replace(str(root), '<ROOT>')
        for start in (0, value.find('{')):
            if start < 0: continue
            try: parsed = json.loads(value[start:])
            except ValueError: continue
            if isinstance(parsed, (dict,list)):
                return {'prefix':value[:start], 'json':walk(parsed)}
        value = re.sub(r'/proc/(\d+)/', lambda m:'/proc/'+identity('pid',m[1])+'/', value)
        value = re.sub(r'(skill-(?:material|snapshot)-)([0-9a-f]{20})',
                       lambda m:identity(m[1],m[2]), value)
        value = re.sub(r'object:sha256:[0-9a-f]{64}', lambda m:identity('object',m[0]), value)
        return re.sub(r'(?<=file_edits/)[0-9a-f]{32}',lambda m:identity('edit',m[0]),value)
    return walk(value)
