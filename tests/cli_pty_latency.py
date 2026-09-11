import fcntl,json,os,pty,select,struct,subprocess,sys,termios,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];folder=Path(sys.argv[1]);folder.mkdir(parents=True,exist_ok=True)
master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,100,0,0));attrs=termios.tcgetattr(slave)
env={'PATH':'/usr/bin:/bin','TERM':'xterm-256color','PYTHONPATH':str(ROOT)+':/tmp/biomind-cli-deps','PYTHONDONTWRITEBYTECODE':'1','BIOMIND_FIXTURE_LOG_BURST':'1'}
p=subprocess.Popen([sys.executable,'-s',str(ROOT/'tests/cli_fixture_entry.py')],cwd=folder,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
raw=bytearray()
def drain(seconds):
 until=time.monotonic()+seconds
 while time.monotonic()<until:
  if select.select([master],[],[],.01)[0]:
   try:chunk=os.read(master,65536)
   except OSError:return
   raw.extend(chunk)
   if b'\x1b[6n' in chunk:os.write(master,b'\x1b[1;1R')
try:
 until=time.monotonic()+45
 while b'Ctrl+C' not in raw and p.poll() is None and time.monotonic()<until:drain(.1)
 assert b'Ctrl+C' in raw
 drain(.3);sent=[]
 for i in range(1,33):
  sent.append((time.perf_counter_ns(),'测'*i));os.write(master,'测'.encode());drain(.04)
 drain(.3);os.write(master,b'\x15/exit\r')
 until=time.monotonic()+15
 while p.poll() is None and time.monotonic()<until:drain(.1)
 assert p.poll()==0
 data=json.loads((folder/'ui-metrics.json').read_text());events={text:t for t,text in data['input_events']}
 samples=[(events[text]-t)/1e6 for t,text in sent]
 p95=sorted(samples)[int(.95*(len(samples)-1))]
 result={'samples':len(samples),'buffer_input_ms':samples,'p50_ms':sorted(samples)[len(samples)//2],'p95_ms':p95,'target_under_100ms':p95<100,'terminal_bytes':len(raw),'perf_lines':raw.count(b'phase=state.load'),'frames':data['frames'],'history_entries':data['history_entries'],'termios_restored':termios.tcgetattr(slave)==attrs}
 assert result['termios_restored'] and result['perf_lines']==0
 (folder/'latency.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master);os.close(slave)
