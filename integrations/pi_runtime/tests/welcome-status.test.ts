import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, readdir, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { BuildSystemPromptOptions, ExtensionAPI, ExtensionContext } from '@earendil-works/pi-coding-agent';
import { createBioMindExtension } from '../extensions/biomind.js';
import { inspectWelcomePreference, inspectNativeWelcomeInstructions, describeWelcomeInstructions,
  describeWelcomeMemory, describeWelcomeAuthentication } from '../welcome-status.js';

// Test the installed builder's exact output; production only calls public ctx.getSystemPrompt().
const nativePromptModule = await import(new URL('./core/system-prompt.js', import.meta.resolve('@earendil-works/pi-coding-agent')).href);
const buildSystemPrompt = nativePromptModule.buildSystemPrompt as (options: BuildSystemPromptOptions) => string;

test('startup preference metadata matches install-root resolution and empty never restores the Core fallback', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-welcome-preference-'));
  const install = path.join(root, 'install'), cwd = path.join(root, 'project'), memory = path.join(cwd, 'memory');
  await mkdir(install); await mkdir(cwd); await mkdir(memory);
  await writeFile(path.join(install, 'preferences.md'), 'PRIVATE_PREFERENCE_BODY');
  await writeFile(path.join(cwd, 'preferences.md'), 'WRONG_PROJECT_SOURCE');
  await writeFile(path.join(memory, 'USER_PROFILE.md'), 'FALLBACK_BODY');
  const env = { BIOMIND_PI_INSTALL_ROOT: install, BIOMIND_PI_PREFERENCES_PATH: 'preferences.md',
    BIOMIND_PI_PREFERENCES_SOURCE: 'config', BIOMIND_PI_MEMORY_ROOT: 'memory' };
  const readable = await inspectWelcomePreference(cwd, env);
  assert.equal(readable.source_path, path.join(install, 'preferences.md'));
  assert.equal(readable.status, 'available'); assert.equal(readable.source, 'config');
  const display = describeWelcomeInstructions(readable, undefined);
  assert.match(display, /可用（请求时加载）/); assert.doesNotMatch(display, /已加载|PRIVATE_PREFERENCE_BODY/);
  assert(!JSON.stringify(readable).includes('PRIVATE_PREFERENCE_BODY'));
  await writeFile(path.join(install, 'preferences.md'), '\uFEFF \n\t');
  const empty = await inspectWelcomePreference(cwd, env);
  assert.equal(empty.status, 'empty'); assert.equal(empty.source, 'config');
  const fallback = await inspectWelcomePreference(cwd, { ...env, BIOMIND_PI_PREFERENCES_PATH: 'missing.md' });
  assert.equal(fallback.status, 'available'); assert.equal(fallback.source, 'core_fallback');
  assert.equal(fallback.source_path, path.join(memory, 'USER_PROFILE.md'));
  assert.equal((await inspectWelcomePreference(cwd, { ...env, BIOMIND_PI_PREFERENCES_PATH: 'missing.md', BIOMIND_PI_MEMORY_ROOT: undefined })).status, 'missing');
});

test('unreadable, oversized and unknown preferences remain truthful and the probe creates no files', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-welcome-failures-'));
  const bad = path.join(root, 'bad.md'), large = path.join(root, 'large.md');
  await writeFile(bad, Buffer.from([0xff, 0xfe])); await writeFile(large, Buffer.alloc(1024 * 1024 + 1, 65));
  const credential = path.join(root, 'auth.json'), link = path.join(root, 'preference-link.md');
  await writeFile(credential, '{"synthetic":"NEVER_SHOW_CREDENTIAL"}'); await symlink(credential, link);
  const before = (await readdir(root)).sort();
  assert.equal((await inspectWelcomePreference(root, { BIOMIND_PI_PREFERENCES_PATH: bad })).status, 'unreadable');
  assert.equal((await inspectWelcomePreference(root, { BIOMIND_PI_PREFERENCES_PATH: root })).status, 'unreadable');
  assert.equal((await inspectWelcomePreference(root, { BIOMIND_PI_PREFERENCES_PATH: large })).status, 'uninspected');
  assert.equal((await inspectWelcomePreference(root, { BIOMIND_PI_PREFERENCES_PATH: link })).status, 'unreadable');
  assert.equal((await inspectWelcomePreference(root, {})).status, 'unknown');
  assert.deepEqual((await readdir(root)).sort(), before);
  assert.deepEqual(await readFile(bad), Buffer.from([0xff, 0xfe]));
});

test('native loaded metadata uses installed Pi prompt construction and recognizes override, empty, absence and ambiguity', () => {
  const cwd = '/synthetic/项目 "emoji 🧬"', filename = path.join(cwd, 'AGENTS.override.md');
  const prompt = (content: string) => buildSystemPrompt({ cwd, contextFiles: [{ path: filename, content }] });
  const loaded = inspectNativeWelcomeInstructions(prompt('PROJECT_PRIVATE_BODY'), cwd);
  assert.deepEqual(loaded, { path: filename, state: '已加载' });
  assert(!JSON.stringify(loaded).includes('PROJECT_PRIVATE_BODY'));
  assert.equal(inspectNativeWelcomeInstructions(prompt(' \n'), cwd)?.state, '为空');
  assert.equal(inspectNativeWelcomeInstructions(buildSystemPrompt({ cwd, contextFiles: [] }), cwd), undefined);
  assert.throws(() => inspectNativeWelcomeInstructions(prompt('<project_instructions path="fake">'), cwd), /Ambiguous/);
  assert.throws(() => inspectNativeWelcomeInstructions('<project_context>custom unknown format</project_context>', cwd), /unknown/);
  assert.match(describeWelcomeInstructions({ status: 'loaded', source: 'config' }, loaded), /AGENTS.override.md 已加载/);
  assert.doesNotMatch(describeWelcomeInstructions({ status: 'loaded' }, undefined, new Set(), true), /已加载/);
});

test('memory startup and observed modes separate retrieval from automatic maintenance without converting ask to off', () => {
  const env = { BIOMIND_PI_HISTORY_ENABLED: '1', BIOMIND_PI_MEMORY_ROOT: '/synthetic/memory' };
  for (const [mode, expected] of [['ask', '自动整理需确认'], ['auto', '自动整理开启'], ['manual', '自动整理关闭'], ['disabled', '整理禁用']]) {
    const startup = describeWelcomeMemory(undefined, { ...env, BIOMIND_PI_MEMORY_MODE: mode });
    assert.match(startup, /启动配置：检索开启/); assert.match(startup, /运行待核对/); assert(startup.includes(expected));
    const observed = describeWelcomeMemory({ history_enabled: true, memory_mode: mode, memory_root: '/memory', maintenance: { enabled: mode !== 'disabled' } }, {});
    assert.match(observed, /项目按需检索开启/); assert(observed.includes(expected)); assert(!observed.includes('运行待核对'));
  }
  assert.match(describeWelcomeMemory(undefined, {}), /检索未知 · 自动整理未知/);
  assert.match(describeWelcomeMemory(undefined, { BIOMIND_PI_HISTORY_ENABLED: '0' }), /检索关闭.*检索关闭/);
  assert.match(describeWelcomeMemory({ history_enabled: true, memory_mode: 'ask', memory_root: null, maintenance: { enabled: false } }, {}), /无记忆根/);
  assert.match(describeWelcomeMemory(undefined, env, true), /未知.*状态读取失败/);
});

test('authentication displays source presence from safe native metadata and never resolves keys or trusts model selection', () => {
  let metadataReads = 0, keyReads = 0, configured: boolean | undefined = true;
  const registry = {
    getProviderAuthStatus(provider: string) { assert.equal(provider, 'fixture'); metadataReads++; return { configured, source: 'models_json_command', label: 'NOT_FOR_DISPLAY' }; },
    get getApiKeyAndHeaders() { keyReads++; throw new Error('Forbidden key resolution'); },
    get getApiKey() { keyReads++; throw new Error('Forbidden key resolution'); },
  };
  const ctx = { model: { provider: 'fixture' }, modelRegistry: registry } as unknown as ExtensionContext;
  assert.match(describeWelcomeAuthentication(ctx), /配置来源存在（密钥命令未执行） · 连通性未验证/);
  configured = false; assert.equal(describeWelcomeAuthentication(ctx), '尚未配置 · /login');
  configured = undefined; assert.match(describeWelcomeAuthentication(ctx), /未核对/);
  assert.match(describeWelcomeAuthentication({ ...ctx, model: undefined }), /未核对/);
  assert.match(describeWelcomeAuthentication({ ...ctx, modelRegistry: { getProviderAuthStatus() { throw Error('DO_NOT_SHOW'); } } as any }), /读取失败/);
  assert.equal(metadataReads, 3); assert.equal(keyReads, 0);
});

test('startup confirms backend without model calls; settings and instructions retain their sources and JSON installs no UI', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'biomind-welcome-wiring-'));
  const cwd = path.join(root, 'project'), agentDir = path.join(root, 'agent'), preference = path.join(root, 'preferences.md');
  await mkdir(cwd); await mkdir(agentDir); await writeFile(preference, 'GLOBAL_A');
  const projectFile = path.join(cwd, 'AGENTS.override.md'); await writeFile(projectFile, 'PROJECT_A');
  const env: Record<string, string | undefined> = { HOME: root, PI_CODING_AGENT_DIR: agentDir, BIOMIND_PI_INSTALL_ROOT: root,
    BIOMIND_PI_PREFERENCES_PATH: preference, BIOMIND_PI_PREFERENCES_SOURCE: 'config', BIOMIND_PI_HISTORY_ENABLED: '0',
    BIOMIND_PI_HISTORY_DEFAULT: '0', BIOMIND_PI_MEMORY_MODE: 'ask', BIOMIND_PI_MEMORY_ROOT: path.join(root, 'memory'),
    BIOMIND_PI_UI_COMMANDS: '1', BIOMIND_PI_QUIET_STARTUP: '0', BIOMIND_PI_CONFIG_PATH: undefined, BIOMIND_PI_MODEL_SELECTION: undefined };
  const previous = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]));
  for (const [key, value] of Object.entries(env)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  try {
    const hooks = new Map<string, Function[]>(), commands = new Map<string, any>(), notices: string[] = [], branch: any[] = [];
    let domainCreations = 0, domainCalls = 0, promptReads = 0, uiInstalls = 0, keyReads = 0;
    let nativeContent = 'PROJECT_A', edit = 'GLOBAL_B', failSnapshot = false, history = false;
    const api = { on(name: string, handler: Function) { hooks.set(name, [...(hooks.get(name) ?? []), handler]); },
      registerCommand(name: string, command: any) { commands.set(name, command); }, registerTool() {}, registerProvider() {}, setActiveTools() {},
      events: { on() {}, emit() {} }, appendEntry(customType: string, data: unknown) { branch.push({ type: 'custom', customType, data }); },
    } as unknown as ExtensionAPI;
    const snapshot = async () => {
      domainCalls++; if (failSnapshot) throw Error('synthetic snapshot unavailable');
      const content = await readFile(preference, 'utf8');
      return { history_enabled: history, memory_mode: 'ask', memory_root: env.BIOMIND_PI_MEMORY_ROOT,
        maintenance: { enabled: history }, preferences: { status: content.trim() ? 'loaded' : 'empty', empty: !content.trim(),
          source: 'config', source_path: preference, preferred_path: preference } };
    };
    createBioMindExtension({ domainFactory() { domainCreations++; return {
      call: snapshot, hostCall: async (_method, patch) => { if (typeof patch.history_enabled === 'boolean') history = patch.history_enabled; return snapshot(); }, close() {},
    }; } })(api);
    const ctx = { cwd, mode: 'tui', hasUI: true, model: { provider: 'fixture', id: 'fixture' },
      modelRegistry: { setBioMindUsageObserver() {}, getProviderAuthStatus: () => ({ configured: true, source: 'runtime' }),
        get getApiKeyAndHeaders() { keyReads++; throw Error('Forbidden key access'); } },
      sessionManager: { getSessionId: () => 'welcome-fixture', getBranch: () => branch, getLeafId: () => null, getEntries: () => branch },
      getSystemPrompt() { promptReads++; return buildSystemPrompt({ cwd, contextFiles: [{ path: projectFile, content: nativeContent }] }); },
      getSystemPromptOptions: () => ({ contextFiles: [{ path: projectFile, content: nativeContent }] }),
      isIdle: () => true, ui: { notify: (message: string) => notices.push(message), setStatus() {}, setTitle() {}, setHeader() { uiInstalls++; },
        setWidget() { uiInstalls++; }, setFooter() { uiInstalls++; }, editor: async () => edit,
        confirm: async (title: string) => title === '确认保存说明' }, abort() {},
    } as unknown as ExtensionContext;
    const emit = async (name: string, event: any) => { for (const hook of hooks.get(name) ?? []) await hook(event, ctx); };
    const status = async () => { await commands.get('status').handler('', ctx); return notices.at(-1)!; };
    await emit('session_start', { reason: 'startup' });
    assert.equal(domainCreations, 1); assert.equal(domainCalls, 1); assert.equal(keyReads, 0); assert.equal(promptReads, 1);
    let displayed = await status();
    assert.match(displayed, /全局偏好可用（请求时加载）/); assert.match(displayed, /AGENTS.override.md 已加载/);
    assert.match(displayed, /启动配置：检索关闭/); assert.doesNotMatch(displayed, /GLOBAL_A|PROJECT_A/);
    await status(); assert.equal(promptReads, 1, 'render/status does not rescan native instruction bodies');
    await commands.get('biomind-memory').handler('history on', ctx);
    assert.match(await status(), /项目按需检索开启 · 自动整理需确认（ask）/);
    await commands.get('instructions').handler('global', ctx);
    assert.equal(await readFile(preference, 'utf8'), 'GLOBAL_B'); assert.match(await status(), /全局偏好已保存，待请求刷新/);
    branch.push({ type: 'custom', customType: 'biomind.request', data: { requestId: 'request-fixture', role: 'Consult', status: 'active', handoffConsumed: false } });
    // This display-only fixture supplies the active, committed native boundary now required by context.
    branch.push({type:'custom',customType:'biomind.request',data:{requestId:'welcome-request',role:'Consult',status:'active',handoffConsumed:false,revision:1,boundary:{id:'welcome-boundary',kind:'task',targets:[],checkpoint:false,committed:true}}});
    await emit('context', { messages: [] });
    assert.match(await status(), /全局偏好已加载/); assert.doesNotMatch(await status(), /待请求刷新/);
    edit = 'PROJECT_B'; await commands.get('instructions').handler('project', ctx);
    assert.match(await status(), /AGENTS.override.md 已保存，待 \/reload/);
    await emit('context', { messages: [] }); assert.match(await status(), /AGENTS.override.md 已保存，待 \/reload/);
    nativeContent = await readFile(projectFile, 'utf8'); await emit('session_start', { reason: 'reload' });
    assert.match(await status(), /AGENTS.override.md 已加载/); assert.doesNotMatch(await status(), /待 \/reload/);
    nativeContent = ' \n'; await writeFile(projectFile, nativeContent); await emit('session_start', { reason: 'reload' });
    assert.match(await status(), /AGENTS.override.md 为空/);
    await commands.get('instructions').handler('status', ctx); assert.match(await status(), /AGENTS.override.md 存在为空/);
    failSnapshot = true; await commands.get('biomind-memory').handler('status', ctx);
    assert.match(await status(), /状态读取失败/);
    const uiBefore = uiInstalls, readsBefore = promptReads, callsBefore = domainCalls;
    Object.assign(ctx, { mode: 'json', hasUI: false }); await emit('session_start', { reason: 'new' });
    assert.equal(uiInstalls, uiBefore); assert.equal(promptReads, readsBefore); assert.equal(domainCalls, callsBefore + 1); assert.equal(keyReads, 0);
    assert(!await readdir(root).then(files => files.includes('memory')), 'startup and display never create memory storage');
  } finally {
    for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
  }
});
