import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
let ts;
try { ts = require(process.env.BIOMIND_TYPESCRIPT_PATH || 'typescript'); }
catch { console.error('TypeScript unavailable. Run npm install, or set BIOMIND_TYPESCRIPT_PATH to an existing TypeScript library.'); process.exit(1); }
const configPath = path.join(root, 'tsconfig.json');
const config = ts.readConfigFile(configPath, ts.sys.readFile);
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, root);
const program = ts.createProgram(parsed.fileNames, parsed.options);
const diagnostics = [...(config.error ? [config.error] : []), ...parsed.errors, ...ts.getPreEmitDiagnostics(program)];
if (diagnostics.length) {
  console.error(ts.formatDiagnosticsWithColorAndContext(diagnostics, { getCurrentDirectory: () => root, getCanonicalFileName: p => p, getNewLine: () => '\n' }));
  process.exit(1);
}
console.log(`TypeScript ${ts.version}: strict typecheck passed (${parsed.fileNames.length} source files).`);
if (!process.argv.includes('--check')) {
  const emitted = program.emit();
  if (emitted.emitSkipped) throw new Error('TypeScript emission failed');
  const buildDir = path.join(root, '.build');
  const files = [];
  function collect(dir) { for (const f of readdirSync(dir, { withFileTypes: true })) { const p = path.join(dir, f.name); if (f.isDirectory()) collect(p); else if (f.name.endsWith('.js')) files.push(p); } }
  collect(buildDir);
  writeFileSync(path.join(buildDir, 'package.json'), '{"type":"commonjs"}\n');
  // Small local-only CommonJS packer. No external module or runtime loader is shipped.
  const modules = files.map(p => `${JSON.stringify(path.relative(buildDir, p).split(path.sep).join('/'))}: function(require, exports) {\n${readFileSync(p, 'utf8')}\n}`).join(',\n');
  const bundle = `(() => { 'use strict'; const modules = {${modules}}; const cache = {}; function load(id) { if(cache[id]) return cache[id]; const exports = cache[id] = {}; modules[id](name => { const parts=id.split('/'); parts.pop(); for(const p of name.split('/')) { if(p==='..') parts.pop(); else if(p!=='.') parts.push(p); } return load(parts.join('/')); }, exports); return exports; } load('main.js'); })();`;
  const html = readFileSync(path.join(root, 'index.html'), 'utf8')
    .replace('/* BIOMIND_CSS */', () => readFileSync(path.join(root, 'src/styles.css'), 'utf8'))
    .replace('/* BIOMIND_JS */', () => bundle.replace(/<\/script/gi, '<\\/script'));
  mkdirSync(path.join(root, 'dist'), { recursive: true });
  writeFileSync(path.join(root, 'dist/index.html'), html);
  writeFileSync(path.join(root, 'dist/workbench.js'), bundle);
  console.log(`Built dist/index.html (${Buffer.byteLength(html)} bytes). Open this file directly; no server or network is required.`);
}
