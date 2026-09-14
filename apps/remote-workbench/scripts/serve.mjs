import { createServer } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const file = fileURLToPath(new URL('../dist/index.html', import.meta.url));
if (!existsSync(file)) { console.error('Run npm run build first.'); process.exit(1); }
const port = Number(process.env.BIOMIND_PREVIEW_PORT || 18765);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid BIOMIND_PREVIEW_PORT');
const server = createServer((req, res) => {
  // Serve exactly one public prototype file; never expose the repository or arbitrary paths.
  if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405); res.end(); return; }
  if (!['/', '/index.html'].includes(req.url?.split('?')[0])) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
  res.end(req.method === 'HEAD' ? undefined : readFileSync(file));
});
server.on('error', error => { console.error(`Preview could not start: ${error.code}. Open dist/index.html directly instead.`); process.exitCode = 1; });
server.listen(port, '127.0.0.1', () => console.log(`BioMind 演示模式: http://127.0.0.1:${port}/ (loopback only)`));
