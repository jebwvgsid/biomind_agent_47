"""Replay the committed fake-fixture PTY recordings in a real terminal."""
import argparse,json,sys,time
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('recording',type=Path);parser.add_argument('--speed',type=float,default=4)
args=parser.parse_args()
if not sys.stdout.isatty() or args.speed<=0:raise SystemExit('Use a TTY and a positive --speed')
rows=args.recording.read_text().splitlines();header=json.loads(rows[0]);assert header['version']==2
previous=0
try:
 for row in rows[1:]:
  timestamp,kind,data=json.loads(row)
  time.sleep(max(0,timestamp-previous)/args.speed);previous=timestamp
  if kind=='o':sys.stdout.write(data);sys.stdout.flush()
finally:
 sys.stdout.write('\x1b[0m\x1b[?25h\x1b[?2004l\n');sys.stdout.flush()
