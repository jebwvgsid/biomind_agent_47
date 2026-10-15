"""Terminal presentation and bounded, private diagnostics for the package child."""
from __future__ import annotations
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time


def redact(value):
    text = str(value)
    text = re.sub(r'(https?://)[^\s/@]+(?::[^\s/@]*)?@', r'\1[redacted]@', text)
    text = re.sub(r'(?i)(?:bearer|basic)\s+\S+', '[redacted-auth]', text)
    text = re.sub(r"""(?i)((?:token|password|secret|api[_-]?key|authorization)["']?\s*[=:]\s*["']?)[^\s&,;"']+""", r'\1[redacted]', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)


def progress(event):
    kind = event.get('type')
    if kind == 'target':
        c = event['context']
        return (f"目标：{c['platform']} / {c['hostname']} / {c['user']} (uid {c['uid']})\n"
                f"核心：BioMind {c.get('coreVersion', '未核验')} / {c['releaseId']} · Pi {c['piVersion']}\n"
                f"runtime：{c['runtime']}\n范围：{event['scope']}\n"
                f"agentDir：{c['agentDir']}\n项目：{c['cwd']}\n配置：{c['settingsPath']}")
    if kind == 'stage':
        return {'settings_flush': '包操作已返回，正在保存并核对配置…', 'reconcile': '正在核对安装路径与配置…'}.get(event.get('stage'), '')
    action = {'install': '安装', 'remove': '移除', 'update': '更新', 'clone': '克隆', 'pull': '更新仓库'}.get(event.get('action'), '处理')
    return {'start': f"正在{action} {event.get('source', '')}…",
            'complete': f"{action}文件阶段完成（配置尚待核对）",
            'error': f"{action}文件阶段失败；请查看诊断日志"}.get(kind, '')


def emit(result, machine=False):
    if machine:
        def safe(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, dict):
                return {k: safe(v) for k, v in value.items()}
            if isinstance(value, list):
                return [safe(v) for v in value]
            return value
        print(json.dumps(safe(result), ensure_ascii=False))
        return
    if result.get('status') != 'ok':
        print(f"操作未完成：{result.get('code')} ({result.get('stage', 'validate')})")
        print(result.get('guidance', '保留现场；结果未确认时先查询并检查诊断，不要盲目重试。'))
    else:
        print('查询完成' if result.get('action') == 'list' else '操作完成')
    for row in result.get('packages', []):
        enabled = {'enabled': '已启用', 'disabled': '已禁用', 'overridden': '被项目配置覆盖',
                   'missing': '文件缺失', 'unknown': '启用未核验', 'empty': '无资源', 'delta': '项目过滤增量'}.get(row.get('enablement'), '启用未核验')
        version = row.get('installedVersion', '版本未核验')
        if row.get('installedGitCommit'):
            version += ' / commit ' + row['installedGitCommit']
        print(redact(f"  {row['source']} @ {version} [{row['scope']}]"))
        print(f"    {'已安装' if row.get('installed') else '未安装'} · {enabled} · 会话加载未核验 · 功能未核验")
        print(redact(f"    配置：{row.get('settingsPath')}\n    路径：{row.get('installedPath', '缺失')}"))
        if row.get('resources') is not None:
            print('    有效资源：' + ' / '.join(f"{k} {v['enabled']}/{v['total']}" for k, v in row['resources'].items()))
    if 'packageFiles' in result:
        print(f"文件阶段：{result['packageFiles']}；配置阶段：{result['configuration']}")
    for warning in result.get('warnings', []):
        print(redact(f"提示：{warning}"))
    if result.get('activation'):
        print('生效方式：' + result['activation']['instruction'])
    if result.get('logFile'):
        print('诊断日志：' + result['logFile'])


def run_child(command, payload, environment, result_fd, *, machine=False, verbose=False, timeout=900):
    """Drain output without unbounded memory; cancel only our own process group."""
    read_fd, write_fd = os.pipe()
    log_fd, log_path = tempfile.mkstemp(prefix='biomind-plugin-', suffix='.log')
    environment = {**environment, 'BIOMIND_PLUGIN_PROGRESS_FD': str(write_fd)}
    started = time.monotonic()
    child = None
    log_bytes = 0
    last_event = None
    old_term = signal.getsignal(signal.SIGTERM)
    def cancel(_signum, _frame):
        raise KeyboardInterrupt
    def log(text):
        nonlocal log_bytes
        data = (redact(text) + '\n').encode()
        if log_bytes + len(data) <= 1024 * 1024:
            os.write(log_fd, data)
            log_bytes += len(data)
    signal.signal(signal.SIGTERM, cancel)
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 env=environment, pass_fds=(result_fd, write_fd), start_new_session=True)
        os.close(write_fd); write_fd = -1
        child.stdin.write(json.dumps(payload).encode()); child.stdin.close()
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ, 'event')
            selector.register(child.stdout, selectors.EVENT_READ, 'log')
            pending = {'event': b'', 'log': b''}
            dropping = {'event': False, 'log': False}
            heartbeat = started
            while selector.get_map():
                now = time.monotonic()
                if now - started >= timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if now - heartbeat >= 5:
                    if not machine:
                        print(f"操作进行中，已耗时 {int(now - started)} 秒…", flush=True)
                    heartbeat = now
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    channel = key.data
                    for part in chunk.splitlines(keepends=True):
                        end = part.endswith(b'\n')
                        if not dropping[channel]:
                            pending[channel] += part
                            if len(pending[channel]) > 16384:
                                log('[overlong diagnostic line omitted]')
                                pending[channel] = b''; dropping[channel] = True
                        if end:
                            raw = pending[channel]; pending[channel] = b''
                            if dropping[channel]:
                                dropping[channel] = False
                                continue
                            text = raw.decode('utf-8', errors='replace').strip()
                            if channel == 'event':
                                try:
                                    event = json.loads(text); last_event = event
                                    text = progress(event)
                                    if text and not machine:
                                        print(redact(text), flush=True)
                                except (ValueError, KeyError):
                                    text = '[invalid progress event]'
                            elif verbose and not machine:
                                print(redact(text), file=sys.stderr, flush=True)
                            log(text)
            for channel, raw in pending.items():
                if raw:
                    log(raw.decode('utf-8', errors='replace'))
        child.wait(timeout=max(1, timeout - (time.monotonic() - started)))
        log(f'exitCode={child.returncode} elapsedSeconds={time.monotonic() - started:.2f}')
        return child.returncode, log_path
    except (KeyboardInterrupt, subprocess.TimeoutExpired, OSError) as error:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        log(f'{type(error).__name__}; own process group terminated; lastEvent={json.dumps(last_event)}')
        error.log_path = log_path
        raise
    finally:
        signal.signal(signal.SIGTERM, old_term)
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        os.close(log_fd)
        if child and child.stdout:
            child.stdout.close()
