/** Pi owns the schema, edit algorithm, queue and rendering. The existing domain
 * authority owns approval, descriptor-safe CAS and durable operation receipts. */
import { createEditToolDefinition, createWriteToolDefinition } from '@earendil-works/pi-coding-agent';

export interface MutationBridge {
  replay(input: Record<string, unknown>): Promise<Record<string, any>>;
  snapshot(absolutePath: string): Promise<{ path: string; content: string; sha256: string; exists: boolean }>;
  commit(args: Record<string, unknown>): Promise<Record<string, any>>;
  readVersion(absolutePath: string): string | undefined;
  rememberVersion(absolutePath: string, sha256: string): void;
}

export function nativeMutation(kind: 'write' | 'edit', cwd: string, bridge: MutationBridge) {
  const definition = kind === 'write' ? createWriteToolDefinition(cwd) : createEditToolDefinition(cwd);
  return { ...definition,
    async execute(id: string, args: any, signal: AbortSignal | undefined, update: any, ctx: any) {
      // Receipts are checked before running even the native editing algorithm.
      const input = { native_tool: kind, native_input: args, call_id: id };
      signal?.throwIfAborted();
      const replay = await bridge.replay(input);
      if (replay.status !== 'new') {
        if (replay.status !== 'completed') throw new Error(replay.error ?? 'MUTATION_OUTCOME_UNKNOWN');
        return { content: [{ type: 'text' as const, text: JSON.stringify(replay) }], details: { biomind_receipt: replay } };
      }
      let before: Awaited<ReturnType<MutationBridge['snapshot']>> | undefined;
      let receipt: Record<string, any> | undefined;
      const capture = async (absolutePath: string) => {
        signal?.throwIfAborted();
        before = await bridge.snapshot(absolutePath);
        signal?.throwIfAborted();
        if (before.path !== absolutePath) throw new Error('MUTATION_PATH_MISMATCH');
        if (kind === 'edit' && !before.exists) throw new Error('FILE_NOT_FOUND');
        if (kind === 'write' && before.exists) {
          const observed = bridge.readVersion(absolutePath);
          if (observed === undefined) throw new Error('FILE_READ_REQUIRED: use read before replacing an existing file');
          if (observed !== before.sha256) throw new Error('FILE_CHANGED: use read again before replacing an existing file');
        }
      };
      const commit = async (absolutePath: string, content: string) => {
        signal?.throwIfAborted();
        if (kind === 'write') await capture(absolutePath);
        if (!before || before.path !== absolutePath) throw new Error('MUTATION_SNAPSHOT_REQUIRED');
        receipt = await bridge.commit({ ...input, path: absolutePath, content, expected_sha256: before.sha256 });
        if (receipt.status !== 'completed') throw new Error(receipt.error ?? `MUTATION_NOT_COMPLETED: ${receipt.status}`);
        bridge.rememberVersion(absolutePath, receipt.sha256);
      };
      const tool = kind === 'write'
        ? createWriteToolDefinition(cwd, { operations: {
            // Pi calls mkdir before writeFile. Do not create anything here:
            // the approved domain commit creates parents under the same permit.
            async mkdir() { signal?.throwIfAborted(); },
            writeFile: commit,
          } })
        : createEditToolDefinition(cwd, { operations: {
            access: capture,
            async readFile(absolutePath) {
              if (!before || before.path !== absolutePath) throw new Error('MUTATION_SNAPSHOT_REQUIRED');
              return Buffer.from(before.content, 'utf8');
            },
            writeFile: commit,
          } });
      const result = await tool.execute(id, args, signal, update, ctx);
      return { ...result, details: { ...result.details, biomind_receipt: receipt } };
    },
  };
}
