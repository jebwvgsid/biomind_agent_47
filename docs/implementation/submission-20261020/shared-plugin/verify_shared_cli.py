"""Bounded real-model smoke through the official CLI and its default resources."""
from pathlib import Path
import json
import os
import select
import subprocess
import time
import xml.etree.ElementTree as ET

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020/shared-plugin'
RUN = O / 'cli-network'
JSON_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=19304878%5Buid%5D&retmode=json&retmax=1'
XML_URL = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=19304878&retmode=xml'

def main():
    RUN.mkdir(mode=0o700)
    project = RUN / 'project'
    project.mkdir()
    cmd = ['/public/home/liusiqi/.local/bin/biomind', '--project-dir', str(project),
           '--pi-mode', 'json', '--session-dir', str(RUN / 'sessions')]
    env = dict(os.environ, BIOMIND_DISABLE_DOTENV='1', PYTHONDONTWRITEBYTECODE='1',
               PI_WEB_ACCESS_CACHE_ROOT=str(RUN / 'web-cache'))
    frames, buf = [], b''
    stderr = (RUN / 'stderr.log').open('wb')
    proc = subprocess.Popen(cmd, cwd=project, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=stderr)
    receipt = {'command': cmd, 'customRuntimeConfig': False, 'modelRequests': 0,
               'scientificRuns': 0, 'status': 'FAIL'}
    def wait(predicate, seconds):
        nonlocal buf
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            value = next((f for f in frames if predicate(f)), None)
            if value is not None:
                return value
            if proc.poll() is not None:
                raise RuntimeError('CLI exited before response')
            if select.select([proc.stdout], [], [], .1)[0]:
                buf += os.read(proc.stdout.fileno(), 65536)
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    try:
                        frames.append(json.loads(line))
                    except ValueError:
                        pass
        raise TimeoutError('CLI response timeout')
    def send(frame):
        proc.stdin.write((json.dumps(frame) + '\n').encode())
        proc.stdin.flush()
    try:
        ready = wait(lambda f: f.get('type') == 'ready', 120)
        build = json.loads((B / 'BioMind_Production_20261020/build-result.json').read_text())
        assert ready['loaded_build']['buildId'] == build['releaseId']
        receipt['ready'] = ready
        for name in ('get_tools', 'get_resources'):
            send({'type': name, 'id': name})
            receipt[name] = wait(lambda f: f.get('id') == name, 30)
        names = {t['name'] for t in receipt['get_tools']['data']['registered']}
        assert {'fetch_content', 'web_search', 'get_search_content'} <= names
        receipt['loadedWebTools'] = sorted(names & {'fetch_content', 'web_search', 'get_search_content'})
        prompt = f'''本次仅做生产联网插件的三次小请求验收，不执行生物学分析。
请使用 fetch_content(mode=raw) 分别获取以下两个官方 NCBI 地址，每个地址只请求一次：
{JSON_URL}
{XML_URL}
再用 web_search 搜索 NCBI PubMed 19304878，numResults=1，仅请求一次。
总共最多三次网络工具调用。失败不重试、不换代理、不新增凭据；不调用 bash、不写文件。
报告实际返回的 JSON idlist、XML PMID 和搜索状态。不要把工具加载成功或 error:null 当作联网成功。'''
        send({'type': 'prompt', 'id': 'network-acceptance', 'clientMessageId': 'network-acceptance',
              'ownerGeneration': ready['ownerGeneration'], 'sessionId': ready['sessionId'],
              'project': ready['cwd'], 'text': prompt})
        receipt['modelRequests'] = 1
        receipt['result'] = wait(lambda f: f.get('type') == 'result', 240)
        ends = [f['event'] for f in frames if f.get('type') == 'event'
                and f.get('event', {}).get('type') == 'tool_execution_end']
        receipt['networkTools'] = []
        json_ok, xml_ok = False, False
        for i, e in enumerate(ends):
            name = e.get('toolName')
            if name not in ('fetch_content', 'web_search'):
                continue
            result = e.get('result', {})
            (RUN / f'tool-{i}-{name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            receipt['networkTools'].append({'tool': name, 'isError': e.get('isError')})
            for row in (result.get('structuredContent') or {}).get('urls', []):
                content = row.get('content')
                if not isinstance(content, str):
                    continue
                try:
                    value = json.loads(content)
                    json_ok |= value.get('esearchresult', {}).get('idlist') == ['19304878']
                except ValueError:
                    pass
                try:
                    doc = ET.fromstring(content)
                    xml_ok |= any(x.text == '19304878' for x in doc.findall('.//PMID'))
                except ET.ParseError:
                    pass
        receipt['ncbiJsonValidated'] = json_ok
        receipt['ncbiXmlValidated'] = xml_ok
        receipt['status'] = 'PASS_NCBI' if json_ok and xml_ok else 'FAIL_NETWORK'
    except Exception as e:
        receipt['error'] = type(e).__name__ + ': ' + str(e)
    finally:
        if proc.poll() is None:
            try:
                send({'type': 'shutdown', 'id': 'close'})
                proc.stdin.close()
                proc.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                # Only this script's isolated smoke child, never a user session.
                proc.terminate()
                proc.wait(10)
        stderr.close()
        (RUN / 'frames.jsonl').write_text(''.join(json.dumps(f, ensure_ascii=False) + '\n' for f in frames))
        (RUN / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: receipt.get(k) for k in ('status', 'loadedWebTools', 'ncbiJsonValidated', 'ncbiXmlValidated', 'error')}))
    raise SystemExit(receipt['status'] != 'PASS_NCBI')

if __name__ == '__main__':
    main()
