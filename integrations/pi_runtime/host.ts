/** A transport/UI host around Pi. Contains no model/tool continuation loop. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { parseArgs } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
import { StringDecoder } from 'node:string_decoder';
import { resolveHandshakeTimeoutMs } from './domain-client.js';
import { assertObservationMode, observationMode, runtimeObservationLabel, projectObservedUsage } from './usage-observer.js';
import { validateSessionSelection, resolveSessionDir, ProductSessionError, TerminalInputError, type NativeSessionSelection } from './native-session-selection.js';
import { selectConfiguredModel, validateConfiguredCustomModel, validateModelSelection, type ModelSelection } from './model-config.js';
import { chooseExecutionProfile, sessionProfile, type ExecutionProfile } from './execution-profile.js';
import { validateProjectRoot, ProjectDirectoryError } from './project-launcher.js';


const runtime = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const extensionPath = path.join(runtime, 'dist/extensions/biomind.js');
const nativeExtensionPath = path.join(runtime, 'dist/biomind-extension.ts');
type Json = Record<string, any>;
interface Config extends NativeSessionSelection {
  python?: string; agentDir?: string; dataRoot?: string; installRoot?: string;
  handshakeTimeoutMs?: number;
  provider?: string; model?: string; thinkingLevel?: 'off' | 'minimal' | 'low' | 'medium' | 'high';
  providerFixture?: string; registryPath?: string; preferencesPath?: string;
  memoryRoot?: string; wikiRoot?: string; historyEnabled?: boolean;
  archivePath?: string; memoryMode?: 'ask' | 'auto' | 'manual' | 'disabled';
  modelSelection?: ModelSelection;
  configPath?: string;
  launcherPython?: string;
  executionProfile?: ExecutionProfile; profileSource?: string; migrateProfile?: boolean;
}
function readConfig(filename?: string): Config {
  if (!filename) return {};
  const value = JSON.parse(fs.readFileSync(path.resolve(filename), 'utf8'));
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Runtime settings must be a JSON object');
  const allowed = new Set(['python','agentDir','dataRoot','installRoot','provider','model','thinkingLevel',
    'providerFixture','registryPath','preferencesPath','memoryRoot','wikiRoot','historyEnabled','archivePath','memoryMode','sessionMode','session','continueSession','sessionDir','handshakeTimeoutMs','executionProfile']);
  for (const key of Object.keys(value)) if (!allowed.has(key)) throw new Error('Unknown runtime setting; credentials do not belong in this file');
  if (value.historyEnabled !== undefined && typeof value.historyEnabled !== 'boolean') throw new Error('historyEnabled must be boolean');
  if ((value.provider !== undefined || value.model !== undefined) &&
      (typeof value.provider !== 'string' || !value.provider.trim() || typeof value.model !== 'string' || !value.model.trim())) throw new Error('INCOMPLETE_PRODUCT_MODEL_CONFIGURATION');
  if (value.memoryMode !== undefined && !['ask','auto','manual','disabled'].includes(value.memoryMode)) throw new Error('Invalid memoryMode');
  if (value.handshakeTimeoutMs !== undefined) {
    if (typeof value.handshakeTimeoutMs !== 'number') throw new Error('handshakeTimeoutMs must be a number');
    resolveHandshakeTimeoutMs(value.handshakeTimeoutMs);
  }
  validateSessionSelection(value);
  return { ...value, configPath: path.resolve(filename) };
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
function configureDomain(config: Config, cwd: string, loadedExtension = extensionPath): void {
  const installRoot = path.resolve(config.installRoot ?? path.join(runtime, '..', '..'));
  const python = config.python ?? process.env.BIOMIND_PI_PYTHON ?? '/public/home/liusiqi/anaconda3/envs/biomind/bin/python';
  // The domain client validates Python when a domain operation is requested;
  // native login/model/instructions must remain reachable without scientific Python.
  for (const [key, value] of Object.entries({ BIOMIND_PI_PYTHON: python,
    BIOMIND_PI_HANDSHAKE_TIMEOUT_MS: String(resolveHandshakeTimeoutMs(config.handshakeTimeoutMs)),
    BIOMIND_PI_INSTALL_ROOT: installRoot, BIOMIND_PI_PROJECT_ROOT: cwd,
    BIOMIND_PI_EXECUTION_PROFILE: config.executionProfile,
    BIOMIND_PI_BUILD_ID: createHash('sha256').update(fs.readFileSync(loadedExtension)).digest('hex').slice(0,12),
    BIOMIND_PI_SESSION_EXISTING: config.session ? '1' : '0',
    BIOMIND_PI_PROFILE_SOURCE: config.profileSource,
    BIOMIND_PI_MIGRATE_PROFILE: config.migrateProfile ? '1' : '0',
    BIOMIND_PI_DATA_ROOT: path.resolve(cwd, config.dataRoot ?? '.biomind/pi-domain'),
    BIOMIND_PI_REGISTRY_PATH: config.registryPath ?? process.env.BIOMIND_PI_REGISTRY_PATH ??
      (process.env.BIO_SOFTWARE_ROOT ? path.join(process.env.BIO_SOFTWARE_ROOT, 'software_registry.json') : undefined),
    BIOMIND_PI_PREFERENCES_PATH: config.preferencesPath ?? process.env.BIOMIND_PI_PREFERENCES_PATH ?? process.env.BIOMIND_USER_PREFERENCES,
    BIOMIND_PI_MEMORY_ROOT: config.memoryRoot, BIOMIND_PI_WIKI_ROOT: config.wikiRoot,
    BIOMIND_PI_ARCHIVE_PATH: config.archivePath, BIOMIND_PI_MEMORY_MODE: config.memoryMode,
    BIOMIND_PI_CONFIG_PATH: config.configPath,
    BIOMIND_PI_PREFERENCES_SOURCE: config.preferencesPath !== undefined ? 'config' : (process.env.BIOMIND_PI_PREFERENCES_PATH || process.env.BIOMIND_USER_PREFERENCES) ? 'environment' : 'default',
    BIOMIND_PI_HISTORY_SOURCE: config.historyEnabled !== undefined ? 'config' : process.env.BIOMIND_PI_HISTORY_ENABLED !== undefined ? 'environment' : 'default',
    BIOMIND_PI_HISTORY_DEFAULT: config.historyEnabled !== undefined ? (config.historyEnabled ? '1' : '0') : process.env.BIOMIND_PI_HISTORY_ENABLED === '1' ? '1' : '0',
    BIOMIND_PI_HISTORY_ENABLED: config.historyEnabled !== undefined ? (config.historyEnabled ? '1' : '0')
      : process.env.BIOMIND_PI_HISTORY_ENABLED === '1' ? '1' : '0' })) {
    if (value !== undefined) process.env[key] = value;
    else delete process.env[key];
  }
}
async function startNative(config: Config, cwd: string, prompt?: string): Promise<number> {
  if (!config.providerFixture && !!config.provider !== !!config.model) throw new Error('INCOMPLETE_PRODUCT_MODEL_CONFIGURATION');
  if ((config.modelSelection && !validateConfiguredCustomModel(config.modelSelection)) ||
      (!config.modelSelection && config.provider && config.model && !config.providerFixture)) {
    const { ModelRuntime, getAgentDir } = await import('@earendil-works/pi-coding-agent');
    const credentials = { async read() { return undefined; }, async list() { return []; },
      async modify() { throw new Error('Credential writes are unavailable'); }, async delete() { throw new Error('Credential writes are unavailable'); } };
    const catalog = await ModelRuntime.create({ credentials,
      modelsPath: config.modelSelection ? null : path.join(config.agentDir ?? getAgentDir(), 'models.json'),
      allowModelNetwork: false, refreshOnCreate: false });
    if (config.modelSelection) selectConfiguredModel(catalog, config.modelSelection);
    else if (!catalog.getModel(config.provider!, config.model!)) throw new Error('REQUESTED_MODEL_UNAVAILABLE');
  }
  const packageRoot = locatePackage('@earendil-works/pi-coding-agent');
  // No parent-owned default: the official CLI resolves its native agent directory.
  const agentDir = config.agentDir;
  if (agentDir !== undefined) fs.mkdirSync(agentDir, { recursive: true });
  // Presentation uses the public extension UI. Preserve the user's quietStartup setting.
  const manifest = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));
  const bin = typeof manifest.bin === 'string' ? manifest.bin : manifest.bin?.pi;
  if (typeof bin !== 'string' || !bin) throw new Error('PI_OFFICIAL_BIN_UNAVAILABLE');
  const args = [path.resolve(packageRoot, bin),
    '--no-extensions', '--extension', nativeExtensionPath, '--skill', process.env.BIOMIND_PI_SKILLS_ROOT ?? path.join(process.env.BIOMIND_PI_INSTALL_ROOT!, 'skills'),
    '--no-prompt-templates', '--no-themes', ...(config.executionProfile === 'native' ? [] : ['--no-builtin-tools']), '--tui-mode', 'fullscreen'];
  if (config.providerFixture) {
    args.push('--offline', '--extension', path.join(runtime, 'dist/native-fixture.ts'), '--provider', 'biomind-fixture', '--model', 'scripted');
  } else {
    if (config.provider) args.push('--provider', config.provider);
    if (config.model) args.push('--model', config.model);
  }
  if (config.sessionMode === 'temporary') args.push('--no-session');
  if (config.sessionDir) args.push('--session-dir', config.sessionDir);
  if (config.session) args.push('--session', config.session);
  if (config.continueSession) args.push('--continue');
  if (config.thinkingLevel) args.push('--thinking', config.thinkingLevel);
  if (prompt !== undefined) args.push('--', prompt);
  const env = { ...process.env, ...(agentDir !== undefined ? { PI_CODING_AGENT_DIR: agentDir } : {}),
    BIOMIND_PI_UI_COMMANDS: '1', BIOMIND_PI_QUIET_STARTUP: 'native', PI_SKIP_VERSION_CHECK: '1',
    ...(config.modelSelection ? { BIOMIND_PI_MODEL_SELECTION: JSON.stringify(config.modelSelection) } : {}),
    ...(config.providerFixture ? { PI_OFFLINE: '1', BIOMIND_PI_PROVIDER_FIXTURE: path.resolve(config.providerFixture) } : {}) };
  const child = spawn(process.execPath, args, { cwd, env, stdio: 'inherit' });
  const code = await new Promise<number>((resolve, reject) => { child.once('error', reject); child.once('exit', (code, signal) => resolve(code ?? (signal ? 130 : 1))); });
  return code;
}

async function startJson(config: Config, cwd: string, initialPrompt?: string): Promise<number> {
  const { createAgentSession, createEventBus, DefaultResourceLoader, getAgentDir, ModelRuntime } =
    await import('@earendil-works/pi-coding-agent');
  const { openProductSession, describeProductSession, createProductSettings } = await import('./native-session.js');
  const { filterNativeContextFiles } = await import('./native-context.js');
  // Native-only presentation commands must not consume JSON prompt text.
  delete process.env.BIOMIND_PI_UI_COMMANDS;
  const agentDir = path.resolve(config.agentDir ??
    (config.providerFixture ? path.join(cwd, '.biomind/pi-agent') : getAgentDir()));
  try { fs.mkdirSync(agentDir, { recursive: true }); }
  catch { throw new ProductSessionError('SESSION_AGENT_DIRECTORY_UNAVAILABLE'); }
  // SessionManager's public defaults use the same native agent directory as auth/settings.
  process.env.PI_CODING_AGENT_DIR = agentDir;
  const settings = createProductSettings(cwd, agentDir);
  settings.applyOverrides({ cacheWarming: 'off', enableAnalytics: false, enableInstallTelemetry: false });
  const manager = openProductSession(cwd, config);
  // Match native Pi's public HTTP setup, including the existing proxy routing.
  const dispatcher = await import(pathToFileURL(path.join(locatePackage('@earendil-works/pi-coding-agent'), 'dist/core/http-dispatcher.js')).href);
  dispatcher.configureHttpDispatcher(settings.getHttpIdleTimeoutMs());
  const events = createEventBus();
  const resources = new DefaultResourceLoader({ cwd, agentDir, settingsManager: settings, eventBus: events,
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
    modelsPath: config.providerFixture ? null : path.join(agentDir, 'models.json'), allowModelNetwork: false, refreshOnCreate: false });
  let provider = config.provider ?? settings.getDefaultProvider(), modelId = config.model ?? settings.getDefaultModel();
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
    tools: [...new Set([...(config.executionProfile === 'native' ? ['read','write','edit','bash','ls','find','grep'] : []),
      ...resources.getExtensions().extensions.flatMap(extension => [...extension.tools.keys()])])] });
  session.agent.toolExecution = 'sequential';
  let writeTail = Promise.resolve(); let detached = false; let active: Promise<void> | undefined;
  let exitCode = 0; let usageAccountingFailed = false; let profileBindingFailed = false; let backendReady = false;
  let activeInputId: string | undefined; let outcome: string = 'unknown';
  const executionFacts: Json[] = [];
  const identity = { execution_profile: config.executionProfile, profile_source: config.profileSource,
    extension_sha256: createHash('sha256').update(fs.readFileSync(extensionPath)).digest('hex'),
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
  events.on('biomind:backend', (raw: unknown) => {
    const data = raw as Json;
    backendReady = data.state === 'ready';
    if (data.state === 'unavailable' || data.state === 'failed') {
      outcome = 'error'; exitCode = 2;
      write({ type: 'diagnostic', code: data.state === 'failed' ? 'BACKEND_FAILED' : 'BACKEND_UNAVAILABLE',
        stage: data.stage, ...(data.message ? { message: data.message } : {}) });
    }
  });
  events.on('biomind:settings_result', (raw: unknown) => write({ type: 'settings_result', ...(raw as Json) }));
  events.on('biomind:job_result', (raw: unknown) => write({ type: 'job_result', ...(raw as Json) }));
  events.on('biomind:permissions_result', (raw: unknown) => write({ type: 'permissions_result', ...(raw as Json) }));
  events.on('biomind:archive', (raw: unknown) => write({ type: 'archive_receipt', ...(raw as Json) }));
  events.on('biomind:diagnostic', (raw: unknown) => {
    const code = String((raw as Json).code ?? 'EXTENSION_DIAGNOSTIC');
    const diagnostic = raw as Json;
    write({ type: 'diagnostic', code,
      ...(code === 'EXECUTION_PROFILE_BINDING_FAILED' && /^EXECUTION_PROFILE_[A-Z_]+$/.test(diagnostic.reason ?? '')
        ? { reason: diagnostic.reason } : {}),
      ...(diagnostic.reason === 'DOMAIN_HANDSHAKE_TIMEOUT' && Number.isInteger(diagnostic.timeoutMs)
        ? { reason: 'DOMAIN_HANDSHAKE_TIMEOUT', timeoutMs: diagnostic.timeoutMs } : {}) });
    if (['EXECUTION_PROFILE_BINDING_FAILED', 'REQUEST_INITIALIZATION_FAILED', 'CONTEXT_POLICY_UNAVAILABLE', 'BIOMIND_USAGE_OBSERVER_FAILED'].includes(code)) {
      outcome = 'error'; exitCode = 2;
      if (code === 'EXECUTION_PROFILE_BINDING_FAILED') profileBindingFailed = true;
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
  if (profileBindingFailed || !backendReady) {
    await session.extensionRunner?.emit({ type: 'session_shutdown', reason: 'quit' });
    session.dispose(); await writeTail; return 2;
  }
  write({ type: 'ready', engine: 'pi', version: runtimeObservationLabel(), cwd,
    sessionDir: manager.getSessionDir(), projectTrusted: settings.isProjectTrusted(),
    extension: extensionPath, ...(config.modelSelection ? { model_selection: config.modelSelection } : {}), ...sessionIdentity(),
    capabilities: { sessionResume: 'startup_exact_id_or_file', continueSession: 'explicit_same_project', sessionSwitch: 'restart_with_explicit_session',
      temporarySession: 'explicit', persistenceOwner: 'Pi SessionManager', jobOwnerTakeover: 'not_implied_by_session_resume' } });
  const control = (operation: Promise<unknown>, id?: unknown) => {
    void operation.catch(() => write({ type: 'error', id, code: 'PI_CONTROL_FAILED' }));
  };
  const handle = (frame: Json) => {
    if (!backendReady && ['prompt', 'steer', 'follow_up', 'compact'].includes(frame.type)) {
      write({ type: 'error', id: frame.id, code: 'BACKEND_NOT_READY' }); return;
    }
    if (frame.type === 'prompt') {
      if (active) { write({ type: 'error', id: frame.id, code: 'REQUEST_ACTIVE_USE_STEER_OR_FOLLOW_UP' }); return; }
      if (typeof frame.text !== 'string') { write({ type: 'error', id: frame.id, code: 'TEXT_REQUIRED' }); return; }
      activeInputId = String(frame.requestId ?? frame.taskId ?? frame.id ?? randomUUID());
      const inputId = activeInputId; outcome = 'unknown'; executionFacts.splice(0);
      active = (async () => {
        try {
          await session.prompt(frame.text); await session.waitForIdle();
          write({ type: 'result', ...sessionIdentity(), requestId: inputId, runtime_status: usageAccountingFailed || !backendReady ? 'error' : outcome,
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
      // The opaque UI id correlates legacy local clients. Clients supplying
      // explicit business/session/action identities must match the same prompt.
      if ((frame.sessionId !== undefined && frame.sessionId !== request.sessionId)
          || (frame.requestId !== undefined && frame.requestId !== activeInputId)
          || (frame.businessRequestId !== undefined && frame.businessRequestId !== request.requestId)
          || (frame.action_hash !== undefined && frame.action_hash !== request.proposal?.action_hash)) {
        write({ type: 'error', id: frame.id, code: 'STALE_UI_RESPONSE' }); return;
      }
      const value = frame.value;
      if (value && typeof value === 'object' && ((value.sessionId !== undefined && value.sessionId !== request.sessionId)
          || (value.requestId !== undefined && value.requestId !== request.requestId)
          || (value.action_hash !== undefined && value.action_hash !== request.proposal?.action_hash))) {
        write({ type: 'error', id: frame.id, code: 'STALE_UI_RESPONSE' }); return;
      }
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
    } else if (frame.type === 'permissions') {
      if (!['status', 'revoke'].includes(frame.action) || (frame.action === 'revoke'
          && (typeof frame.grant_id !== 'string' || !frame.request_scope || typeof frame.policy_revision !== 'string'))) {
        write({ type: 'permissions_result', id: frame.id, status: 'error', code: 'INVALID_PERMISSIONS_COMMAND' }); return;
      }
      events.emit('biomind:permissions_request', { id: frame.id ?? randomUUID(), action: frame.action,
        ...(frame.action === 'revoke' ? { grant_id: frame.grant_id, request_scope: frame.request_scope, policy_revision: frame.policy_revision } : {}) });
    } else if (frame.type === 'settings') {
      if ((frame.history_enabled !== undefined && typeof frame.history_enabled !== 'boolean') ||
          (frame.memory_mode !== undefined && !['ask','auto','manual','disabled'].includes(frame.memory_mode))) {
        write({ type: 'settings_result', id: frame.id, status: 'error', code: 'INVALID_SETTINGS' }); return;
      }
      events.emit('biomind:settings_request', { id: frame.id ?? randomUUID(),
        ...(frame.history_enabled !== undefined ? { history_enabled: frame.history_enabled } : {}),
        ...(frame.memory_mode !== undefined ? { memory_mode: frame.memory_mode } : {}) });
    } else if (frame.type === 'reload') {
      if (active) { write({ type: 'error', id: frame.id, code: 'REQUEST_ACTIVE_RELOAD_PENDING' }); return; }
      backendReady = false;
      control(session.reload().then(() => write({ type: 'reload_result', id: frame.id, status: 'reloaded' })), frame.id);
    } else if (frame.type === 'compact') {
      if (active) { write({ type: 'error', id: frame.id, code: 'REQUEST_ACTIVE' }); return; }
      control(session.compact().then(() => write({ type: 'compact_result', id: frame.id, status: 'compacted' })), frame.id);
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
    'project-dir': { type: 'string' }, 'project-source': { type: 'string' }, config: { type: 'string' },
    'execution-profile': { type: 'string' }, 'migrate-execution-profile': { type: 'boolean' },
    'launch-cwd': { type: 'string' }, 'session-dir': { type: 'string' },
    'project-command': { type: 'string' }, 'project-target': { type: 'string' },
    'project-open': { type: 'boolean' },
    'entry-python': { type: 'string' }, 'resolve-entry-model': { type: 'boolean' }, 'model-record': { type: 'string' },
    'model-selection': { type: 'string' }, 'registry-path': { type: 'string' }, prompt: { type: 'string' },
    session: { type: 'string' }, continue: { type: 'boolean' }, resume: { type: 'boolean', short: 'r' }, 'no-session': { type: 'boolean' } } });
  if (values['project-command'] && !['list', 'create', 'menu'].includes(values['project-command']))
    throw new Error('PROJECT_COMMAND_INVALID');
  const launchCwd = path.resolve(values['launch-cwd'] ?? process.cwd());
  let cwd = path.resolve(values['project-dir'] ?? launchCwd);
  const projectSource = values['project-source'] ?? (values['project-dir'] === undefined ? 'cwd' : 'explicit');
  if (!['cwd', 'explicit'].includes(projectSource)) throw new Error('PROJECT_SOURCE_INVALID');
  const explicitProject = projectSource === 'explicit' || !!values['project-open'];
  locatePackage('@earendil-works/pi-coding-agent'); locatePackage('@earendil-works/pi-ai'); locatePackage('@earendil-works/pi-agent-core');
  // Verify the actual SDK and published CLI bundle bytes before any session can send.
  // Extension-load errors alone are not fail-closed in upstream Pi.
  assertObservationMode();
  const compat = JSON.parse(fs.readFileSync(path.join(runtime, 'compat/pi-0.87.1-observer.patch.json'), 'utf8'));
  const packageRoot = locatePackage('@earendil-works/pi-coding-agent');
  for (const target of compat.files) {
    const actual = createHash('sha256').update(fs.readFileSync(path.join(packageRoot, target.path))).digest('hex');
    if (actual !== target.patchedSha256) throw new Error('BIOMIND_PI_COMPAT_PATCH_REQUIRED');
  }
  const config = readConfig(values.config);
  if (values['project-open']) { delete config.session; delete config.continueSession; }
  config.launcherPython = values['entry-python'];
  if (config.launcherPython) process.env.BIOMIND_PI_LAUNCHER_PYTHON = config.launcherPython;
  process.env.BIOMIND_PI_NODE = process.execPath;
  if (values['model-record']) process.env.BIOMIND_PI_MODEL_RECORD = path.resolve(values['model-record']);
  else delete process.env.BIOMIND_PI_MODEL_RECORD;
  if (values.session !== undefined) {
    config.session = values.session;
    // An exact CLI recovery target overrides a configured latest-session default.
    // Explicit CLI --session + --continue remains invalid below.
    if (values.continue === undefined) delete config.continueSession;
  }
  if (values.continue !== undefined) config.continueSession = values.continue;
  if (values['no-session']) config.sessionMode = 'temporary';
  if (values['session-dir'] !== undefined) config.sessionDir = values['session-dir'];
  validateSessionSelection(config);
  if (values['migrate-execution-profile'] && (!values.session || !values['execution-profile']))
    throw new Error('EXECUTION_PROFILE_MIGRATION_REQUIRES_EXPLICIT_SESSION_AND_PROFILE');
  const terminal = !!process.stdin.isTTY && !!process.stdout.isTTY;
  if (values.resume) {
    if (config.session || config.continueSession || config.sessionMode === 'temporary') throw new ProductSessionError('SESSION_SELECTION_CONFLICT');
    if (values.mode !== 'interactive' || !terminal) throw new Error('--resume 需要交互终端；非交互模式请使用 --session 或 --continue。');
    if (values['project-command'] && values['project-command'] !== 'menu') throw new Error('--resume 仅适用于打开项目或交互启动。');
  }
  const automaticSessionChoice = values.mode === 'interactive' && terminal && values.prompt === undefined &&
    !config.session && !config.continueSession && config.sessionMode !== 'temporary' && !values.resume;
  const explicitAgentDir = config.agentDir !== undefined;
  const { SettingsManager, getAgentDir } = await import('@earendil-works/pi-coding-agent');
  const { resolveSessionTarget, createProductSettings } = await import('./native-session.js');
  let agentDir = path.resolve(config.agentDir ?? getAgentDir());
  config.agentDir = agentDir;
  process.env.PI_CODING_AGENT_DIR = agentDir;
  const sessionDirOverride = config.sessionDir;
  // Global preferences may locate saved sessions. Do not read launch-cwd project
  // settings until a valid project (or its bound session) has been selected.
  config.sessionDir = resolveSessionDir(launchCwd, config,
    SettingsManager.create(launchCwd, agentDir, { projectTrusted: false }));
  const configForProject = (root: string) => {
    const selectedAgent = !explicitAgentDir && config.providerFixture ? path.join(root, '.biomind/pi-agent') : agentDir;
    return { ...config, agentDir: selectedAgent,
      sessionDir: resolveSessionDir(root, { sessionDir: sessionDirOverride }, SettingsManager.create(root, selectedAgent)) };
  };
  const bindProjectSettings = () => {
    const selected = configForProject(cwd);
    agentDir = selected.agentDir;
    config.agentDir = agentDir; config.sessionDir = selected.sessionDir;
    process.env.PI_CODING_AGENT_DIR = agentDir;
  };
  let startupSelection = false;
  if (values['project-command'] === 'list' || values['project-command'] === 'create') {
    const { runProjectCommand } = await import('./project-launcher.js');
    return (await runProjectCommand(config, cwd, values['project-command'], values['project-target'])).exitCode ?? 0;
  }
  if (explicitProject) {
    cwd = await validateProjectRoot(config, cwd);
    bindProjectSettings();
  }
  // Exact session ownership wins over an implicit cwd. An explicit conflicting
  // project is rejected by the native metadata resolver, never silently rebound.
  if (config.session) {
    try { cwd = await resolveSessionTarget(cwd, config, launchCwd, explicitProject); }
    catch (error) {
      if (explicitProject || !(error instanceof ProductSessionError) || error.code !== 'SESSION_NOT_FOUND' ||
          config.session.includes('/') || config.session.includes('\\') || config.session.endsWith('.jsonl')) throw error;
      // A known session may live in this cwd's trusted custom sessionDir. This
      // fallback only locates native metadata: no project resources/model are
      // initialized, and HOME must pass the shared validator before any read.
      let candidate: string;
      try { candidate = await validateProjectRoot(config, cwd); }
      catch (failure) { if (failure instanceof ProjectDirectoryError) throw error; throw failure; }
      const selectedAgent = !explicitAgentDir && config.providerFixture ? path.join(candidate, '.biomind/pi-agent') : agentDir;
      const directory = resolveSessionDir(candidate, { sessionDir: sessionDirOverride }, createProductSettings(candidate, selectedAgent));
      if (directory === config.sessionDir) throw error;
      config.sessionDir = directory;
      cwd = await resolveSessionTarget(candidate, config, launchCwd, false);
    }
  }
  let projectFailure: ProjectDirectoryError | undefined;
  try { cwd = await validateProjectRoot(config, cwd); }
  catch (error) { if (!(error instanceof ProjectDirectoryError)) throw error; projectFailure = error; }
  // An implicit cwd is a candidate, not the user's project confirmation.
  // Reuse the existing project menu before loading that project's resources.
  const confirmProject = values.mode === 'interactive' && terminal && !explicitProject &&
    !config.session && !config.continueSession && !values.resume;
  const chooseProject = values['project-command'] === 'menu' || confirmProject ||
    (!!projectFailure && !explicitProject && !config.session && values.mode === 'interactive' && terminal);
  if (chooseProject) {
    if (values.mode !== 'interactive' || !terminal) throw new ProjectDirectoryError('选择项目需要交互终端；请使用 biomind --project-dir <项目目录>。');
    const { runStartupProjectMenu } = await import('./startup-menu.js');
    const result = await runStartupProjectMenu(config, launchCwd, configForProject,
      { selectSession: automaticSessionChoice });
    if (result.exitCode !== undefined) return result.exitCode;
    cwd = await validateProjectRoot(config, result.cwd!);
    if (result.session) { config.session = result.session; delete config.continueSession; }
    if (result.continueSession) config.continueSession = true;
    startupSelection = true;
  } else if (projectFailure) {
    throw new ProjectDirectoryError(`${projectFailure.message}；请使用 biomind --project-dir <项目目录>，或在交互终端选择项目。`);
  }
  bindProjectSettings();
  if (values.resume || (automaticSessionChoice && !startupSelection)) {
    const { runStartupSessionMenu } = await import('./project-launcher.js');
    const choice = await runStartupSessionMenu(config, cwd, { resume: values.resume,
      allowAllProjects: !explicitProject && values['project-command'] === undefined });
    if (choice.exitCode !== undefined) return choice.exitCode;
    if (choice.session) config.session = choice.session;
    if (choice.continueSession) config.continueSession = true;
  }
  cwd = await resolveSessionTarget(cwd, config, launchCwd, startupSelection || explicitProject || values['project-command'] !== undefined);
  cwd = await validateProjectRoot(config, cwd);
  bindProjectSettings();
  if (values.mode === 'interactive') {
    if (!fs.existsSync(nativeExtensionPath)) throw new Error('PI_NATIVE_EXTENSION_BUILD_REQUIRED');
    if (terminal) process.stderr.write('正在初始化聊天环境，请稍候…\n');
  }
  if (values['resolve-entry-model']) {
    if (!values['entry-python']) throw new Error('ENTRY_PYTHON_REQUIRED');
    const args = ['-m', 'agent_core.pi_launch_metadata', 'model', '--project', cwd,
      ...(values['model-record'] ? ['--record', values['model-record']] : [])];
    const resolved = await promisify(execFile)(values['entry-python'], args, {
      cwd: path.join(runtime, '../..'), env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, encoding: 'utf8', maxBuffer: 1024 * 1024,
    });
    const metadata = JSON.parse(resolved.stdout);
    if (metadata.selection) values['model-selection'] = JSON.stringify(metadata.selection);
    if (metadata.registryPath && !config.registryPath) config.registryPath = metadata.registryPath;
  }
  if (values['registry-path'] && !config.registryPath) config.registryPath = values['registry-path'];
  if (values['model-selection']) {
    const selection = validateModelSelection(JSON.parse(values['model-selection']));
    if (config.providerFixture || (config.provider && config.provider !== selection.provider) ||
        (config.model && config.model !== selection.model) || (config.thinkingLevel && config.thinkingLevel !== selection.thinking)) {
      throw new Error('CONFLICTING_PRODUCT_MODEL_CONFIGURATION');
    }
    Object.assign(config, { provider: selection.provider, model: selection.model, thinkingLevel: selection.thinking, modelSelection: selection });
    if (selection.provider === 'deepseek' && !process.env.DEEPSEEK_API_KEY) {
      const alias = process.env.QWEN_API_KEY ?? process.env.DASHSCOPE_API_KEY ?? process.env.OPENAI_API_KEY;
      if (alias) process.env.DEEPSEEK_API_KEY = alias;
    }
  }
  let previousProfile: ExecutionProfile | undefined;
  if (config.session || config.continueSession) {
    const { openProductSession } = await import('./native-session.js');
    const selected = openProductSession(cwd, config);
    const filename = selected.getSessionFile();
    if (filename && fs.existsSync(filename)) {
      previousProfile = sessionProfile(path.resolve(cwd, config.dataRoot ?? '.biomind/pi-domain'), selected.getSessionId());
      config.session = filename; delete config.continueSession;
    }
  }
  const chosen = chooseExecutionProfile({ cli: values['execution-profile'], configured: config.executionProfile,
    environment: process.env.BIOMIND_PI_EXECUTION_PROFILE, forced: process.env.BIOMIND_PI_FORCE_EXECUTION_PROFILE,
    previous: previousProfile, migrate: values['migrate-execution-profile'] });
  config.executionProfile = chosen.profile; config.profileSource = chosen.source;
  config.migrateProfile = values['migrate-execution-profile'] ?? false;
  configureDomain(config, cwd, values.mode === 'interactive' ? nativeExtensionPath : extensionPath);
  if (values.mode === 'interactive') return await startNative(config, cwd, values.prompt);
  if (values.mode === 'json') return await startJson(config, cwd, values.prompt);
  throw new Error('Unsupported mode');
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await main(); } catch (error) {
    // Never print unfiltered settings/provider exceptions (they can contain URLs
    // with credentials). Full typed fixture assertions live in test evidence.
    if (error instanceof ProductSessionError) {
      process.stderr.write(`BioMind 会话错误: ${error.message}. No temporary fallback was used.\n`);
    }
    if (error instanceof Error && error.message.startsWith('EXECUTION_PROFILE_'))
      process.stderr.write(error.message + '\nUse a new process with --execution-profile protected, or explicitly migrate an idle --session.\n');
    if (error instanceof Error && error.message === 'PI_NATIVE_EXTENSION_BUILD_REQUIRED')
      process.stderr.write('PI_NATIVE_EXTENSION_BUILD_REQUIRED: run npm run build in integrations/pi_runtime.\n');
    if (error instanceof ProjectDirectoryError) process.stderr.write(`BioMind: ${error.message}\n`);
    if (error instanceof TerminalInputError) process.stderr.write(`${error.message}\n${error.inputError.stack ?? ''}\n`);
    if (process.argv.includes('json')) process.stdout.write(JSON.stringify({ type: 'error',
      code: error instanceof ProductSessionError || error instanceof ProjectDirectoryError || error instanceof TerminalInputError ? error.code : 'PI_INITIALIZATION_FAILED',
      ...(error instanceof ProjectDirectoryError || error instanceof ProductSessionError ? { message: error.message } : {}) }) + '\n');
    process.stderr.write('BioMind Pi host failed to initialize or run; no legacy fallback was attempted.\n');
    process.exitCode = 2;
  }
}
