/** A per-call runtime reference on Pi's existing bash tool. No global activation. */
import { Type } from 'typebox';
import { createBashToolDefinition, createLocalBashOperations, getAgentDir, getShellConfig, SettingsManager,
  type BashToolOptions, type ExtensionContext, type ToolDefinition } from '@earendil-works/pi-coding-agent';

export interface RuntimeBashLaunch {
  command: string;
  env: Record<string, string>;
  launch?: Record<string, unknown>;
}
export type RuntimeBashResolver = (input: { binding_ref: string; command: string; shell: string; base_path?: string },
  signal: AbortSignal | undefined, ctx: ExtensionContext) => Promise<RuntimeBashLaunch>;
type ShellOptions = Pick<BashToolOptions, 'shellPath' | 'commandPrefix'>;

export function bashBudget(requested: number | undefined, configured = process.env.BIOMIND_BASH_DEFAULT_TIMEOUT_SECONDS) {
  const seconds = requested === undefined ? Number(configured ?? 60) : requested;
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds * 1000 > 2_147_483_647) {
    throw new Error('BASH_TIMEOUT_INVALID: finite positive seconds required (maximum 2147483.647)');
  }
  return { requested_seconds: requested ?? null, effective_seconds: seconds,
    source: requested === undefined ? 'host_default' : 'explicit', unit: 'seconds' };
}

export function runtimeBash(resolve: RuntimeBashResolver,
  shellOptions?: (ctx: ExtensionContext) => ShellOptions,
  record?: (receipt: Record<string, unknown>) => void,
  admit?: (ctx: ExtensionContext) => Promise<void>): ToolDefinition {
  const definition = createBashToolDefinition(process.cwd());
  return {
    ...definition,
    description: definition.description + ' Omitted timeout uses a finite host default (60 seconds unless configured). Explicit longer budgets are preserved. Use binding_ref for selected scientific software; select_environment does not globally activate anything.',
    parameters: Type.Object({ ...definition.parameters.properties,
      timeout: Type.Optional(Type.Number({ minimum: 0.001, maximum: 2147483.647, description: 'Seconds; omitted = finite host default, normally 60. Set an explicit appropriate budget for longer work, or use run_command.' })),
      binding_ref: Type.Optional(Type.String({ minLength: 1, description: 'Current-request binding_ref returned by select_environment.' })) }),
    async execute(id, input, signal, update, ctx) {
      await admit?.(ctx);
      signal?.throwIfAborted();
      // Reuse Pi's setting parser and project-trust decision. The product does
      // not override these shell settings in memory; tests may supply the same
      // options directly. Creating the reader never writes settings.
      const settings = shellOptions ? undefined : SettingsManager.create(ctx.cwd, getAgentDir(), { projectTrusted: ctx.isProjectTrusted() });
      const options = shellOptions?.(ctx) ?? { shellPath: settings!.getShellPath(), commandPrefix: settings!.getShellCommandPrefix() };
      const args = input as { command: string; timeout?: number; binding_ref?: string };
      const budget = bashBudget(args.timeout), started = Date.now();
      const local = createLocalBashOperations({ shellPath: options.shellPath });
      const shell = getShellConfig(options.shellPath).shell;
      let launch: Record<string, unknown> | undefined;
      const receipt: Record<string, unknown> = { tool_call_id: id, budget, cwd: ctx.cwd,
        binding_ref: args.binding_ref ?? null, environment_source: args.binding_ref ? 'registered_binding' : 'host',
        backend: 'pi_local_bash', started_at: new Date(started).toISOString(), status: 'preparing' };
      const bound = createBashToolDefinition(ctx.cwd, { ...options, operations: {
        async exec(command, cwd, execution) {
          const prepared = args.binding_ref
            ? await resolve({ binding_ref: args.binding_ref, command, shell, base_path: execution.env?.PATH }, execution.signal, ctx)
            : { command, env: {} };
          execution.signal?.throwIfAborted();
          if (typeof prepared.command !== 'string' || !prepared.command.trim() || !prepared.env ||
              Object.entries(prepared.env).some(([key, value]) => !key || key.includes('=') || typeof value !== 'string')) {
            throw new Error('INVALID_RUNTIME_BASH_LAUNCH');
          }
          const env = { ...execution.env, ...prepared.env };
          launch = prepared.launch;
          receipt.runtime_launch = launch;
          // These are the effective backend arguments, not the original model JSON.
          receipt.backend_timeout_seconds = execution.timeout;
          receipt.environment_prefix = env.CONDA_PREFIX ?? null;
          receipt.status = 'running';
          try {
            const result = await local.exec(prepared.command, cwd, { ...execution, env });
            receipt.exit_code = result.exitCode; receipt.status = 'exited';
            return result;
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            receipt.status = message.startsWith('timeout:') ? 'timed_out' : message === 'aborted' ? 'cancelled' : 'failed';
            // Pi kills the owned process tree and waits for the shell. Escaped or
            // scheduler jobs require their own cancellation receipt.
            receipt.termination = ['timed_out', 'cancelled'].includes(String(receipt.status))
              ? 'pi_process_tree_kill_and_shell_wait_completed' : 'execution_failed';
            throw error;
          }
        },
      } });
      try {
        const result = await bound.execute(id, { ...args, timeout: budget.effective_seconds }, signal, update, ctx);
        receipt.elapsed_ms = Date.now() - started;
        return { ...result, details: { ...result.details, runtime_launch: launch, binding_ref: args.binding_ref, execution: receipt } };
      } catch (error) {
        receipt.elapsed_ms = Date.now() - started;
        if (receipt.status === 'preparing') receipt.status = 'launch_failed';
        throw new Error(`${error instanceof Error ? error.message : String(error)}\nBioMind execution: ${JSON.stringify(receipt)}`);
      } finally {
        receipt.elapsed_ms = Date.now() - started;
        record?.(receipt);
      }
    },
  } as ToolDefinition;
}
