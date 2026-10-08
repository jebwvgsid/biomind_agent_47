/** BioMind-only adapter for the locked Pi 0.87.1 terminal lifecycle.
 * Copied byte-for-byte into Pi by apply-pi-observer.mjs; no independent TUI.
 */
import fs from 'node:fs';
import path from 'node:path';

export const isBioMindTerminal = () => process.env.BIOMIND_PI_UI_COMMANDS === '1';
const shellQuote = value => `'${String(value).replaceAll("'", "'\"'\"'")}'`;

export function formatBioMindResumeCommand(manager, isTTY = process.stdout.isTTY, environment = process.env) {
  if (!isTTY || !manager?.isPersisted()) return undefined;
  const filename = manager.getSessionFile();
  const cwd = manager.getCwd();
  if (!filename || !cwd || !fs.existsSync(filename)) return undefined;
  // A file path is unambiguous even with a custom session directory. The product
  // entry validates its embedded project before starting another runtime.
  const args = ['biomind', '--project-dir', shellQuote(cwd), '--session', shellQuote(filename)];
  // Reuse the host's resolved launch sources, also used by /project restarts.
  // These are file/executable paths, never credentials or serialized settings.
  for (const [flag, key] of [['--pi-config', 'BIOMIND_PI_CONFIG_PATH'],
    ['--pi-node', 'BIOMIND_PI_NODE'], ['--pi-model-record', 'BIOMIND_PI_MODEL_RECORD']]) {
    const value = environment[key];
    if (typeof value === 'string' && value) args.push(flag, shellQuote(value));
  }
  return args.join(' ');
}

export function bioMindCrashInstructions(owner, agentDir) {
  const resume = formatBioMindResumeCommand(owner.sessionManager);
  return `诊断记录：${path.join(agentDir, 'crashes.json')}\n${resume
    ? `恢复已落盘的会话记录：${resume}\n未落盘的最后输入不保证可恢复；此命令不会自动续算科研作业。`
    : '当前没有可确认的持久会话，不能承诺恢复；请选择项目后重新启动 BioMind。'}`;
}

// Do not turn a file/tool EIO into a terminal error: only the owned stdin
// ReadStream calls the handler installed below.
function terminalSnapshot() {
  const snapshot = { source: 'process.stdin', fd: process.stdin.fd ?? 0,
    code: undefined, syscall: undefined, pid: process.pid, ppid: process.ppid,
    stdinTTY: Boolean(process.stdin.isTTY), stdoutTTY: Boolean(process.stdout.isTTY),
    rawBefore: Boolean(process.stdin.isRaw), phase: 'interactive' };
  if (process.platform === 'linux') {
    try {
      const stat = fs.readFileSync('/proc/self/stat', 'utf8');
      const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
      Object.assign(snapshot, { pgid: Number(fields[2]), sid: Number(fields[3]),
        ttyNumber: Number(fields[4]), foregroundPgid: Number(fields[5]) });
      snapshot.tty = fs.readlinkSync('/proc/self/fd/0');
      const status = fs.readFileSync('/proc/self/status', 'utf8');
      for (const [key, field] of [['sigttinIgnored', 'SigIgn'], ['sigttinBlocked', 'SigBlk']]) {
        const mask = status.match(new RegExp(`^${field}:\\s*([a-f0-9]+)`, 'mi'))?.[1];
        if (mask) snapshot[key] = Boolean(BigInt(`0x${mask}`) & (1n << 20n));
      }
    } catch { /* A platform snapshot is evidence, never a startup requirement. */ }
  }
  return snapshot;
}

function errorText(error) {
  return error instanceof Error ? error.stack ?? `${error.name}: ${error.message}` : String(error);
}
function diagnosticError(error, diagnostic) {
  const result = new Error(error instanceof Error ? error.message : String(error));
  result.stack = `${errorText(error)}\nBioMind terminal diagnostic: ${JSON.stringify(diagnostic)}`;
  return result;
}
function writeStderr(text) {
  return new Promise(resolve => {
    let settled = false;
    const finish = () => { if (!settled) { settled = true; clearTimeout(deadline); resolve(); } };
    // Only a blocked/broken output stream may reach this deadline. A successful
    // write callback completes immediately; no arbitrary sleep precedes exit.
    const deadline = setTimeout(finish, 1000);
    try { process.stderr.write(text, finish); } catch { finish(); }
  });
}

export function reportBioMindUncaught(owner, error, agentDir) {
  const saved = owner.recordCrash('uncaught_exception', error);
  const text = `BioMind 因未处理异常退出：\n${errorText(error)}\n${saved
    ? bioMindCrashInstructions(owner, agentDir) : '诊断记录写入失败；以上为原始异常。'}\n`;
  // Last-resort global exception handling remains synchronous and fatal. It
  // never resumes the event loop or starts asynchronous session recovery.
  try { fs.writeSync(2, text); } catch { /* stderr itself may be gone. */ }
}

export function installBioMindTerminalInputHandler(owner, { agentDir, killTrackedDetachedChildren }) {
  if (!isBioMindTerminal()) return undefined;
  const onError = error => {
    if (owner.isShuttingDown) return;
    owner.isShuttingDown = true;
    const diagnostic = terminalSnapshot();
    diagnostic.code = error?.code;
    diagnostic.syscall = error?.syscall;
    diagnostic.cleanup = [];
    process.stdin.pause();
    owner.unregisterSignalHandlers();
    // Keep guards until process exit: repeated stdin errors or restore writes
    // must not re-enter Pi's global fatal handler while controlled cleanup runs.
    const ignoreFurtherInput = () => {};
    const outputError = failure => { if (diagnostic.cleanup.length < 8) diagnostic.cleanup.push(`terminal output: ${errorText(failure)}`); };
    process.stdin.on('error', ignoreFurtherInput);
    process.stdout.on('error', outputError);
    process.stderr.on('error', outputError);
    try { killTrackedDetachedChildren(); } catch (failure) { diagnostic.cleanup.push(errorText(failure)); }
    try { owner.stop('resume-hint'); }
    catch (failure) {
      diagnostic.cleanup.push(`terminal restore: ${errorText(failure)}`);
      // stop() may fail before reaching the renderer. The renderer itself owns
      // raw mode/cursor/alternate-screen restoration; do not reimplement it.
      try { owner.ui.stop(); } catch (restoreFailure) { diagnostic.cleanup.push(errorText(restoreFailure)); }
    }
    diagnostic.rawAfter = Boolean(process.stdin.isRaw);
    void (async () => {
      let deadline;
      try {
        await Promise.race([
          Promise.resolve().then(() => owner.runtimeHost.dispose()),
          new Promise((_, reject) => { deadline = setTimeout(() => reject(new Error('session cleanup deadline exceeded')), 2000); }),
        ]);
      } catch (failure) { diagnostic.cleanup.push(`session dispose: ${errorText(failure)}`); }
      finally { clearTimeout(deadline); }
      const saved = owner.recordCrash('terminal_input', diagnosticError(error, diagnostic));
      await writeStderr(`BioMind 终端输入失败，已停止当前交互：${error?.code ?? 'UNKNOWN'} ${error?.syscall ?? 'read'}\n${saved
        ? bioMindCrashInstructions(owner, agentDir) : `诊断记录写入失败：${errorText(error)}`}\n`);
      process.exit(1);
    })().catch(failure => {
      // Cleanup failure is also terminal; never continue analysis after it.
      try { fs.writeSync(2, `BioMind 终端退出清理失败：${errorText(failure)}\n`); } catch { }
      process.exit(1);
    });
  };
  process.stdin.on('error', onError);
  return () => process.stdin.off('error', onError);
}
