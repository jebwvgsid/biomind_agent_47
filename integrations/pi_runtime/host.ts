/** A transport/UI host around Pi. Contains no model/tool continuation loop. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { parseArgs } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
import { StringDecoder } from 'node:string_decoder';
import { resolveHandshakeTimeoutMs } from './domain-client.js';
import { assertObservationMode, observationMode, runtimeObservationLabel, projectObservedUsage } from './usage-observer.js';
import { filterNativeContextFiles } from './native-context.js';
import { openProductSession, describeProductSession, validateSessionSelection, ProductSessionError, type NativeSessionSelection } from './native-session.js';
import { selectConfiguredModel, validateModelSelection, type ModelSelection } from './model-config.js';
import {
  createAgentSession, createEventBus, DefaultResourceLoader, getAgentDir, ModelRuntime,
  SessionManager, SettingsManager,
} from '@earendil-works/pi-coding-agent';

const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const extensionPath = path.join(runtime, 'dist/extensions/biomind.js');
type Json = Record<string, any>;
interface Config extends NativeSessionSelection {
  python?: string; agentDir?: string; dataRoot?: string; installRoot?: string;
  handshakeTimeoutMs?: number;
  provider?: string; model?: string; thinkingLevel?: 'off' | 'minimal' | 'low' | 'medium' | 'high';
  providerFixture?: string; registryPath?: string; preferencesPath?: string;
  memoryRoot?: string; wikiRoot?: string; historyEnabled?: boolean;
  archivePath?: string; memoryMode?: 'ask' | 'auto' | 'manual' | 'disabled';
  modelSelection?: ModelSelection;
}
function readConfig(filename?: string): Config {
  if (!filename) return {};
  const value = JSON.parse(fs.readFileSync(path.resolve(filename), 'utf8'));
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Runtime settings must be a JSON object');
  const allowed = new Set(['python','agentDir','dataRoot','installRoot','provider','model','thinkingLevel',
    'providerFixture','registryPath','preferencesPath','memoryRoot','wikiRoot','historyEnabled','archivePath','memoryMode','sessionMode','session','continueSession','handshakeTimeoutMs']);
  for (const key of Object.keys(value)) if (!allowed.has(key)) throw new Error('Unknown runtime setting; credentials do not belong in this file');
  if (value.historyEnabled !== undefined && typeof value.historyEnabled !== 'boolean') throw new Error('historyEnabled must be boolean');
  if (value.memoryMode !== undefined && !['ask','auto','manual','disabled'].includes(value.memoryMode)) throw new Error('Invalid memoryMode');
  if (value.handshakeTimeoutMs !== undefined) {
    if (typeof value.handshakeTimeoutMs !== 'number') throw new Error('handshakeTimeoutMs must be a number');
    resolveHandshakeTimeoutMs(value.handshakeTimeoutMs);
  }
  validateSessionSelection(value);
  return value;
}
function locatePackage(name: string): string {
  let folder = path.dirname(fileURLToPath(import.meta.resolve(name)));
  while (folder !== path.dirname(folder)) {
    const candidate = path.join(folder, 'package.json');
    if (fs.existsSync(candidate)) {
      const value = JSON.parse(fs.readFileSync(candidate, 'utf8'));
      if (value.name === name) {
        if (value.version !== '0.87.1') throw new Error(`Pinned package version mismatch: ${name}`);
        return folder;
      }
    }
    folder = path.dirname(folder);
  }
  throw new Error(`Package manifest not found: ${name}`);
}
function configureDomain(config: Config, cwd: string): void {
  const installRoot = path.resolve(config.installRoot ?? path.join(runtime, '..', '..'));
  const python = config.python ?? process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
  if (!path.isAbsolute(python) || !fs.existsSync(python)) throw new Error('Configured BioMind Python interpreter unavailable');
  for (const [key, value] of Object.entries({ BIOMIND_PI_PYTHON: python,
    BIOMIND_PI_HANDSHAKE_TIMEOUT_MS: String(resolveHandshakeTimeoutMs(config.handshakeTimeoutMs)),
    BIOMIND_PI_INSTALL_ROOT: installRoot, BIOMIND_PI_PROJECT_ROOT: cwd,
    BIOMIND_PI_DATA_ROOT: path.resolve(config.dataRoot ?? path.join(cwd, '.biomind/pi-domain')),
    BIOMIND_PI_REGISTRY_PATH: config.registryPath ?? process.env.BIOMIND_PI_REGISTRY_PATH ??
      (process.env.BIO_SOFTWARE_ROOT ? path.join(process.env.BIO_SOFTWARE_ROOT, 'software_registry.json') : undefined),
    BIOMIND_PI_PREFERENCES_PATH: config.preferencesPath ?? process.env.BIOMIND_PI_PREFERENCES_PATH ?? process.env.BIOMIND_USER_PREFERENCES,
    BIOMIND_PI_MEMORY_ROOT: config.memoryRoot, BIOMIND_PI_WIKI_ROOT: config.wikiRoot,
    BIOMIND_PI_ARCHIVE_PATH: config.archivePath, BIOMIND_PI_MEMORY_MODE: config.memoryMode,
    BIOMIND_PI_HISTORY_ENABLED: config.historyEnabled !== undefined ? (config.historyEnabled ? '1' : '0')
      : process.env.BIOMIND_PI_HISTORY_ENABLED === '1' ? '1' : '0' })) {
    if (value !== undefined) process.env[key] = value;
  }
}
async function startNative(config: Config, cwd: string, prompt?: string): Promise<number> {
  if (!config.providerFixture && (!config.provider || !config.model)) throw new Error('EXPLICIT_ACTIVE_PRODUCT_MODEL_REQUIRED');
  if (config.modelSelection) {
    const credentials = { async read() { return undefined; }, async list() { return []; },
      async modify() { throw new Error('Credential writes are unavailable'); }, async delete() { throw new Error('Credential writes are unavailable'); } };
    const catalog = await ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
    selectConfiguredModel(catalog, config.modelSelection);
  }
  const packageRoot = locatePackage('@earendil-works/pi-coding-agent');
  const agentDir = path.resolve(config.agentDir ??
    (config.providerFixture ? path.join(cwd, '.biomind/pi-agent') : getAgentDir()));
  fs.mkdirSync(agentDir, { recursive: true });
  // Presentation uses the public extension UI. Preserve the user's quietStartup setting.
  const args = [path.join(packageRoot, 'dist/bundle/cli.js'),
    '--no-extensions', '--extension', extensionPath, '--skill', process.env.BIOMIND_PI_SKILLS_ROOT ?? path.join(process.env.BIOMIND_PI_INSTALL_ROOT!, 'skills'),
    '--no-prompt-templates', '--no-themes', '--no-builtin-tools', '--tui-mode', 'fullscreen'];
  if (config.providerFixture) {
    args.push('--offline', '--extension', path.join(runtime, 'dist/testing/native-fixture.js'), '--provider', 'biomind-fixture', '--model', 'scripted');
  } else {
    if (config.provider) args.push('--provider', config.provider);
    if (config.model) args.push('--model', config.model);
  }
  if (config.sessionMode === 'temporary') args.push('--no-session');
  if (config.session) args.push('--session', config.session);
  if (config.continueSession) args.push('--continue');
  if (config.thinkingLevel) args.push('--thinking', config.thinkingLevel);
  if (prompt !== undefined) args.push('--', prompt);
  const env = { ...process.env, PI_CODING_AGENT_DIR: agentDir, BIOMIND_PI_UI_COMMANDS: '1',
    ...(config.modelSelection ? { BIOMIND_PI_MODEL_SELECTION: JSON.stringify(config.modelSelection) } : {}),
    ...(config.providerFixture ? { PI_OFFLINE: '1', BIOMIND_PI_PROVIDER_FIXTURE: path.resolve(config.providerFixture) } : {}) };
  const child = spawn(process.execPath, args, { cwd, env, stdio: 'inherit' });
  return await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', (code, signal) => resolve(code ?? (signal ? 130 : 1))); });
}

async function startJson(config: Config, cwd: string, initialPrompt?: string): Promise<number> {
  // Native-only presentation commands must not consume JSON prompt text.
  delete process.env.BIOMIND_PI_UI_COMMANDS;
  const agentDir = path.resolve(config.agentDir ??
    (config.providerFixture ? path.join(cwd, '.biomind/pi-agent') : getAgentDir()));
  try { fs.mkdirSync(agentDir, { recursive: true }); }
  catch { throw new ProductSessionError('SESSION_AGENT_DIRECTORY_UNAVAILABLE'); }
  // SessionManager's public defaults use the same native agent directory as auth/settings.
  process.env.PI_CODING_AGENT_DIR = agentDir;
  const manager = openProductSession(cwd, config);
  const settings = SettingsManager.inMemory({ retry: { enabled: true }, compaction: { enabled: true },
    cacheWarming: 'off', enableAnalytics: false, enableInstallTelemetry: false });
  // Match native Pi's public HTTP setup, including the existing proxy routing.
  const dispatcher = await import(pathToFileURL(path.join(locatePackage('@earendil-works/pi-coding-agent'), 'dist/core/http-dispatcher.js')).href);
  dispatcher.configureHttpDispatcher(settings.getHttpIdleTimeoutMs());
  // Headless input cannot answer Pi's project trust dialog. Keep project resources
  // untrusted; explicit BioMind skills and native user skills remain available.
  settings.setProjectTrusted(false);
  // Let native resource settings own user paths/selection, without persisting
  // the JSON host's runtime-only retry/compaction overrides to user settings.
  const resourceSettings = SettingsManager.create(cwd, agentDir, { projectTrusted: false });
  const events = createEventBus();
  const resources = new DefaultResourceLoader({ cwd, agentDir, settingsManager: resourceSettings, eventBus: events,
    noExtensions: true, additionalExtensionPaths: [extensionPath],
    additionalSkillPaths: [process.env.BIOMIND_PI_SKILLS_ROOT ?? path.join(process.env.BIOMIND_PI_INSTALL_ROOT!, 'skills')], noPromptTemplates: true,
    noThemes: true, agentsFilesOverride: ({ agentsFiles }) => ({ agentsFiles: filterNativeContextFiles(agentsFiles, cwd, agentDir) }) });
  await resources.reload();
  if (resources.getExtensions().errors.length) throw new Error('BioMind extension failed to load');
  if (!resources.getExtensions().extensions.length) throw new Error('BioMind extension was not loaded');
  // Use Pi's same saved credential store as native mode. Synthetic fixtures
  // retain an empty in-memory store and never open the user's auth.json.
  const credentials = config.providerFixture ? {
    async read() { return undefined; }, async list() { return []; },
    async modify() { throw new Error('Credential writes are unavailable'); },
    async delete() { throw new Error('Credential writes are unavailable'); },
  } : undefined;
  const modelRuntime = await ModelRuntime.create({ ...(credentials ? { credentials } : { authPath: path.join(agentDir, 'auth.json') }),
    modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  let provider = config.provider, modelId = config.model;
  if (config.providerFixture) {
    const fixture = await import('./testing/scripted-provider.js');
    modelRuntime.registerProvider('biomind-fixture', fixture.scriptedProvider(config.providerFixture));
    provider = 'biomind-fixture'; modelId = 'scripted';
  }
  if (!provider || !modelId) throw new Error('Headless mode requires explicit existing provider/model or a synthetic providerFixture');
  const model = config.modelSelection ? selectConfiguredModel(modelRuntime, config.modelSelection) : modelRuntime.getModel(provider, modelId);
  if (!model) throw new Error('Requested model is unavailable in this pinned runtime');
  const { session } = await createAgentSession({ cwd, agentDir, modelRuntime, model,
    thinkingLevel: config.thinkingLevel ?? 'off', resourceLoader: resources, settingsManager: settings,
    sessionManager: manager,
    tools: [...new Set(resources.getExtensions().extensions.flatMap(extension => [...extension.tools.keys()]))] });
  session.agent.toolExecution = 'sequential';
  let writeTail = Promise.resolve(); let detached = false; let active: Promise<void> | undefined;
  let exitCode = 0; let usageAccountingFailed = false;
  let activeInputId: string | undefined; let outcome: string = 'unknown';
  const executionFacts: Json[] = [];
  const identity = { extension_sha256: createHash('sha256').update(fs.readFileSync(extensionPath)).digest('hex'),
    sessionId: session.sessionManager.getSessionId() };
  const sessionIdentity = () => ({ ...identity, ...describeProductSession(manager) });
  const waiting = new Map<string, Json>();
  const write = (value: unknown) => {
    if (detached) return;
    writeTail = writeTail.then(() => new Promise<void>((resolve, reject) => {
      process.stdout.write(JSON.stringify(value) + '\n', error => error ? reject(error) : resolve());
    })).catch(() => {
      detached = true; exitCode = 2;
      process.stderr.write('BioMind Pi output channel failed; request state is unavailable to this client.\n');
      void session.abort();
    });
  };
  events.on('biomind:ui_request', (raw: unknown) => {
    const request = raw as Json; waiting.set(request.id, request);
    write({ type: request.kind, ...request, requestId: activeInputId,
      businessRequestId: request.requestId, type_alias: 'ui_request', payload: request.proposal });
  });
  events.on('biomind:state', (data: unknown) => write({ type: 'biomind_state', data, inputId: activeInputId }));
  events.on('biomind:settings_result', (raw: unknown) => write({ type: 'settings_result', ...(raw as Json) }));
  events.on('biomind:job_result', (raw: unknown) => write({ type: 'job_result', ...(raw as Json) }));
  events.on('biomind:archive', (raw: unknown) => write({ type: 'archive_receipt', ...(raw as Json) }));
  events.on('biomind:diagnostic', (raw: unknown) => {
    const code = String((raw as Json).code ?? 'EXTENSION_DIAGNOSTIC');
    const diagnostic = raw as Json;
    write({ type: 'diagnostic', code,
      ...(diagnostic.reason === 'DOMAIN_HANDSHAKE_TIMEOUT' && Number.isInteger(diagnostic.timeoutMs)
        ? { reason: 'DOMAIN_HANDSHAKE_TIMEOUT', timeoutMs: diagnostic.timeoutMs } : {}) });
    if (['REQUEST_INITIALIZATION_FAILED', 'CONTEXT_POLICY_UNAVAILABLE', 'BIOMIND_USAGE_OBSERVER_FAILED'].includes(code)) {
      outcome = 'error'; exitCode = 2;
      if (code === 'BIOMIND_USAGE_OBSERVER_FAILED') usageAccountingFailed = true;
    }
  });
  events.on('biomind:settled', (raw: unknown) => { outcome = (raw as Json).outcome; write({ type: 'biomind_settled', data: raw }); });
  session.subscribe(event => {
    write({ type: 'event', event });
    if (event.type === 'tool_execution_end') {
      const details = (event.result as Json)?.details;
      if (details?.execution_facts && Object.keys(details.execution_facts).length) executionFacts.push(details.execution_facts);
    }
  });
  await session.bindExtensions({ mode: 'json', onError: error => {
    write({ type: 'extension_error', event: error });
  } });
  write({ type: 'ready', engine: 'pi', version: runtimeObservationLabel(), cwd,
    extension: extensionPath, ...(config.modelSelection ? { model_selection: config.modelSelection } : {}), ...sessionIdentity(),
    capabilities: { sessionResume: 'startup_exact_id_or_file', continueSession: 'explicit_same_project', sessionSwitch: 'restart_with_explicit_session',
      temporarySession: 'explicit', persistenceOwner: 'Pi SessionManager', jobOwnerTakeover: 'not_implied_by_session_resume' } });
  const control = (operation: Promise<unknown>, id?: unknown) => {
    void operation.catch(() => write({ type: 'error', id, code: 'PI_CONTROL_FAILED' }));
  };
  const handle = (frame: Json) => {
    if (frame.type === 'prompt') {
      if (active) { write({ type: 'error', id: frame.id, code: 'REQUEST_ACTIVE_USE_STEER_OR_FOLLOW_UP' }); return; }
      if (typeof frame.text !== 'string') { write({ type: 'error', id: frame.id, code: 'TEXT_REQUIRED' }); return; }
      activeInputId = String(frame.requestId ?? frame.taskId ?? frame.id ?? randomUUID());
      const inputId = activeInputId; outcome = 'unknown'; executionFacts.splice(0);
      active = (async () => {
        try {
          await session.prompt(frame.text); await session.waitForIdle();
          write({ type: 'result', ...sessionIdentity(), requestId: inputId, runtime_status: usageAccountingFailed ? 'error' : outcome,
            model_conclusion: session.getLastAssistantText() ?? '', execution_facts: [...executionFacts], scientific_success: null,
            usage_scope: projectObservedUsage(session.sessionManager.getEntries()).scope, usage: projectObservedUsage(session.sessionManager.getEntries()), pi_entry_usage: session.getSessionStats() });
        } catch {
          write({ type: 'error', requestId: inputId, code: 'PI_RUN_FAILED' });
          write({ type: 'result', ...sessionIdentity(), requestId: inputId, runtime_status: 'error', model_conclusion: '', execution_facts: [...executionFacts], scientific_success: null,
            usage_scope: projectObservedUsage(session.sessionManager.getEntries()).scope, usage: projectObservedUsage(session.sessionManager.getEntries()), pi_entry_usage: session.getSessionStats() });
        } finally { waiting.clear(); active = undefined; activeInputId = undefined; }
      })();
    } else if (frame.type === 'ui_response') {
      const request = waiting.get(String(frame.id));
      if (!request) { write({ type: 'error', id: frame.id, code: 'STALE_UI_RESPONSE' }); return; }
      const value = frame.value;
      const answer = typeof value === 'string' ? value : value?.answer;
      const rawChoice = typeof value === 'string' ? value : value?.choice;
      const choice = rawChoice === 'deny' ? 'n' : rawChoice === 'approve' ? 'y' : rawChoice;
      const response = { ...(typeof value === 'object' && value ? value : {}), id: request.id,
        requestId: request.requestId, sessionId: request.sessionId,
        ...(request.kind === 'question' ? { answer } : { choice }) };
      waiting.delete(request.id); events.emit('biomind:ui_response', response);
    } else if (frame.type === 'job') {
      if (!['list','status','logs','cancel'].includes(frame.action) ||
          (frame.action !== 'list' && typeof frame.run_ref !== 'string')) {
        write({ type: 'job_result', id: frame.id, status: 'error', code: 'INVALID_JOB_COMMAND' }); return;
      }
      events.emit('biomind:job_request', { id: frame.id ?? randomUUID(), action: frame.action,
        ...(frame.run_ref ? { run_ref: frame.run_ref } : {}),
        ...(typeof frame.after_cursor === 'string' ? { after_cursor: frame.after_cursor } : {}) });
    } else if (frame.type === 'settings') {
      if ((frame.history_enabled !== undefined && typeof frame.history_enabled !== 'boolean') ||
          (frame.memory_mode !== undefined && !['ask','auto','manual','disabled'].includes(frame.memory_mode))) {
        write({ type: 'settings_result', id: frame.id, status: 'error', code: 'INVALID_SETTINGS' }); return;
      }
      events.emit('biomind:settings_request', { id: frame.id ?? randomUUID(),
        ...(frame.history_enabled !== undefined ? { history_enabled: frame.history_enabled } : {}),
        ...(frame.memory_mode !== undefined ? { memory_mode: frame.memory_mode } : {}) });
    } else if (frame.type === 'abort') { waiting.clear(); control(session.abort(), frame.id); }
    else if (frame.type === 'steer' && typeof frame.text === 'string') { control(session.steer(frame.text), frame.id); }
    else if (frame.type === 'follow_up' && typeof frame.text === 'string') { control(session.followUp(frame.text), frame.id); }
    else if (frame.type === 'shutdown') { detached = true; control(session.abort(), frame.id); }
    else write({ type: 'error', id: frame.id, code: 'UNKNOWN_COMMAND' });
  };
  const decoder = new StringDecoder('utf8'); let pending = '';
  try {
    if (initialPrompt !== undefined) handle({ type: 'prompt', text: initialPrompt, id: 'initial' });
    for await (const chunk of process.stdin) {
      pending += decoder.write(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      if (Buffer.byteLength(pending) > 4 * 1024 * 1024) throw new Error('HOST_FRAME_LIMIT');
      let index: number;
      while ((index = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, index); pending = pending.slice(index + 1);
        try { const frame = JSON.parse(line); if (!frame || typeof frame !== 'object') throw new Error(); handle(frame); }
        catch { write({ type: 'error', code: 'INVALID_JSON_FRAME' }); exitCode = 2; detached = true; }
        if (detached) break;
      }
      if (detached) break;
    }
    if (!detached && (pending + decoder.end()).trim()) {
      write({ type: 'error', code: 'TRUNCATED_JSON_FRAME' }); exitCode = 2;
    }
  } finally {
    // EOF is disconnect, not a cancellation of a scientific job. Pi abort only
    // cancels conversation waiters; the extension closes the bridge disconnect-only.
    detached = true; await session.abort(); await active;
    await session.extensionRunner?.emit({ type: 'session_shutdown', reason: 'quit' });
    session.dispose(); await writeTail;
  }
  return exitCode;
}

export async function main(): Promise<number> {
  const { values } = parseArgs({ options: { mode: { type: 'string', default: 'interactive' },
    'project-dir': { type: 'string', default: process.cwd() }, config: { type: 'string' },
    'model-selection': { type: 'string' }, 'registry-path': { type: 'string' }, prompt: { type: 'string' },
    session: { type: 'string' }, continue: { type: 'boolean' }, 'no-session': { type: 'boolean' } } });
  const cwd = path.resolve(values['project-dir']!);
  if (!fs.statSync(cwd).isDirectory()) throw new Error('Project cwd is not a directory');
  locatePackage('@earendil-works/pi-coding-agent'); locatePackage('@earendil-works/pi-ai'); locatePackage('@earendil-works/pi-agent-core');
  // Verify the actual SDK and published CLI bundle bytes before any session can send.
  // Extension-load errors alone are not fail-closed in upstream Pi.
  assertObservationMode();
  const compat = JSON.parse(fs.readFileSync(path.join(runtime, 'compat/pi-0.87.1-observer.patch.json'), 'utf8'));
  const packageRoot = locatePackage('@earendil-works/pi-coding-agent');
  for (const target of observationMode() === 'compat' ? compat.files : []) {
    const actual = createHash('sha256').update(fs.readFileSync(path.join(packageRoot, target.path))).digest('hex');
    if (actual !== target.patchedSha256) throw new Error('BIOMIND_PI_COMPAT_PATCH_REQUIRED');
  }
  const config = readConfig(values.config);
  if (values.session !== undefined) config.session = values.session;
  if (values.continue !== undefined) config.continueSession = values.continue;
  if (values['no-session']) config.sessionMode = 'temporary';
  validateSessionSelection(config);
  if (values['registry-path'] && !config.registryPath) config.registryPath = values['registry-path'];
  if (values['model-selection']) {
    const selection = validateModelSelection(JSON.parse(values['model-selection']));
    if (config.providerFixture || (config.provider && config.provider !== selection.provider) ||
        (config.model && config.model !== selection.model) || (config.thinkingLevel && config.thinkingLevel !== selection.thinking)) {
      throw new Error('CONFLICTING_PRODUCT_MODEL_CONFIGURATION');
    }
    Object.assign(config, { provider: selection.provider, model: selection.model, thinkingLevel: selection.thinking, modelSelection: selection });
  }
  configureDomain(config, cwd);
  if (values.mode === 'interactive') return await startNative(config, cwd, values.prompt);
  if (values.mode === 'json') return await startJson(config, cwd, values.prompt);
  throw new Error('Unsupported mode');
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await main(); } catch (error) {
    // Never print unfiltered settings/provider exceptions (they can contain URLs
    // with credentials). Full typed fixture assertions live in test evidence.
    if (error instanceof ProductSessionError) {
      process.stderr.write(`BioMind Pi session error: ${error.code}. No temporary fallback was used.\n`);
      if (process.argv.includes('json')) process.stdout.write(JSON.stringify({ type: 'error', code: error.code }) + '\n');
    }
    process.stderr.write('BioMind Pi host failed to initialize or run; no legacy fallback was attempted.\n');
    process.exitCode = 2;
  }
}
