/** A transport/UI host around Pi. Contains no model/tool continuation loop. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { parseArgs } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
import type { ModelRuntime } from '@earendil-works/pi-coding-agent';
import { JsonFrameDecoder } from './json-frame.js';
import { IMAGE_LIMITS, ImageInputError, validateInputImages } from './image-input.js';
import { AdmissionError, MessageAdmissions, validateFrameIds, type Admission } from './message-admissions.js';
import { BoundedJsonOutput } from './bounded-output.js';
import { loadBuildIdentity, type LoadedBuildIdentity } from './build-identity.js';
import { resolveHandshakeTimeoutMs } from './domain-client.js';
import { assertObservationMode, observationMode, runtimeObservationLabel, projectObservedUsage } from './usage-observer.js';
import { validateSessionSelection, resolveSessionDir, ProductSessionError, TerminalInputError, type NativeSessionSelection } from './native-session-selection.js';
import { configuredProvider, selectConfiguredModel, validateConfiguredCustomModel, validateModelSelection, type ModelSelection } from './model-config.js';
import { chooseExecutionProfile, sessionProfile, type ExecutionProfile } from './execution-profile.js';
import { StudioResourceSelection } from './studio-resources.js';
import { createStudioControls } from './studio-controls.js';
import { acquireSessionWriter, SessionWriterError, type SessionWriterLease } from './session-writer.js';
import { attachNativeWriter, writerHeader } from './session-writer-host.js';
import { validateProjectRoot, ProjectDirectoryError } from './project-launcher.js';
import { acquirePluginLease, pluginChildOptions } from './plugin-lease.js';
import { StudioPackageShutdown } from './studio-package-shutdown.js';
import { verifyObserverBytes } from './compatibility-check.js';


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
  loadedBuild?: LoadedBuildIdentity;
  writerLeases?: SessionWriterLease[]; ownerGeneration?: string; writerClean?: boolean;
  pythonSource?: 'config' | 'environment' | 'launcher';
  resourceOwnership?: {protocol:'linux_flock_v1';mode:'shared';roots:string[];releaseOn:'last_actual_process_exit'};
  executionProfile?: ExecutionProfile; profileSource?: string; migrateProfile?: boolean;
}
export function readConfig(filename?: string): Config {
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

/** Check the saved choice against current definitions and native credential storage.
 * This is local credential availability, not proof that a provider accepts the key.
 */
export async function validateRestoredModel(modelRuntime: ModelRuntime,
  restoredModel: { provider: string; modelId: string } | null, selection?: ModelSelection) {
  // Restoration runs before createAgentSession loads extension providers. Register
  // the same confirmed custom definition as new sessions without selecting it.
  if (selection) {
    const custom = configuredProvider(selection);
    if (custom) modelRuntime.registerProvider(selection.provider, custom);
  }
  // refreshOnCreate:false leaves auth-status snapshots empty; checkAuth consults
  // the native store, including credentials saved by another process since launch.
  const model = restoredModel && modelRuntime.getModel(restoredModel.provider, restoredModel.modelId);
  if (!model || !(await modelRuntime.checkAuth(model.provider))) {
    throw new ProductSessionError('SESSION_MODEL_UNAVAILABLE');
  }
  return model;
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
  const installRoot = config.loadedBuild?.releaseRoot ?? path.resolve(config.installRoot ?? path.join(runtime, '..', '..'));
  if (config.loadedBuild && fs.realpathSync(config.installRoot ?? installRoot) !== fs.realpathSync(installRoot)) throw new Error('BIOMIND_BUILD_INSTALL_ROOT_MISMATCH');
  const python = config.python ?? process.env.BIOMIND_PI_PYTHON ?? config.launcherPython;
  if (typeof python !== 'string' || !python.trim()) throw new Error('DOMAIN_PYTHON_REQUIRED');
  config.pythonSource = config.python !== undefined ? 'config' : process.env.BIOMIND_PI_PYTHON !== undefined ? 'environment' : 'launcher';
  // The domain client validates Python when a domain operation is requested;
  // native login/model/instructions must remain reachable without scientific Python.
  for (const [key, value] of Object.entries({ BIOMIND_PI_PYTHON: python,
    BIOMIND_PI_HANDSHAKE_TIMEOUT_MS: String(resolveHandshakeTimeoutMs(config.handshakeTimeoutMs)),
    BIOMIND_PI_INSTALL_ROOT: installRoot, BIOMIND_PI_PROJECT_ROOT: cwd,
    BIOMIND_PI_RELEASE_ROOT: config.loadedBuild?.releaseRoot, BIOMIND_PI_RELEASE_ID: config.loadedBuild?.releaseId,
    BIOMIND_PI_SOURCE_COMMIT:config.loadedBuild?.sourceCommit,
    BIOMIND_PI_EXECUTION_PROFILE: config.executionProfile,
    BIOMIND_PI_BUILD_ID: config.loadedBuild?.buildId ?? createHash('sha256').update(fs.readFileSync(loadedExtension)).digest('hex').slice(0,12),
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
  if (!config.session && ((config.modelSelection && !validateConfiguredCustomModel(config.modelSelection)) ||
      (!config.modelSelection && config.provider && config.model && !config.providerFixture))) {
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
  const args = ['--import', path.join(runtime, 'dist/plugin-lease.js'), path.resolve(packageRoot, bin),
    '--extension', nativeExtensionPath, '--skill', process.env.BIOMIND_PI_SKILLS_ROOT ?? path.join(process.env.BIOMIND_PI_INSTALL_ROOT!, 'skills'),
    ...(config.executionProfile === 'native' ? [] : ['--no-builtin-tools']), '--tui-mode', 'fullscreen'];
  if (config.providerFixture) {
    args.push('--offline', '--extension', path.join(runtime, 'dist/native-fixture.ts'));
    if(!config.session)args.push('--provider','biomind-fixture','--model','scripted');
  } else if (!config.session) {
    if (config.provider) args.push('--provider', config.provider);
    if (config.model) args.push('--model', config.model);
  }
  if (config.sessionMode === 'temporary') args.push('--no-session');
  if (config.sessionDir) args.push('--session-dir', config.sessionDir);
  if (config.session) args.push('--session', config.session);
  if (config.continueSession) args.push('--continue');
  if (!config.session && config.thinkingLevel) args.push('--thinking', config.thinkingLevel);
  if (prompt !== undefined) args.push('--', prompt);
  const resourceChild = pluginChildOptions(['inherit','inherit','inherit','ipc']);
  const env = { ...process.env, ...resourceChild.env, ...(agentDir !== undefined ? { PI_CODING_AGENT_DIR: agentDir } : {}),
    BIOMIND_SESSION_WRITER_MANAGED:'parent',
    BIOMIND_PI_UI_COMMANDS: '1', BIOMIND_PI_QUIET_STARTUP: 'native', PI_SKIP_VERSION_CHECK: '1',
    ...(config.modelSelection ? { BIOMIND_PI_MODEL_SELECTION: JSON.stringify(config.modelSelection) } : {}),
    ...(config.providerFixture ? { PI_OFFLINE: '1', BIOMIND_PI_PROVIDER_FIXTURE: path.resolve(config.providerFixture) } : {}) };
  const child = spawn(process.execPath, args, { cwd, env, stdio: resourceChild.stdio });
  const finishWriter=attachNativeWriter(child,{cwd,sessionDir:config.sessionDir,ownerGeneration:config.ownerGeneration!,leases:config.writerLeases!});
  let code:number;
  try {code = await new Promise<number>((resolve, reject) => { child.once('error', reject); child.once('exit', (code, signal) => {config.writerClean=code===0&&signal===null;resolve(code ?? (signal ? 130 : 1));}); });}
  finally {await finishWriter();}
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
  process.env.BIOMIND_SESSION_WRITER_MANAGED='json';
  process.env.BIOMIND_SESSION_WRITER_OWNER_GENERATION=config.ownerGeneration!;
  if(manager.getSessionFile()){
    const lease=await acquireSessionWriter({sessionFile:manager.getSessionFile()!,sessionId:manager.getSessionId(),cwd,ownerGeneration:config.ownerGeneration!,mode:config.session?'resume':'new'});
    if(!config.writerLeases!.some(item=>item.identity.sessionFile===lease.identity.sessionFile))config.writerLeases!.push(lease);
  }
  // Match native Pi's public HTTP setup, including the existing proxy routing.
  const dispatcher = await import(pathToFileURL(path.join(locatePackage('@earendil-works/pi-coding-agent'), 'dist/core/http-dispatcher.js')).href);
  dispatcher.configureHttpDispatcher(settings.getHttpIdleTimeoutMs());
  const events = createEventBus();
  const resourceSelection = new StudioResourceSelection(settings,cwd,agentDir,extensionPath);
  const resources = new DefaultResourceLoader({ cwd, agentDir, settingsManager: settings, eventBus: events,
    additionalExtensionPaths: [extensionPath],
    additionalSkillPaths: [process.env.BIOMIND_PI_SKILLS_ROOT ?? path.join(process.env.BIOMIND_PI_INSTALL_ROOT!, 'skills')],
    skillsOverride: resourceSelection.override,
    agentsFilesOverride: ({ agentsFiles }) => ({ agentsFiles: filterNativeContextFiles(agentsFiles, cwd, agentDir) }) });
  await resources.reload();
  if (resources.getExtensions().errors.length) throw new Error('BioMind extension failed to load');
  if (!resources.getExtensions().extensions.some(extension=>path.resolve(extension.path)===extensionPath)) throw new Error('BioMind extension was not loaded');
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
  const restored = manager.buildSessionContext();
  const restoring = !!config.session && restored.model !== null;
  // Pi owns persisted choices. Refuse unavailable restored choices instead of
  // allowing SDK fallback to a different configured (possibly paid) model.
  if(restoring&&!['off','minimal','low','medium','high','xhigh'].includes(restored.thinkingLevel))throw new ProductSessionError('SESSION_THINKING_LEVEL_INVALID');
  const restoredModel = restoring ? await validateRestoredModel(modelRuntime, restored.model, config.modelSelection) : undefined;
  if (!restoring && (!provider || !modelId)) throw new Error('Headless mode requires explicit existing provider/model or a synthetic providerFixture');
  const model = restoring ? restoredModel : config.modelSelection ? selectConfiguredModel(modelRuntime, config.modelSelection) : modelRuntime.getModel(provider!, modelId!);
  if (!restoring && !model) throw new Error('Requested model is unavailable in this pinned runtime');
  const { session } = await createAgentSession({ cwd, agentDir, modelRuntime,
    ...(!restoring ? {model,...(config.thinkingLevel!==undefined?{thinkingLevel:config.thinkingLevel}:{})}
      // Native SDK restoration also consults its auth snapshot. Supplying the
      // checked saved choice prevents a fallback to a different default model.
      : {model,thinkingLevel:restored.thinkingLevel as import('@earendil-works/pi-agent-core').ThinkingLevel}),
    resourceLoader: resources, settingsManager: settings,
    sessionManager: manager,
    tools: [...new Set([...(config.executionProfile === 'native' ? ['read','write','edit','bash','ls','find','grep'] : []),
      ...resources.getExtensions().extensions.flatMap(extension => [...extension.tools.keys()])])] });
  session.agent.toolExecution = 'sequential';
  let detached = false; let active: Promise<void> | undefined;
  let exitCode = 0; let usageAccountingFailed = false; let profileBindingFailed = false; let backendReady = false;
  let activeInputId: string | undefined; let outcome: string = 'unknown'; let settledReceipt: Json = {};
  let controlBusy: 'reload' | 'compact' | undefined;
  const executionFacts: Json[] = [];
  const identity = { resourceOwnership:config.resourceOwnership, loaded_build: config.loadedBuild, build_id: process.env.BIOMIND_PI_BUILD_ID, python: { path: process.env.BIOMIND_PI_PYTHON, source: config.pythonSource }, execution_profile: config.executionProfile, profile_source: config.profileSource,
    extension_sha256: createHash('sha256').update(fs.readFileSync(extensionPath)).digest('hex'),
    sessionId: session.sessionManager.getSessionId() };
  const ownerGeneration = config.ownerGeneration!;
  const studio = createStudioControls({session,settings,resources,resourceSelection,events,ownerGeneration,
    busy:()=>!!active||!!controlBusy,activeRequestId:()=>activeInputId,
    memoryReadTimeoutMs:resolveHandshakeTimeoutMs(config.handshakeTimeoutMs)+10000});
  const admissions = new MessageAdmissions({ ownerGeneration, sessionId: identity.sessionId, project: cwd });
  const modelIdentity = () => ({ provider: session.model?.provider, id: session.model?.id,
    input: session.model?.input ?? [], blockImages: settings.getBlockImages(),
    autoResize: settings.getImageAutoResize(), providerInputLimits: session.model?.inputLimits ?? null });
  const sessionIdentity = () => ({ ...identity, ...describeProductSession(manager),writerOwnership:{supported:true,acquired:!!manager.getSessionFile(),ownerGeneration,pid:process.pid,scope:'session',temporary:!manager.getSessionFile()} });
  const waiting = new Map<string, Json>();
  let lastJobReceipt: Json | undefined;
  const pendingDomainControls=new Set<string>();
  const packageReads=new Map<string,{resolve:(value:boolean)=>void,reject:(error:Error)=>void,timer:ReturnType<typeof setTimeout>}>();
  // Associations are receipts only: Pi still owns its execution queues.
  const queuedAdmissions: { entry?: Admission; command: 'steer' | 'follow_up'; textHash: string; imageHashes: string[] }[] = [];
  let promptAdmission: Admission | undefined;
  let promptUserPending = false;
  const validationLifetime = new AbortController();
  const outputLimits = { maxPendingBytes: 24 * 1024 * 1024, maxPendingFrames: 256 };
  const output = new BoundedJsonOutput(process.stdout, outputLimits, () => {
      detached = true; exitCode = 2;
      validationLifetime.abort();
      process.stderr.write('BioMind Pi output channel failed; request state is unavailable to this client.\n');
      // This is a failed owned stdio connection, not a desktop detach. A future
      // Studio adapter must keep consuming this stream when pages disconnect.
      process.stdin.destroy();
      void session.abort();
  });
  const write = (value: unknown) => {
    if (detached || output.detached) return;
    const frame = value as Json;
    output.write(value, { droppable: frame.type === 'event' && frame.event?.type === 'message_update' });
  };
  const packages = new StudioPackageShutdown(
    ()=>backendReady&&!active&&!controlBusy&&!waiting.size&&!pendingDomainControls.size
      &&!session.isStreaming&&!session.isCompacting&&!session.agent.hasQueuedMessages()
      &&queuedAdmissions.length===0,
    ()=>new Promise<boolean>((resolve,reject)=>{
      const id='packages-'+randomUUID();
      const timer=setTimeout(()=>{packageReads.delete(id);reject(new Error('PACKAGES_STATE_UNAVAILABLE'));},10000);
      packageReads.set(id,{resolve,reject,timer});events.emit('biomind:packages_state_request',{id});
    }));
  events.on('biomind:packages_state_result',(raw:unknown)=>{
    const result=raw as Json, entry=packageReads.get(result.id);
    if(!entry)return;clearTimeout(entry.timer);packageReads.delete(result.id);
    if(result.status==='ok'&&result.sessionId===identity.sessionId&&result.idle===true&&result.activeJobs===0)entry.resolve(true);
    else entry.reject(new Error(result.code==='PACKAGES_BUSY'?'PACKAGES_BUSY':'PACKAGES_STATE_UNAVAILABLE'));
  });
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
  events.on('biomind:settings_result', (raw: unknown) => {pendingDomainControls.delete(String((raw as Json).id));write({ type: 'settings_result', ...(raw as Json) });});
  events.on('biomind:job_result', (raw: unknown) => { lastJobReceipt = raw as Json; pendingDomainControls.delete(String(lastJobReceipt.id)); write({ type: 'job_result', ...lastJobReceipt }); });
  events.on('biomind:permissions_result', (raw: unknown) => {pendingDomainControls.delete(String((raw as Json).id));write({ type: 'permissions_result', ...(raw as Json) });});
  events.on('biomind:archive', (raw: unknown) => write({ type: 'archive_receipt', ...(raw as Json) }));
  events.on('biomind:diagnostic', (raw: unknown) => {
    const code = String((raw as Json).code ?? 'EXTENSION_DIAGNOSTIC');
    const diagnostic = raw as Json;
    write({ type: 'diagnostic', code,
      ...(code === 'EXECUTION_PROFILE_BINDING_FAILED' && /^EXECUTION_PROFILE_[A-Z_]+$/.test(diagnostic.reason ?? '')
        ? { reason: diagnostic.reason } : {}),
      ...(diagnostic.reason === 'DOMAIN_HANDSHAKE_TIMEOUT' && Number.isInteger(diagnostic.timeoutMs)
        ? { reason: 'DOMAIN_HANDSHAKE_TIMEOUT', timeoutMs: diagnostic.timeoutMs } : {}) });
    if (['EXECUTION_PROFILE_BINDING_FAILED', 'SESSION_WRITER_BINDING_FAILED', 'REQUEST_INITIALIZATION_FAILED', 'CONTEXT_POLICY_UNAVAILABLE', 'BIOMIND_USAGE_OBSERVER_FAILED'].includes(code)) {
      outcome = 'error'; exitCode = 2;
      if (['EXECUTION_PROFILE_BINDING_FAILED','SESSION_WRITER_BINDING_FAILED'].includes(code)) profileBindingFailed = true;
      if (code === 'BIOMIND_USAGE_OBSERVER_FAILED') usageAccountingFailed = true;
    }
  });
  events.on('biomind:settled', (raw: unknown) => { settledReceipt = raw as Json; outcome = settledReceipt.outcome; write({ type: 'biomind_settled', data: raw }); });
  session.subscribe(event => {
    if (event.type === 'message_start' && event.message.role === 'user') {
      if (promptUserPending) { if (promptAdmission) promptAdmission.status = 'consumed'; promptAdmission = undefined; promptUserPending = false; }
      else {
        const content = event.message.content;
        const text = typeof content === 'string' ? content : content.filter(part => part.type === 'text').map(part => part.text).join('');
        const hashes = typeof content === 'string' ? [] : content.filter(part => part.type === 'image').map(part => createHash('sha256').update(Buffer.from(part.data, 'base64')).digest('hex'));
        // Match exact content only. Skill expansion or other transformations remain
        // unknown rather than assigning a consumed id by a guessed queue position.
        const textHash = createHash('sha256').update(text).digest('hex');
        const matches = queuedAdmissions.filter(item => item.textHash === textHash && JSON.stringify(item.imageHashes) === JSON.stringify(hashes));
        const next = matches.find(item => item.command === 'steer') ?? matches[0];
        if (next) {
          queuedAdmissions.splice(queuedAdmissions.indexOf(next), 1);
          if (next.entry) { next.entry.status = 'consumed'; delete next.entry.code;
            write({ type: 'message_consumed', clientMessageId: next.entry.clientMessageId, ownerGeneration, sessionId: identity.sessionId }); }
        } else {
          // A transformed input cannot prove which accepted id was consumed.
          // Retain bounded associations and forbid replay; never guess a position.
          for (const item of queuedAdmissions) if (item.entry) {
            item.entry.status = 'unknown'; item.entry.code = 'PI_INPUT_CONSUMPTION_UNKNOWN';
          }
        }
      }
    }
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
    studio.dispose(); session.dispose(); await output.flush().catch(() => {}); return 2;
  }
  // Read the current Domain policy before exposing settings capability; no model or settings write.
  await studio.refreshMemory();
  if (!backendReady) {
    await session.extensionRunner?.emit({type:'session_shutdown',reason:'quit'});
    studio.dispose(); session.dispose(); await output.flush().catch(()=>{}); return 2;
  }
  write({ type: 'ready', engine: 'pi', version: runtimeObservationLabel(), protocolVersion: 1, ownerGeneration, model: modelIdentity(), inputLimits: IMAGE_LIMITS, outputLimits, cwd,
    sessionDir: manager.getSessionDir(), projectTrusted: settings.isProjectTrusted(),
    resources: { contextFiles: resources.getAgentsFiles().agentsFiles.map(file => ({ path: file.path })),
      skills: resources.getSkills().skills.map(skill => ({ name: skill.name, path: skill.filePath })),
      activeTools: session.getActiveToolNames(), domainInitialization: 'before_ready' },
    extension: extensionPath, ...(config.modelSelection ? { model_selection: config.modelSelection } : {}), ...sessionIdentity(),
    capabilities: { sessionResume: 'startup_exact_id_or_file', continueSession: 'explicit_same_project', sessionSwitch: 'restart_with_explicit_session',
      temporarySession: 'explicit', persistenceOwner: 'Pi SessionManager', jobOwnerTakeover: 'not_implied_by_session_resume',
      images: true, imageInputEnabled: !!session.model?.input.includes('image') && !settings.getBlockImages(),
      getState: true, admissionReceipts: 'same_owner_bounded_no_eviction', maxAdmissionReceipts: admissions.capacity,
      automaticReplay: false, clearQueue: false, ...studio.capabilities(),
      maxPendingInputs: 16,
      historyExport: false, settings: ['history_enabled','memory_mode'],
      serviceDetach: false, remoteServerBinding: false } });
  const control = (operation: Promise<unknown>, id?: unknown) => {
    void operation.catch(() => write({ type: 'error', id, code: 'PI_CONTROL_FAILED' }));
  };
  const handle = async (frame: Json, frameBytes?: number) => {
    try { validateFrameIds(frame); }
    catch { write({ type: 'error', code: 'INVALID_CORRELATION_ID' }); return; }
    if (!['prompt','steer','follow_up'].includes(frame.type) &&
        ((frame.ownerGeneration !== undefined && frame.ownerGeneration !== ownerGeneration)
        || (frame.sessionId !== undefined && frame.sessionId !== identity.sessionId)
        || (frame.project !== undefined && frame.project !== cwd))) {
      write({ type: 'error', id: frame.id, code: frame.type === 'ui_response' ? 'STALE_UI_RESPONSE' : 'MESSAGE_SCOPE_MISMATCH' }); return;
    }
    if(frame.type==='resume_packages_operations'){
      packages.cancel();write({type:'packages_shutdown_cancelled',id:frame.id,...sessionIdentity(),ownerGeneration,cwd,safeToRelease:false,packagesIdle:false});return;
    }
    if(frame.type==='prepare_packages_shutdown'){
      try{await packages.prepare();write({type:'packages_shutdown_ready',id:frame.id,...sessionIdentity(),ownerGeneration,cwd,safeToRelease:true,packagesIdle:true});}
      catch(error){write({type:'error',id:frame.id,code:error instanceof Error&&error.message==='PACKAGES_BUSY'?'PACKAGES_BUSY':'PACKAGES_STATE_UNAVAILABLE'});}return;
    }
    if(packages.ready&&!['get_state','shutdown','get_models','get_tools','get_commands','get_resources','get_prompt_snapshot'].includes(frame.type)){
      write({type:'error',id:frame.id,code:'PACKAGES_OWNER_QUIESCING'});return;
    }
    if (studio.handles(frame.type)) {
      try { write({...await studio.handle(frame),id:frame.id,...sessionIdentity(),ownerGeneration}); }
      catch(error) { const code=error instanceof Error && /^[A-Z][A-Z0-9_]+$/.test(error.message)?error.message:'STUDIO_CONTROL_FAILED';
        write({type:'error',id:frame.id,code,...(error && typeof error==='object' && 'currentVersion' in error?{currentVersion:error.currentVersion}:{})}); }
      return;
    }
    if (['prompt','steer','follow_up'].includes(frame.type)) {
      let admission: Admission | undefined;
      const fail = (code: string) => { if (admission) { admission.status = 'failed'; admission.code = code; }
        write({ type: 'error', id: frame.id, clientMessageId: frame.clientMessageId, code }); };
      let validated: Awaited<ReturnType<typeof validateInputImages>>;
      try {
        const attempt = admissions.begin(frame); admission = attempt.entry;
        if (attempt.duplicate) {
          if (admission?.receipt) write({ ...admission.receipt, duplicate: true });
          else write({ type: 'message_status', id: frame.id, clientMessageId: admission?.clientMessageId,
            status: admission?.status, code: admission?.code, ownerGeneration, sessionId: identity.sessionId });
          return;
        }
        if (!backendReady) { fail('BACKEND_NOT_READY'); return; }
        if (typeof frame.text !== 'string') { fail('TEXT_REQUIRED'); return; }
        try { studio.validateCommand(frame.text); } catch { fail('UNSUPPORTED_COMMAND'); return; }
        if (controlBusy) { fail('SESSION_CONTROL_BUSY'); return; }
        if (frame.type === 'prompt' && active) { fail('REQUEST_ACTIVE_USE_STEER_OR_FOLLOW_UP'); return; }
        if (frame.type !== 'prompt' && queuedAdmissions.length >= 16) {
          fail('INPUT_QUEUE_LIMIT'); return;
        }
        validated = await validateInputImages(frame.images, { modelInput: session.model?.input,
          blockImages: settings.getBlockImages(), autoResizeImages: settings.getImageAutoResize(),
          resize: session.model?.inputLimits?.images?.resize,
          maxPerMessage: session.model?.inputLimits?.images?.maxPerMessage, frameBytes, signal: validationLifetime.signal });
        if (validated.images.length && frame.text.startsWith('/')) {
          // Pi extension commands receive text arguments only and report a
          // successful preflight even when no user/image message was admitted.
          const space = frame.text.indexOf(' ');
          const command = space < 0 ? frame.text.slice(1) : frame.text.slice(1, space);
          if (session.extensionRunner?.getCommand(command)) { fail('IMAGE_EXTENSION_COMMAND_UNSUPPORTED'); return; }
        }
      } catch (error) {
        fail(error instanceof ImageInputError || error instanceof AdmissionError ? error.code : 'INPUT_VALIDATION_FAILED'); return;
      }
      if (detached || output.detached) { if (admission) { admission.status = 'failed'; admission.code = 'OWNER_OUTPUT_UNAVAILABLE'; } return; }
      const accept = () => {
        if (!admission) return;
        if (admission.status === 'pending') admission.status = 'accepted';
        admission.receipt = { type: 'accepted', status: 'accepted', id: frame.id,
          clientMessageId: admission.clientMessageId, command: frame.type,
          ownerGeneration, sessionId: identity.sessionId, project: cwd, images: validated.receipts };
        write(admission.receipt);
      };
      if (frame.type !== 'prompt') {
        // Image decoding can outlive the run that the sender observed. Do not
        // acknowledge a new remote append that will sit on an already idle Pi.
        if ((admission || validated.images.length) && !session.isStreaming) { fail('REQUEST_NOT_ACTIVE_USE_PROMPT'); return; }
        const queue = () => frame.type === 'steer' ? session.getSteeringMessages() : session.getFollowUpMessages();
        let observedCount = queue().length; let enqueued = false;
        // Net queue length can stay unchanged when an older message is consumed
        // during input hooks. Observe Pi's actual enqueue edge instead. This host
        // serializes commands and the sole BioMind extension has no other user-input producer.
        const stopObserving = session.subscribe(event => {
          if (event.type !== 'queue_update') return;
          const count = frame.type === 'steer' ? event.steering.length : event.followUp.length;
          if (count > observedCount) enqueued = true;
          observedCount = count;
        });
        // Include legacy sends: this is a bounded hash/receipt association, not
        // another execution queue. Every input still goes directly to Pi below.
        const association = { entry: admission, command: frame.type as 'steer' | 'follow_up',
          textHash: createHash('sha256').update(frame.text).digest('hex'),
          imageHashes: validated.images.map(image => createHash('sha256').update(Buffer.from(image.data, 'base64')).digest('hex')) };
        queuedAdmissions.push(association);
        let failedBeforeAdmission = false;
        try {
          if (frame.type === 'steer') await session.steer(frame.text, validated.images);
          else await session.followUp(frame.text, validated.images);
          if (enqueued || admission?.status === 'consumed') accept();
          else {
            if (admission) { admission.status = 'unknown'; admission.code = 'PI_INPUT_ADMISSION_UNKNOWN'; }
            write({ type: 'message_status', id: frame.id, clientMessageId: frame.clientMessageId, status: 'unknown', code: 'PI_INPUT_ADMISSION_UNKNOWN' });
          }
        } catch {
          if (enqueued) { accept(); write({ type: 'error', id: frame.id, code: 'PI_CONTROL_FAILED_AFTER_ADMISSION' }); }
          else { failedBeforeAdmission = true; fail('PI_CONTROL_FAILED'); }
        } finally { stopObserving(); }
        if (failedBeforeAdmission && queuedAdmissions.includes(association)) queuedAdmissions.splice(queuedAdmissions.indexOf(association), 1);
        return;
      }
      activeInputId = String(frame.requestId ?? frame.taskId ?? frame.id ?? randomUUID());
      const inputId = activeInputId; outcome = 'unknown'; settledReceipt = {}; executionFacts.splice(0);
      active = (async () => {
        try {
          await session.prompt(frame.text, { images: validated.images, preflightResult: success => {
            if (success && outcome !== 'error') { promptUserPending = true; promptAdmission = admission; accept(); }
            else fail('PI_INPUT_NOT_ACCEPTED');
          } }); await session.waitForIdle();
          write({ type: 'result', ...sessionIdentity(), requestId: inputId, runtime_status: usageAccountingFailed || !backendReady ? 'error' : outcome,
            model_conclusion: session.getLastAssistantText() ?? '', execution_facts: [...executionFacts], scientific_success: settledReceipt.scientificSuccess ?? null,
            usage_scope: projectObservedUsage(session.sessionManager.getEntries()).scope, usage: projectObservedUsage(session.sessionManager.getEntries()), pi_entry_usage: session.getSessionStats() });
        } catch {
          if (admission?.status === 'pending') fail('PI_INPUT_NOT_ACCEPTED');
          write({ type: 'error', requestId: inputId, code: 'PI_RUN_FAILED' });
          write({ type: 'result', ...sessionIdentity(), requestId: inputId, runtime_status: 'error', model_conclusion: '', execution_facts: [...executionFacts], scientific_success: null,
            usage_scope: projectObservedUsage(session.sessionManager.getEntries()).scope, usage: projectObservedUsage(session.sessionManager.getEntries()), pi_entry_usage: session.getSessionStats() });
        } finally { waiting.clear(); promptUserPending = false; promptAdmission = undefined; active = undefined; activeInputId = undefined; }
      })();
    } else if (frame.type === 'get_state') {
      if ((frame.ownerGeneration !== undefined && frame.ownerGeneration !== ownerGeneration)
          || (frame.sessionId !== undefined && frame.sessionId !== identity.sessionId)
          || (frame.project !== undefined && frame.project !== cwd)) {
        write({ type: 'error', id: frame.id, code: 'MESSAGE_SCOPE_MISMATCH' }); return;
      }
      write({ type: 'state', id: frame.id, ...sessionIdentity(), ownerGeneration, cwd,
        active: !!active, activeRequestId: activeInputId ?? null, controlBusy: controlBusy ?? null, model: modelIdentity(),
        pendingUI: [...waiting.values()].map(request => ({ ...request, type: request.kind,
          requestId: activeInputId, businessRequestId: request.requestId })), admissions: admissions.snapshot(),
        queue: { pendingAssociationCount: queuedAdmissions.length, actualHasQueuedMessages: session.agent.hasQueuedMessages(),
          nativeDisplayCounts: { steering: session.getSteeringMessages().length, followUp: session.getFollowUpMessages().length,
            limitation: 'Pi 0.87.1 may retain consumed empty-text entries; these are not execution queue counts' },
          associations: 'bounded_exact_content_only; transformed_unmatched_inputs_unknown; no_replay' },
        jobs: { status: 'unknown', reason: 'use_job_list_or_status', ...(lastJobReceipt ? {lastReceipt:lastJobReceipt} : {}) },
        output: output.stats(), safeToRelease: packages.ready, packagesIdle:packages.ready, packagesQuiescing:packages.ready, pendingDomainControlCount:pendingDomainControls.size, ...studio.state() });
    } else if (frame.type === 'ui_response') {
      const request = waiting.get(String(frame.id));
      if (!request) { write({ type: 'error', id: frame.id, code: 'STALE_UI_RESPONSE' }); return; }
      // The opaque UI id correlates legacy local clients. Clients supplying
      // explicit business/session/action identities must match the same prompt.
      if ((frame.ownerGeneration !== undefined && frame.ownerGeneration !== ownerGeneration)
          || (frame.sessionId !== undefined && frame.sessionId !== request.sessionId)
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
      if ((request.kind === 'question' && value?.cancelled !== true && typeof answer !== 'string')
          || (request.kind !== 'question' && value?.cancelled !== true && typeof choice !== 'string')
          || (value?.replacement !== undefined && (!value.replacement || typeof value.replacement !== 'object' || Array.isArray(value.replacement)))) {
        write({ type: 'error', id: frame.id, code: 'INVALID_UI_RESPONSE' }); return;
      }
      const response = { id: request.id,
        requestId: request.requestId, sessionId: request.sessionId,
        ...(value?.cancelled === true ? { cancelled: true } : {}),
        ...(value?.replacement !== undefined ? { replacement: value.replacement } : {}),
        ...(request.kind === 'question' ? { answer } : { choice }) };
      waiting.delete(request.id); events.emit('biomind:ui_response', response);
      // This acknowledges delivery to the existing UI promise, not approval of
      // a domain action or completion of the current run.
      write({ type: 'ui_response_result', id: frame.id, status: 'accepted', requestId: activeInputId,
        businessRequestId: request.requestId, sessionId: identity.sessionId, ownerGeneration });
    } else if (frame.type === 'job') {
      if (!['list','status','logs','cancel'].includes(frame.action) ||
          (frame.action !== 'list' && typeof frame.run_ref !== 'string')) {
        write({ type: 'job_result', id: frame.id, status: 'error', code: 'INVALID_JOB_COMMAND' }); return;
      }
      const controlId=String(frame.id??randomUUID());pendingDomainControls.add(controlId);
      events.emit('biomind:job_request', { id: controlId, action: frame.action,
        ...(frame.run_ref ? { run_ref: frame.run_ref } : {}),
        ...(typeof frame.after_cursor === 'string' ? { after_cursor: frame.after_cursor } : {}) });
    } else if (frame.type === 'permissions') {
      if (!['status', 'revoke'].includes(frame.action) || (frame.action === 'revoke'
          && (typeof frame.grant_id !== 'string' || !frame.request_scope || typeof frame.policy_revision !== 'string'))) {
        write({ type: 'permissions_result', id: frame.id, status: 'error', code: 'INVALID_PERMISSIONS_COMMAND' }); return;
      }
      const controlId=String(frame.id??randomUUID());pendingDomainControls.add(controlId);
      events.emit('biomind:permissions_request', { id: controlId, action: frame.action,
        ...(frame.action === 'revoke' ? { grant_id: frame.grant_id, request_scope: frame.request_scope, policy_revision: frame.policy_revision } : {}) });
    } else if (frame.type === 'settings') {
      if ((frame.history_enabled !== undefined && typeof frame.history_enabled !== 'boolean') ||
          (frame.memory_mode !== undefined && !['ask','auto','manual','disabled'].includes(frame.memory_mode))) {
        write({ type: 'settings_result', id: frame.id, status: 'error', code: 'INVALID_SETTINGS' }); return;
      }
      const controlId=String(frame.id??randomUUID());pendingDomainControls.add(controlId);
      events.emit('biomind:settings_request', { id: controlId,
        ...(frame.history_enabled !== undefined ? { history_enabled: frame.history_enabled } : {}),
        ...(frame.memory_mode !== undefined ? { memory_mode: frame.memory_mode } : {}) });
    } else if (frame.type === 'reload') {
      if (active) { write({ type: 'error', id: frame.id, code: 'REQUEST_ACTIVE_RELOAD_PENDING' }); return; }
      if (controlBusy) { write({ type: 'error', id: frame.id, code: 'SESSION_CONTROL_BUSY' }); return; }
      // Reload project resources only while the code is still the loaded build.
      // Otherwise the extension could change under a process whose identity is A.
      try {
        if (loadBuildIdentity(runtime, 'json').buildId !== config.loadedBuild?.buildId) {
          write({ type: 'error', id: frame.id, code: 'BUILD_CHANGED_RESTART_REQUIRED' }); return;
        }
      } catch { write({ type: 'error', id: frame.id, code: 'BUILD_INVALID_RESTART_REQUIRED' }); return; }
      controlBusy = 'reload'; backendReady = false;
      control(session.reload().then(async() => {if(!backendReady)throw new Error('BACKEND_NOT_READY');studio.afterReload();await studio.refreshMemory();write({ type: 'reload_result', id: frame.id, status: 'reloaded',data:{...studio.state(),resources:resourceSelection.snapshot(resources)} });}).finally(() => {controlBusy = undefined;}), frame.id);
    } else if (frame.type === 'compact') {
      if (!backendReady) { write({type:'error',id:frame.id,code:'BACKEND_NOT_READY'}); return; }
      if (active) { write({ type: 'error', id: frame.id, code: 'REQUEST_ACTIVE' }); return; }
      if (controlBusy) { write({ type: 'error', id: frame.id, code: 'SESSION_CONTROL_BUSY' }); return; }
      if (frame.customInstructions !== undefined && typeof frame.customInstructions !== 'string') { write({ type: 'error', id: frame.id, code: 'INVALID_COMPACT_INSTRUCTIONS' }); return; }
      controlBusy = 'compact';
      control(session.compact(frame.customInstructions).then(result => write({ type: 'compact_result', id: frame.id, status: 'compacted',data:{result} })).finally(() => {controlBusy = undefined;}), frame.id);
    } else if (frame.type === 'abort') {
      waiting.clear();
      control(session.abort().then(() => write({ type: 'abort_result', id: frame.id, status: 'requested',
        sessionId: identity.sessionId, ownerGeneration })), frame.id);
    }
    else if (frame.type === 'shutdown') { detached = true; control(session.abort(), frame.id); }
    else write({ type: 'error', id: frame.id, code: 'UNKNOWN_COMMAND' });
  };
  const decoder = new JsonFrameDecoder(IMAGE_LIMITS.maxFrameBytes);
  const processFrames = async (frames: ReturnType<JsonFrameDecoder['push']>) => {
    for (const item of frames) {
      if (item.type === 'frame') await handle(item.value, item.bytes);
      else {
        write({ type: 'error', code: item.code, frameNumber: item.frameNumber, bytes: item.bytes });
        // Preserve the current Main host's fail-closed transport lifetime. The
        // decoder bounds and validates bytes; a malformed connection ends here.
        exitCode = 2; detached = true; validationLifetime.abort();
      }
      if (detached) break;
    }
  };
  try {
    if (initialPrompt !== undefined) await handle({ type: 'prompt', text: initialPrompt, id: 'initial' });
    for await (const chunk of process.stdin) {
      await processFrames(decoder.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
      if (detached) break;
    }
    if (!detached) await processFrames(decoder.end());
  } finally {
    // EOF is disconnect, not a cancellation of a scientific job. Pi abort only
    // cancels conversation waiters; the extension closes the bridge disconnect-only.
    detached = true; validationLifetime.abort(); await session.abort(); await active;
    await session.extensionRunner?.emit({ type: 'session_shutdown', reason: 'quit' });
    for(const read of packageReads.values()){clearTimeout(read.timer);read.reject(new Error('OWNER_CLOSED'));}packageReads.clear();
    studio.dispose(); session.dispose(); await output.flush().catch(() => { exitCode = 2; });
    config.writerClean=exitCode===0;
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
  const pluginCompat=JSON.parse(fs.readFileSync(path.join(runtime,'compat/pi-0.87.1-plugin-guard.patch.json'),'utf8'));
  for(const target of compat.files){
    const bytes=fs.readFileSync(path.join(packageRoot,target.path));
    if(!verifyObserverBytes(bytes,target.patchedSha256,pluginCompat.files.find((file:any)=>file.path===target.path)))
      throw new Error('BIOMIND_PI_COMPAT_PATCH_REQUIRED');
  }
  for(const target of pluginCompat.files){
    if(createHash('sha256').update(fs.readFileSync(path.join(packageRoot,target.path))).digest('hex')!==target.patchedSha256)
      throw new Error('BIOMIND_PI_PLUGIN_GUARD_PATCH_REQUIRED');
  }
  const config = readConfig(values.config);
  config.loadedBuild = loadBuildIdentity(runtime, values.mode === 'interactive' ? 'native' : 'json');
  if (config.installRoot && fs.realpathSync(config.installRoot) !== fs.realpathSync(config.loadedBuild.releaseRoot)) throw new Error('BIOMIND_BUILD_INSTALL_ROOT_MISMATCH');
  config.installRoot = config.loadedBuild.releaseRoot;
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
  config.writerLeases=[];config.ownerGeneration=randomUUID();config.writerClean=false;
  const resourceLease = acquirePluginLease({cwd,agentDir:config.agentDir!,python:config.python ?? process.env.BIOMIND_PI_PYTHON ?? config.launcherPython,mode:'shared'});
  config.resourceOwnership={protocol:'linux_flock_v1',mode:'shared',roots:resourceLease.plan.resources.map(row=>row.root),releaseOn:'last_actual_process_exit'};
  try {
  let previousProfile: ExecutionProfile | undefined;
  if (config.session || config.continueSession) {
    const { SessionManager }=await import('@earendil-works/pi-coding-agent');
    let filename=config.session;
    if(config.continueSession){
      // Use Pi's original mtime-based discovery without opening/migrating history.
      const sessionModule=await import(pathToFileURL(path.join(packageRoot,'dist/core/session-manager.js')).href);
      const directory=config.sessionDir??SessionManager.create(cwd).getSessionDir();
      filename=sessionModule.findMostRecentSession(directory,cwd)??undefined;delete config.continueSession;
    }
    if(filename){
      if(!path.isAbsolute(filename))filename=SessionManager.findById(cwd,filename,config.sessionDir)??undefined;
      if(!filename)throw new ProductSessionError('SESSION_NOT_FOUND');
      const header=writerHeader(filename);
      const lease=await acquireSessionWriter({sessionFile:filename,sessionId:header.id,cwd,ownerGeneration:config.ownerGeneration,mode:'resume'});
      config.writerLeases.push(lease);config.session=lease.identity.sessionFile;
      previousProfile=sessionProfile(path.resolve(cwd,config.dataRoot??'.biomind/pi-domain'),header.id);
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
  } finally {
    resourceLease.close();
    // Native returns only after actual CLI exit; JSON returns after abort/dispose.
    if(config.writerClean){
      let failure:unknown;for(const lease of config.writerLeases.reverse()){try{await lease.release();}catch(error){failure??=error;}}if(failure)throw failure;
    }else if(config.writerLeases.length){
      process.stderr.write('SESSION_WRITER_RETAINED_ABNORMAL_EXIT: ownership remains unknown/read-only; no stale claim was cleared.\n');
    }
  }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = await main(); } catch (error) {
    // Never print unfiltered settings/provider exceptions (they can contain URLs
    // with credentials). Full typed fixture assertions live in test evidence.
    if (error instanceof SessionWriterError) process.stderr.write(error.code + '\n');
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
      code: error instanceof ProductSessionError || error instanceof ProjectDirectoryError || error instanceof TerminalInputError || error instanceof SessionWriterError ? error.code : error instanceof Error && /^(?:BIOMIND_BUILD|BIOMIND_PI|RESOURCE|PACKAGE|PACKAGES)_[A-Z_]+$/.test(error.message) ? error.message : 'PI_INITIALIZATION_FAILED',
      ...(error instanceof ProjectDirectoryError || error instanceof ProductSessionError ? { message: error.message } : {}) }) + '\n');
    process.stderr.write('BioMind Pi host failed to initialize or run; no legacy fallback was attempted.\n');
    process.exitCode = 2;
  }
}
