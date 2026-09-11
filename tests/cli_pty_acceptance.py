"""Real PTY recording of the exact CLI entry with isolated fake backend."""
import base64
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time

ROOT=Path(__file__).resolve().parents[1]
ART=Path(sys.argv[1]).resolve()
ART.mkdir(parents=True,exist_ok=True)

def run(name, text, *, cancel=False):
    folder=ART/name;folder.mkdir(exist_ok=True)
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
    attrs=termios.tcgetattr(slave)
    env={'PATH':'/usr/bin:/bin','TERM':'xterm-256color','PYTHONPATH':str(ROOT)+':/tmp/biomind-cli-deps','PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
    process=subprocess.Popen([sys.executable,'-s',str(ROOT/'tests/cli_fixture_entry.py')],cwd=folder,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
    started=time.monotonic(); recording=[]; raw=bytearray()
    def drain(seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            ready,_,_=select.select([master],[],[],min(.05,max(0,deadline-time.monotonic())))
            if ready:
                try:chunk=os.read(master,65536)
                except OSError:break
                if not chunk:break
                raw.extend(chunk)
                if b'\x1b[6n' in chunk:
                    os.write(master,b'\x1b[1;1R')
                recording.append([round(time.monotonic()-started,6),'o',chunk.decode('utf-8','replace')])
    def send(value):
        os.write(master,value)
    try:
        deadline=time.monotonic()+45
        while b'Ctrl+C' not in raw and process.poll() is None and time.monotonic()<deadline:
            drain(.2)
        assert b'Ctrl+C' in raw, raw.decode(errors='replace')[-3000:]
        drain(.5)
        send(b'\x1b[200~'+text.encode()+b'\x1b[201~')
        drain(.2);send(b'\r')
        deadline=time.monotonic()+20
        while not (folder/'submitted.json').exists() and process.poll() is None and time.monotonic()<deadline:
            drain(.2)
        assert (folder/'submitted.json').exists(),raw.decode(errors='replace')[-5000:]
        assert json.loads((folder/'submitted.json').read_text())==text
        send('运行中中文草稿'.encode());drain(.2)
        for width in [120,35,80]:
            fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,width,0,0))
            process.send_signal(28);drain(.2)
        send(b'\x1bOQ');drain(.2) # F2
        send(b'\x1b');drain(.7)
        if cancel:
            send(b'\x03');drain(1)
            assert (folder/'cancel_count.json').read_text()=='1'
        else:
            drain(30)
        assert json.loads((folder/'draft-returned.json').read_text())=='运行中中文草稿'
        # Draft remains local; clear explicitly before exiting.
        send(b'\x15');send(b'/exit\r');drain(2)
        process.wait(timeout=5)
        restored=termios.tcgetattr(slave)==attrs
        assert process.returncode==0,(process.returncode,raw.decode(errors='replace')[-4000:])
        assert restored,'termios not restored'
        assert b'phase=state.load' not in raw
        assert b'PROVIDER_ATTEMPT_EVENT' not in raw
        summary={'exit_code':process.returncode,'termios_restored':restored,'terminal_bytes':len(raw),'perf_lines':raw.count(b'phase=state.load'),'input_exact':True,'input_lines':len(text.splitlines()),'elapsed_seconds':time.monotonic()-started}
        (folder/'summary.json').write_text(json.dumps(summary,indent=2))
        return summary
    finally:
        if process.poll() is None:
            process.kill();process.wait()
        header={'version':2,'width':80,'height':24,'timestamp':int(time.time()),'env':{'TERM':'xterm-256color'},'title':'BioMind exact CLI entry / fake backend: '+name}
        (folder/'terminal.cast').write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in [header,*recording])+'\n')
        (folder/'terminal.raw').write_bytes(raw)
        os.close(master);os.close(slave)

payload='\n'.join(['　中文路径 /tmp/样本\t'+str(i) if i%5 else ('/命令 !echo 不得执行' if i%10 else '') for i in range(210)])
results={'paste_wait_resize':run('paste_wait_resize',payload),'cancel':run('cancel','取消测试',cancel=True)}
(ART/'pty-summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False))
