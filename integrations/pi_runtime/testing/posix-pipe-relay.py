#!/usr/bin/env python3
"""Test-only byte relay: create the POSIX pipes used by the Python benchmark.

This runner environment rejects Node's JavaScript stdio on child_process socket
pairs. Python os.pipe works. No protocol, decision, provider or domain is mocked.
It does not certify a Node parent using socket pairs in this environment.
"""
import os
import json
import select
import subprocess
import sys
import resource
import time
from pathlib import Path

def memory_events():
    """Small read-only cgroup counter snapshot; absence is reported, never inferred."""
    result={}
    try:
        for row in Path('/proc/self/cgroup').read_text().splitlines():
            hierarchy,controllers,relative=row.split(':',2)
            if hierarchy=='0' and controllers=='':
                base=Path('/sys/fs/cgroup')/relative.lstrip('/')
                for filename in ('memory.events','memory.events.local'):
                    try:result[filename]=dict(line.split() for line in (base/filename).read_text().splitlines())
                    except OSError:pass
    except (OSError,ValueError):pass
    return result

started=time.monotonic();initial_memory_events=memory_events()

process=subprocess.Popen(sys.argv[1:],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
streams={0:process.stdin.fileno(),process.stdout.fileno():1,process.stderr.fileno():2}
try:
    while streams:
        readable,_,_=select.select(list(streams),[],[],1)
        for source in readable:
            data=os.read(source,65536)
            if not data:
                if source==0:process.stdin.close()
                streams.pop(source,None)
                continue
            destination=streams[source]
            while data:
                try:count=os.write(destination,data)
                except BrokenPipeError:streams.pop(source,None);break
                data=data[count:]
        if process.poll() is not None and set(streams).issubset({0}):break
finally:
    if not process.stdin.closed:process.stdin.close()
    try:code=process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate();code=process.wait(timeout=5)
if code:
    # A negative child code becomes 256-signal at Python's outer exit boundary.
    # Preserve the actual observation; a signal alone does not establish OOM.
    print(json.dumps({'test_relay_child_exit': code, 'child_pid': process.pid,
        'observed_signal': -code if code < 0 else None,
        'elapsed_seconds':round(time.monotonic()-started,3),
        'child_max_rss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        'cgroup_memory_events_before':initial_memory_events,'cgroup_memory_events_after':memory_events(),
        'signal_sender':'unknown; relay did not send SIGKILL'}), file=sys.stderr, flush=True)
raise SystemExit(code)
