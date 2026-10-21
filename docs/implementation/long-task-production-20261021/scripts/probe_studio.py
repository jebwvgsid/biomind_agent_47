"""Read current Studio identity; never emit authentication material."""
from pathlib import Path
import base64, json, subprocess, urllib.request
from datetime import datetime, timezone
B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_LongTask_20261021/evidence'
cfg = B / 'BioMind_Studio_Workbench_20261009/deployment/private/service.json'
service = B / 'BioMind_Unified_Core_20261009/delivery/service/plugin-lifecycle/59f269bcd8027d40b8685feffd14a656176d5817/server/server-service.py'
config = json.loads(cfg.read_text())
discovery = json.loads(subprocess.check_output(['python3', str(service), 'discover', '--config', str(cfg)], text=True, timeout=20))
result = {'retrieved_at': datetime.now(timezone.utc).isoformat(), 'discovery': discovery, 'entrypoint': config.get('environment', {}).get('BIOMIND_ENTRYPOINT'), 'runtime_config': config.get('environment', {}).get('BIOMIND_RUNTIME_CONFIG')}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Unexpected redirect')
password = json.loads(Path(config['environmentFile']).read_text())['PI_WEB_PASSWORD']
auth = 'Basic ' + base64.b64encode(('pi:' + password).encode()).decode()
client = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
for name, route in [('health', '/_biomind_studio/health'), ('profile', '/api/agent/profile'), ('running', '/api/agent/running')]:
    try:
        request = urllib.request.Request('http://127.0.0.1:' + str(discovery['port']) + route, headers={'Authorization': auth})
        with client.open(request, timeout=15) as response:
            result[name] = json.load(response)
    except Exception as exc:
        result[name] = {'error_type': type(exc).__name__}
(O / 'studio-current.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False))
