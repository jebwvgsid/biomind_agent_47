import fcntl,json,os,pty,select,signal,struct,subprocess,sys,termios,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];ART=Path(sys.argv[1]);ART.mkdir(parents=True,exist_ok=True)
ENV={'PATH':'/usr/bin:/bin','TERM':'xterm-256color','PYTHONPATH':str(ROOT)+':/tmp/biomind-cli-deps','PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}
results={}
for scenario in ['approve','exception','cancel-delayed','renderer-failure']:
 folder=ART/scenario;folder.mkdir(exist_ok=True)
 master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0));attrs=termios.tcgetattr(slave)
 proc=subprocess.Popen([sys.executable,'-s',str(ROOT/'tests/cli_fixture_entry.py')],cwd=folder,env=ENV,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
 raw=bytearray();events=[];start=time.monotonic()
 def drain(seconds):
  until=time.monotonic()+seconds
  while time.monotonic()<until:
   ready,_,_=select.select([master],[],[],.03)
   if ready:
    try:chunk=os.read(master,65536)
    except OSError:break
    if not chunk:break
    raw.extend(chunk);events.append([time.monotonic()-start,'o',chunk.decode(errors='replace')])
    if b'\x1b[6n' in chunk:os.write(master,b'\x1b[1;1R')
 def wait_for(predicate,seconds=20):
  until=time.monotonic()+seconds
  while not predicate() and proc.poll() is None and time.monotonic()<until:drain(.1)
  assert predicate(),raw.decode(errors='replace')[-3000:]
 try:
  wait_for(lambda:b'Ctrl+C' in raw,45);drain(.5)
  os.write(master,scenario.encode()+b'\r')
  wait_for(lambda:(folder/'submitted.json').exists())
  if scenario=='cancel-delayed':
   drain(.2);os.write(master,b'\x03');drain(.3);os.write(master,b'\x03');drain(.5)
   assert (folder/'cancel_count.json').read_text()=='1'
   assert '取消仍待确认'.encode() in raw
   os.write(master,b'\x04');expected=130
  elif scenario=='approve':
   wait_for(lambda:'fixture-v1'.encode() in raw)
   os.write(master,b'\x1bOQ');drain(.3);os.write(master,b'\x1b');drain(.6)
   os.write(master,b'n\r');wait_for(lambda:(folder/'approval.json').exists())
   assert json.loads((folder/'approval.json').read_text())=='n'
   drain(.5);os.write(master,b'/exit\r');expected=0
  elif scenario=='renderer-failure':
   wait_for(lambda:'降级 plain'.encode() in raw);drain(.2)
   os.write(master,b'/exit\r');expected=0
  else:
   wait_for(lambda:'交互命令失败'.encode() in raw);drain(.3)
   os.write(master,b'/exit\r');expected=0
  wait_for(lambda:proc.poll() is not None,15)
  drain(.2)
  assert proc.returncode==expected
  assert termios.tcgetattr(slave)==attrs
  results[scenario]={'exit_code':proc.returncode,'termios_restored':True,'terminal_bytes':len(raw)}
 finally:
  if proc.poll() is None:proc.kill();proc.wait()
  header={'version':2,'width':100,'height':24,'title':'BioMind exact CLI / fake '+scenario}
  (folder/'terminal.cast').write_text('\n'.join(json.dumps(x,ensure_ascii=False) for x in [header,*events])+'\n')
  os.close(master);os.close(slave)
# Explicit plain TTY commands must never become model tasks.
folder=ART/'plain-commands';folder.mkdir(exist_ok=True)
master,slave=pty.openpty()
env=dict(ENV,BIOMIND_UI='plain')
proc=subprocess.Popen([sys.executable,'-s',str(ROOT/'tests/cli_fixture_entry.py')],cwd=folder,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
raw=bytearray();events=[];start=time.monotonic()
try:
 wait_for(lambda:b'BioMind' in raw,45)
 os.write(master,b'/status\n/details\n/logs\n/exit\n')
 wait_for(lambda:proc.poll() is not None,15)
 assert proc.returncode==0 and b'\x1b' not in raw
 assert not (folder/'submitted.json').exists()
 results['plain_commands']={'exit_code':0,'ansi_bytes':0,'business_dispatches':0}
finally:
 if proc.poll() is None:proc.kill();proc.wait()
 os.close(master);os.close(slave)
plain=subprocess.run([sys.executable,'-s',str(ROOT/'tests/cli_fixture_entry.py')],cwd=ART,env=ENV,input=b'',stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
assert plain.returncode==0 and b'\x1b' not in plain.stdout+plain.stderr
results['non_tty']={'exit_code':plain.returncode,'ansi_bytes':0,'output':plain.stdout.decode()}
(ART/'edge-summary.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False))
