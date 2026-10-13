'use strict';
// Real flowflic PluginsPanel + store, with only IPC/browser APIs as fixtures.
const { test, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const Module = require('node:module');
const root = path.resolve(__dirname, '..'), desktop = path.join(root, 'desktop');
const { parseHTML } = require(path.join(root, 'backend/pi-studio/node_modules/linkedom'));
const { window } = parseHTML('<html><head></head><body></body></html>');
global.window = window; global.document = window.document;
Object.defineProperty(global, 'navigator', { configurable: true, value: { userAgent: 'offline-packages' } });
global.IS_REACT_ACT_ENVIRONMENT = true;
global.requestAnimationFrame = fn => setImmediate(fn);
const React = require(path.join(desktop, 'node_modules/react')), { act } = React;
const { createRoot } = require(path.join(desktop, 'node_modules/react-dom/client'));
const compiled = require(path.join(desktop, 'node_modules/esbuild')).buildSync({
  stdin: { contents: 'export { PluginsPanel } from "./src/renderer/src/components/PluginsPanel"; export { useStore } from "./src/renderer/src/store";', resolveDir: desktop, loader: 'ts' },
  bundle: true, write: false, format: 'cjs', platform: 'node', target: 'node22', jsx: 'automatic', external: ['react', 'react-dom', 'react-dom/*'], loader: { '.png': 'dataurl' },
}).outputFiles[0].text;
const fixture = new Module(path.join(desktop, 'plugins-fixture.cjs'), module); fixture.filename = fixture.id; fixture.paths = Module._nodeModulePaths(desktop); fixture._compile(compiled, fixture.filename);
const { PluginsPanel, useStore } = fixture.exports;
const pkg = { source: 'npm:fixture@1.0.0', scope: 'user', declaration: { source: 'npm:fixture@1.0.0', autoload: false, extensions: ['selected.ts'], skills: ['!private/**'] }, configured: true, installed: true, discovered: 'unknown', workerLoaded: 'unknown', active: 'unknown' };
const receipt = extra => ({ schema: 'BioMindPluginsV1', serverId: 'synthetic', status: 'ok', code: 'OK', changed: false, reloadRequired: false, repairRequired: false,
  packages: [structuredClone(pkg)], revision: 'fixture-rev', context: { agentDir: '/server/agent', cwd: null, piVersion: '0.87.1', trust: { trusted: false, saved: null, default: 'ask' } }, ...extra });
let lists, writes;
beforeEach(() => {
  lists = []; writes = [];
  window.pi = { plugins: { getPackages: async args => { lists.push(args); return receipt(); }, managePackages: async args => { writes.push(args); return receipt({ changed: true }); }, getSkills: async () => { throw new Error('must not create session'); } } };
  useStore.setState({ config: { language: 'zh' }, activeThreadId: null, activeProjectCwd: null, threads: {}, pluginsOpen: true, packages: [], packageResult: null, pluginsLoading: false, packageBusy: false, skills: [], toasts: [] });
});
async function mounted(run) {
  const container = document.createElement('div'); document.body.appendChild(container); const component = createRoot(container);
  try { await act(async () => component.render(React.createElement(PluginsPanel))); await run(container); }
  finally { await act(async () => component.unmount()); container.remove(); }
}
test('actual panel loads user packages without any native/model session and separates loaded unknown from installed', async () => mounted(async dom => {
  assert.deepEqual(lists, [{}]); assert.match(dom.textContent, /已安装/); assert.match(dom.textContent, /worker: unknown/); assert.match(dom.textContent, /显式资源选择（配置）/);
  assert.match(dom.textContent, /user \+ 当前可信 project/); assert.equal(dom.querySelector('fieldset[disabled]'), null);
  const declaration = dom.querySelector('textarea').value; assert.deepEqual(JSON.parse(declaration), pkg.declaration);
  assert.equal(dom.querySelector('textarea').disabled, false);
}));
test('actual save declaration button keeps autoload and every filter and sends expected revision', async () => mounted(async dom => {
  const button = [...dom.querySelectorAll('button')].find(row => row.textContent.includes('保存资源声明'));
  await act(async () => button.click()); assert.equal(writes.length, 1);
  assert.deepEqual(writes[0], { action: 'config', scope: 'user', source: pkg.source, declaration: pkg.declaration, expectedRevision: 'fixture-rev' });
}));
test('structured BUSY is not reported as success; no hidden retry or fake enabled state', async () => {
  window.pi.plugins.managePackages = async args => { writes.push(args); return receipt({ status: 'BUSY', code: 'LEGACY_OCCUPANCY_UNVERIFIED' }); };
  await useStore.getState().loadPlugins(); assert.equal(await useStore.getState().installPackage('npm:fixture', 'user'), false);
  assert.equal(writes.length, 1); assert.equal(useStore.getState().packageResult.status, 'BUSY');
  assert(!useStore.getState().toasts.some(row => row.level === 'success')); assert.deepEqual(useStore.getState().packages[0].declaration, pkg.declaration);
});
test('unknown transport outcome is never retried and a concurrent click cannot execute a second mutation', async () => {
  await useStore.getState().loadPlugins();
  let reject; window.pi.plugins.managePackages = args => { writes.push(args); return new Promise((_, failure) => { reject = failure; }); };
  const first = useStore.getState().installPackage('npm:fixture', 'user');
  assert.equal(await useStore.getState().installPackage('npm:fixture', 'user'), false);
  reject(new Error('synthetic timeout')); assert.equal(await first, false);
  assert.equal(writes.length, 1); assert.equal(useStore.getState().packageResult.code, 'PACKAGE_OUTCOME_UNKNOWN');
});
test('late project A list cannot replace current B and no session is created during either query', async () => {
  let resolve; window.pi.plugins.getPackages = args => args.cwd === '/A' ? new Promise(done => { resolve = () => done(receipt({ packages: [{ ...pkg, source: 'A' }] })); }) : Promise.resolve(receipt({ packages: [{ ...pkg, source: 'B' }] }));
  useStore.setState({ activeProjectCwd: '/A' }); const a = useStore.getState().loadPlugins();
  useStore.setState({ activeProjectCwd: '/B' }); await useStore.getState().loadPlugins(); resolve(); await a;
  assert.equal(useStore.getState().packages[0].source, 'B');
});


test('failed receipt without revision requires explicit refresh and cannot overwrite concurrent native declarations', async () => {
  await useStore.getState().loadPlugins();
  window.pi.plugins.managePackages = async args => { writes.push(args); return { schema: 'BioMindPluginsV1', status: 'error', code: 'PLUGIN_SETTINGS_CHANGED', changed: false, reloadRequired: false, repairRequired: false }; };
  assert.equal(await useStore.getState().configurePackage(pkg, pkg.declaration), false);
  assert.equal(writes[0].expectedRevision, 'fixture-rev');
  assert.equal(await useStore.getState().configurePackage(pkg, pkg.source), false); assert.equal(writes.length, 1);
  await useStore.getState().loadPlugins();
  await useStore.getState().configurePackage(pkg, pkg.declaration); assert.equal(writes.length, 2); assert.equal(writes[1].expectedRevision, 'fixture-rev');
});
