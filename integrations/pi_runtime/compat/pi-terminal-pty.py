#!/usr/bin/env python3
"""Offline real-bundle PTY acceptance. Injected EIO is not incident root-cause proof."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    runtime = Path(__file__).resolve().parents[1]
    package = runtime / 'node_modules/@earendil-works/pi-coding-agent'
    manifest = json.loads((package / 'package.json').read_text())
    node = runtime / '.runtime/node/bin/node'
    root = Path(tempfile.mkdtemp(prefix='biomind-terminal-pty-'))
    project = root / "中文 空格 ' 项目"
    project.mkdir()
    checks = []
    for scenario in ('eio', 'quit', 'ctrl-c'):
        agent = root / scenario
        agent.mkdir()
        trace = evidence / f'{scenario}-lifecycle.jsonl'
        (agent / 'settings.json').write_text(json.dumps({'quietStartup': True, 'enableAnalytics': False,
            'enableInstallTelemetry': False, 'cacheWarming': 'off', 'fullscreenExitOutput': 'resume-hint'}))
        extension = root / f'{scenario}-fixture.mjs'
        extension.write_text("import fs from 'node:fs';\nexport default function(pi) {\n"
            + 'const trace = value => fs.appendFileSync(' + json.dumps(str(trace)) + ', JSON.stringify(value)+"\\n");\n'
            + 'pi.on("session_start", (_event, ctx) => { setTimeout(() => { trace({event:"ready", raw:process.stdin.isRaw}); '
            + ('const e=Object.assign(new Error("PTY injected read EIO"),{code:"EIO",syscall:"read"}); process.stdin.emit("error",e); process.stdin.emit("error",e);'
               if scenario == 'eio' else '')
            + '}, 300); });\npi.on("session_shutdown", () => trace({event:"shutdown"}));\n}\n')
        env = {'PATH': str(node.parent) + ':/usr/bin:/bin', 'HOME': str(agent), 'TERM': 'xterm-256color',
            'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PI_CODING_AGENT_DIR': str(agent), 'PI_OFFLINE': '1',
            'PI_SKIP_VERSION_CHECK': '1', 'BIOMIND_PI_UI_COMMANDS': '1', 'BIOMIND_DISABLE_DOTENV': '1'}
        # Keep an actual persisted session for recovery-hint checks; no model runs.
        seed = root / f'{scenario}-seed.mjs'
        seed.write_text('import { SessionManager } from ' + json.dumps((package / 'dist/index.js').as_uri()) + ';\n'
            + 'const manager=SessionManager.create(' + json.dumps(str(project)) + ');\n'
            + 'manager.appendMessage({role:"user",content:"Synthetic saved context",timestamp:Date.now()});\n'
            + 'manager.appendMessage({role:"assistant",content:[{type:"text",text:"Synthetic saved response"}],timestamp:Date.now(),'
              'api:"openai-completions",provider:"fixture",model:"fixture",stopReason:"stop",'
              'usage:{input:1,output:1,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}}});\n'
            + 'console.log(manager.getSessionFile());\n')
        seeded = subprocess.run([str(node), str(seed)], env=env, cwd=project, capture_output=True, text=True, timeout=30, check=True)
        session = seeded.stdout.strip()
        command = [str(node), str(package / manifest['bin']['pi']), '--offline', '--no-extensions', '--extension', str(extension),
            '--no-skills', '--no-prompt-templates', '--no-themes', '--no-builtin-tools', '--tui-mode', 'fullscreen', '--session', session]
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 140, 0, 0))
        before = termios.tcgetattr(slave)

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(command, env=env, cwd=project, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, preexec_fn=controlling_terminal)
        output = bytearray()
        started = time.monotonic()
        sent = False
        first_ctrl_c_alive = None
        try:
            while process.poll() is None and time.monotonic() - started < 30:
                if select.select([master], [], [], .05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
                if not sent and scenario != 'eio' and trace.exists() and 'ready' in trace.read_text():
                    sent = True
                    if scenario == 'quit':
                        os.write(master, b'/quit\r')
                    else:
                        os.write(master, b'\x03')
                        time.sleep(.1)
                        first_ctrl_c_alive = process.poll() is None
                        os.write(master, b'\x03')
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            while select.select([master], [], [], .05)[0]:
                try:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    output.extend(data)
                except OSError:
                    break
            after = termios.tcgetattr(slave)
        finally:
            os.close(master)
            os.close(slave)
        text = output.decode(errors='replace')
        (evidence / f'{scenario}-terminal.txt').write_text(text)
        lifecycle = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        crashes = json.loads((agent / 'crashes.json').read_text()) if (agent / 'crashes.json').exists() else []
        if crashes:
            (evidence / f'{scenario}-crashes.json').write_text(json.dumps(crashes, ensure_ascii=False, indent=2))
        flags = termios.ICANON | termios.ECHO | termios.ISIG
        result = {'scenario': scenario, 'exit': process.returncode, 'raw_restored': (before[3] & flags) == (after[3] & flags),
            'shutdown_count': sum(entry['event'] == 'shutdown' for entry in lifecycle),
            'bioMind_error_count': text.count('BioMind 终端输入失败'), 'crash_count': len(crashes),
            'native_misleading_prompt': any(value in text for value in ('pi -r', 'pi exiting', 'pi update')),
            'persisted_recovery': 'biomind --project-dir' in text and '--session' in text,
            'first_ctrl_c_kept_running': first_ctrl_c_alive}
        good = result['exit'] == (1 if scenario == 'eio' else 0) and result['raw_restored'] and result['shutdown_count'] == 1
        good = good and result['bioMind_error_count'] == (1 if scenario == 'eio' else 0)
        good = good and result['crash_count'] == (1 if scenario == 'eio' else 0)
        good = good and not result['native_misleading_prompt'] and result['persisted_recovery']
        if scenario == 'ctrl-c':
            good = good and first_ctrl_c_alive
        result['status'] = 'PASS' if good else 'FAIL'
        checks.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (evidence / 'summary.json').write_text(json.dumps({'root': str(root), 'checks': checks,
        'incident_EIO_trigger': 'NOT_CONFIRMED', 'test_boundary': 'real isolated PTY and bundled Pi; injected input failure'}, ensure_ascii=False, indent=2))
    return 0 if all(check['status'] == 'PASS' for check in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
