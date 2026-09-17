"""Independent file-backed process wait distinguishes tool failure from pipe wait."""
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

base=Path(__file__).parent
old=json.loads((base/'runtime_probe_results.json').read_text())['probes']
follow=json.loads((base/'runtime_gatk_followup_results.json').read_text())
labels={'wgs_java','wgs_bwa_mem','wgs_samtools_coverage_help'}
rows=[]
with tempfile.TemporaryDirectory(prefix='biomind-probe-wait-') as tmp:
    for source in old+follow:
        if source['label'] not in labels: continue
        env={**source['environment_allowlist'],'TMPDIR':tmp}
        before=time.monotonic()
        start=datetime.datetime.now(datetime.timezone.utc).isoformat()
        paths={key:Path(tmp)/(source['label']+'.'+key) for key in ('stdout','stderr')}
        outcome='completed'
        with paths['stdout'].open('wb') as stdout,paths['stderr'].open('wb') as stderr:
            proc=subprocess.Popen(source['argv'],env=env,cwd=tmp,stdin=subprocess.DEVNULL,
                stdout=stdout,stderr=stderr,start_new_session=True)
            while proc.poll() is None:
                if time.monotonic()-before>5:
                    outcome='timeout'; break
                if sum(p.stat().st_size for p in paths.values())>65536:
                    outcome='output_limit'; break
                time.sleep(0.01)
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGKILL)
            rc=proc.wait(timeout=2)
        captured={key:p.read_bytes()[:65536].decode(errors='replace') for key,p in paths.items()}
        rows.append({'label':source['label'],'original_outcome':source['outcome'],
            'original_returncode':source['returncode'],'argv':source['argv'],
            'environment_allowlist':env,'cwd':tmp,'started_at':start,
            'elapsed_seconds':time.monotonic()-before,'timeout_seconds':5,
            'output_limit_bytes':65536,'capture_mode':'regular_files_with_poll_and_output_limit',
            'outcome':outcome,'returncode':rc,**captured})
data={'purpose':'Discriminate async pipe-completion probe timeout from actual tool termination',
      'result_code':'PROBE_WAIT_OR_PIPE_COMPLETION_ANOMALY','results':rows}
(base/'runtime_probe_wait_check_results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
for row in rows:print(row['label'],row['returncode'],row['outcome'],round(row['elapsed_seconds'],3))
