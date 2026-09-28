"""Real PTY/Application replay of synthetic inputs through production producers.

The recording is output from a kernel PTY, not a DummyOutput demonstration.
A small VT text decoder makes inspectable last-screen snapshots; raw bytes and
asciicast remain available, including a list of unsupported control sequences.
"""
import argparse
import asyncio
import codecs
import concurrent.futures
import errno
import fcntl
import hashlib
import html
import inspect
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

PYTHON='/public/home/liusiqi/anaconda3/envs/biomind/bin/python'
DEPS='/tmp/biomind-cli-deps'


def pulse(loop):
    def service():
        if not loop.is_closed():loop.call_later(.002,service)
    service()


def atomic_json(path,value):
    temporary=path.with_suffix('.new')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str))
    temporary.replace(path)






class VTTextScreen:
    """Decode the VT cursor/erase operations emitted by prompt_toolkit.

    Styles are intentionally omitted from the text/SVG snapshot. Unsupported
    commands are recorded so a screen is never silently claimed complete.
    """
    def __init__(self,cols,rows):
        sys.path.insert(0,DEPS)
        from wcwidth import wcwidth
        self.width=wcwidth;self.cols=cols;self.rows=rows;self.x=0;self.y=0
        self.grid=[[' ']*cols for _ in range(rows)];self.history=[];self.pending='';self.unknown=[];self.saved=(0,0)
    def resize(self,cols,rows):
        self.grid=[(row+[' ']*cols)[:cols] for row in self.grid[:rows]]
        self.grid += [[' ']*cols for _ in range(rows-len(self.grid))]
        self.cols,self.rows=cols,rows;self.x=min(self.x,cols-1);self.y=min(self.y,rows-1)
    def down(self):
        self.y+=1
        if self.y>=self.rows:
            self.history.append(''.join(self.grid.pop(0)).rstrip());self.grid.append([' ']*self.cols);self.y=self.rows-1
    def feed(self,text):
        self.pending+=text;replies=[]
        while self.pending:
            if self.pending.startswith('\x1b['):
                match=re.match(r'\x1b\[([0-?]*)([ -/]*)([@-~])',self.pending)
                if not match:break
                args,inter,op=match.groups();self.pending=self.pending[match.end():]
                nums=[int(v) if v else 0 for v in args.lstrip('?').split(';')];n=nums[0] or 1
                if op=='A':self.y=max(0,self.y-n)
                elif op=='B':self.y=min(self.rows-1,self.y+n)
                elif op=='C':self.x=min(self.cols-1,self.x+n)
                elif op=='D':self.x=max(0,self.x-n)
                elif op in 'Hf':self.y=min(self.rows-1,(nums[0] or 1)-1);self.x=min(self.cols-1,(nums[1] if len(nums)>1 and nums[1] else 1)-1)
                elif op=='G':self.x=min(self.cols-1,n-1)
                elif op=='d':self.y=min(self.rows-1,n-1)
                elif op=='K':
                    left=0 if nums[0] in (1,2) else self.x;right=self.cols if nums[0] in (0,2) else self.x+1
                    self.grid[self.y][left:right]=[' ']*(right-left)
                elif op=='J':
                    if nums[0] in (2,3):self.grid=[[' ']*self.cols for _ in range(self.rows)]
                    elif nums[0]==0:
                        self.grid[self.y][self.x:]=[' ']*(self.cols-self.x)
                        for row in range(self.y+1,self.rows):self.grid[row]=[' ']*self.cols
                    else:self.unknown.append(match.group())
                elif op=='n' and nums[0]==6:replies.append(f'\x1b[{self.y+1};{self.x+1}R')
                elif op in 'mhlq':pass
                elif op=='s':self.saved=(self.x,self.y)
                elif op=='u':self.x,self.y=self.saved
                else:self.unknown.append(match.group())
                continue
            if self.pending.startswith('\x1b]'):
                end=re.search('\x07|\x1b\\\\',self.pending)
                if not end:break
                self.pending=self.pending[end.end():];continue
            if self.pending[0]=='\x1b':
                if len(self.pending)<2:break
                op=self.pending[1];self.pending=self.pending[2:]
                if op=='7':self.saved=(self.x,self.y)
                elif op=='8':self.x,self.y=self.saved
                elif op in '=>':pass
                else:self.unknown.append('\x1b'+op)
                continue
            c=self.pending[0];self.pending=self.pending[1:]
            if c=='\r':self.x=0
            elif c=='\n':self.down()
            elif c=='\b':self.x=max(0,self.x-1)
            elif c=='\t':self.x=min(self.cols-1,(self.x//8+1)*8)
            elif ord(c)<32:pass
            else:
                width=max(0,self.width(c))
                if width==0:continue
                if self.x+width>self.cols:self.x=0;self.down()
                self.grid[self.y][self.x]=c
                if width==2:self.grid[self.y][self.x+1]=''
                self.x+=width
        return replies
    def text(self):return '\n'.join(''.join(row).rstrip() for row in self.grid)


def snapshot(root,name,screen):
    from PIL import Image,ImageDraw,ImageFont
    image=Image.new('RGB',(screen.cols*10+24,screen.rows*20+24),'#17191c')
    draw=ImageDraw.Draw(image);font=ImageFont.truetype('/usr/share/fonts/google-noto-cjk/NotoSansCJK-DemiLight.ttc',16)
    for y,row in enumerate(screen.grid):
        for x,char in enumerate(row):
            if char and char!=' ':draw.text((12+x*10,9+y*20),char,font=font,fill='#e6e7e8')
    image.save(root/(name+'.screen.png'))
    text=screen.text();(root/(name+'.screen.txt')).write_text(text)
    width,height=screen.cols*9+24,screen.rows*19+24
    rows=''.join(f'<text x="12" y="{20+19*i}">{html.escape(line)}</text>' for i,line in enumerate(text.splitlines()))
    (root/(name+'.screen.svg')).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#17191c"/><g fill="#e6e7e8" font-family="monospace" font-size="15" xml:space="preserve">{rows}</g></svg>')


def record(source,root,scenario,cols,rows,verbosity,resize=None,term='xterm-256color'):
    source=source.resolve();root=root.resolve()
    assert source.is_relative_to(Path('/tmp')) and root.is_relative_to(Path('/tmp'))
    manifest=json.loads((source/'export_manifest.json').read_text())
    assert all(hashlib.sha256((source/name).read_bytes()).hexdigest()==digest for name,digest in manifest['files'].items())
    root.mkdir(parents=True,exist_ok=False);(root/'scratch').mkdir()
    env={'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','PYTHONPATH':DEPS,'TERM':term,
        'COLUMNS':str(cols),'LINES':str(rows),'PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1',
        'TMPDIR':str(root/'scratch'),'BIOMIND_WIKI_ENABLED':'0','LOG_LEVEL':'CRITICAL',
        'BIOMIND_OFFLINE_RESULTS':str(root/'pytest-results.json'),'BIOMIND_CLI_PROGRESS_ARTIFACTS':str(root),
        'BIOMIND_GER_ARTIFACTS':str(root),'BIOMIND_EXEC_LOG_DIR':str(root/'scratch/executor-logs'),
        'BIOMIND_V2_PTY':'1','BIOMIND_V2_SCENARIO':scenario,'BIOMIND_CLI_VERBOSITY':verbosity}
    # The committed guard executes the existing audited bootstrap. Its initial
    # harmless synthetic-config printout is kept in raw recording as provenance.
    command=[PYTHON,str(source/'tests/cli_model_progress_support.py'),'--guarded-child',str(source),
        '-q','-s','tests/replay_cli_v2_pty.py::test_v2_real_pty_replay']
    master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
    child=subprocess.Popen(command,cwd=source,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
    os.close(slave);screen=VTTextScreen(cols,rows);decode=codecs.getincrementaldecoder('utf-8')('replace')
    started=time.monotonic();actions=[];seen=set();resize_done=False;modal_opened=False
    recording=False
    raw=(root/'terminal.raw').open('wb');cast=(root/'terminal.cast').open('w')
    cast.write(json.dumps({'version':2,'width':cols,'height':rows,'timestamp':int(time.time()),'title':'BioMind synthetic production PTY replay'})+'\n')
    def send(data,label):
        os.write(master,data.encode());actions.append({'at':time.monotonic()-started,'action':label})
    def read_json(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    try:
        while time.monotonic()-started<90:
            if select.select([master],[],[],.02)[0]:
                try:data=os.read(master,65536)
                except OSError as exc:
                    if exc.errno==errno.EIO:break
                    raise
                if not data:break
                raw.write(data);raw.flush();text=decode.decode(data)
                marker='\x1b]133;A;biomind-replay-start\x07'
                if marker in text:
                    recording=True;text_for_cast='\x1b[2J\x1b[H'+text.split(marker,1)[1]
                else:text_for_cast=text
                if recording and 'captured' not in seen:
                    cast.write(json.dumps([time.monotonic()-started,'o',text_for_cast],ensure_ascii=False)+'\n');cast.flush()
                for reply in screen.feed(text):send(reply,'cursor_position_report')
            phase=read_json('phase.json').get('phase');frame=read_json('frame-state.json')
            if phase=='interaction_ready' and 'draft' not in seen:
                send('中文草稿\x0a第二行 /tmp/合成长路径/输入文件.txt','multiline_draft');send('\x1bOQ','F2_details');seen.add('draft')
            if phase=='interaction_ready' and frame.get('modal') and 'proceed' not in seen:
                snapshot(root,'initial-details',screen);modal_opened=True
                if scenario=='approval':
                    if 'initial-closing' not in seen:send('\x1b','close_initial_details');seen.add('initial-closing')
                else:(root/'proceed').write_text('continue fixture');seen.add('proceed')
            if phase=='interaction_ready' and scenario=='approval' and 'initial-closing' in seen and not frame.get('modal') and 'proceed' not in seen:
                (root/'proceed').write_text('continue fixture');seen.add('proceed')
            if resize and 'proceed' in seen and not resize_done:
                newcols,newrows=resize;fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',newrows,newcols,0,0));screen.resize(newcols,newrows)
                os.kill(child.pid,signal.SIGWINCH);actions.append({'at':time.monotonic()-started,'resize':[newcols,newrows]});resize_done=True
            if phase=='approval_pending' and frame.get('pending') and not frame.get('modal') and 'approval-details' not in seen and 'approval-captured' not in seen:
                send('\x1bOQ','approval_F2');seen.add('approval-details')
            if phase=='approval_pending' and frame.get('pending') and frame.get('modal') and 'approval-captured' not in seen:
                snapshot(root,'approval-details',screen);send('\x1b[6~','approval_page_down');send('\x1b','approval_Escape');seen.add('approval-captured')
            if phase=='approval_pending' and not frame.get('modal') and 'approval-captured' in seen and 'rejected' not in seen:
                send('n\r','approval_reject');seen.add('rejected')
            if phase=='cancel_ready' and 'cancel' not in seen:send('\x03','CtrlC_cancel');seen.add('cancel')
            if phase=='business_done' and 'done' not in seen:
                snapshot(root,'business-details',screen);send('\x1b','Escape_to_input');seen.add('done')
            if phase=='snapshot_ready' and 'captured' not in seen:
                snapshot(root,'final',screen);(root/'capture_done').write_text('captured');seen.add('captured')
            if child.poll() is not None:break
        if child.poll() is None:child.terminate()
        returncode=child.wait(timeout=5)
    finally:
        raw.close();cast.close();os.close(master)
        if child.poll() is None:child.kill();child.wait()
    business=read_json('business.json');render=read_json('renderer.json')
    history=[row for row in render.get('items',[]) if row.get('presentation')=='history']
    metrics={'returncode':returncode,'source':manifest['source'],'source_signature':manifest['source_signature'],
        'scenario':scenario,'verbosity':verbosity,'initial_size':[cols,rows],'resize':resize,
        'actual_pty':True,'production_application':True,'counts':business.get('counts'),
        'history_items':len(history),'permanent_logical_lines':sum(len(row['text'].splitlines()) or 1 for row in history),
        'model_paragraphs':sum(row['kind']=='model_public' for row in history),
        'tool_history_items':sum(row['kind'] in ('tool_start','tool_end') for row in history),
        'diagnostic_items':len(render.get('details',[])),'actions':actions,'unsupported_vt':sorted(set(screen.unknown)),
        'captured': 'captured' in seen,'modal_opened':modal_opened,'session_route_evidence':False,
        'replay_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    atomic_json(root/'metrics.json',metrics)
    (root/'invocation.json').write_text(json.dumps({'command':command,'environment':env},indent=2))
    print(json.dumps(metrics,ensure_ascii=False))
    return returncode if 'captured' in seen else 2


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export',type=Path,required=True);parser.add_argument('--results',type=Path,required=True)
    parser.add_argument('--scenario',choices=['normal','failure','approval'],required=True)
    parser.add_argument('--cols',type=int,default=80);parser.add_argument('--rows',type=int,default=24)
    parser.add_argument('--verbosity',choices=['compact','verbose'],default='compact')
    parser.add_argument('--resize');parser.add_argument('--term',default='xterm-256color')
    args=parser.parse_args();resize=tuple(map(int,args.resize.split('x'))) if args.resize else None
    raise SystemExit(record(args.export,args.results,args.scenario,args.cols,args.rows,args.verbosity,resize,args.term))
if __name__=='__main__':main()
