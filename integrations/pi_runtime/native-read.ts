/** Pi owns read behavior; BioMind owns authorization and CAS provenance. */
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createReadToolDefinition, detectSupportedImageMimeTypeFromFile } from '@earendil-works/pi-coding-agent';

export function nativeRead(cwd: string, authorize: (path: string) => Promise<Record<string, any>>) {
  const definition = createReadToolDefinition(cwd);
  return { ...definition,
    async execute(id: string, args: any, signal: AbortSignal | undefined, update: any, ctx: any) {
      let handle: Awaited<ReturnType<typeof open>> | undefined;
      let receipt: Record<string, any> | undefined;
      let digest: string | undefined;
      try {
        const tool = createReadToolDefinition(cwd, { operations: {
          async access(absolutePath) {
            signal?.throwIfAborted();
            receipt = await authorize(absolutePath);
            signal?.throwIfAborted();
            if (receipt.status !== 'completed' || (receipt.requested_path ?? receipt.path) !== absolutePath) throw new Error('READ_AUTHORIZATION_REQUIRED');
            // Domain stat walks directories with O_NOFOLLOW. Bind the open file
            // to that exact inode, closing the stat/open parent-symlink race.
            const opened = await open(receipt.path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
            if (signal?.aborted) { await opened.close(); signal.throwIfAborted(); }
            handle = opened;
            const info = await handle.stat({ bigint: true });
            if (!info.isFile() || info.nlink !== 1n || info.dev !== BigInt(receipt.device)
                || info.ino !== BigInt(receipt.inode) || info.mtimeNs !== BigInt(receipt.mtime_ns)
                || await realpath(`/proc/self/fd/${handle.fd}`) !== receipt.path) throw new Error('FILE_CHANGED_OR_OUT_OF_SCOPE');
          },
          async detectImageMimeType() {
            if (!handle) throw new Error('READ_AUTHORIZATION_REQUIRED');
            return detectSupportedImageMimeTypeFromFile(`/proc/self/fd/${handle.fd}`);
          },
          async readFile() {
            if (!handle) throw new Error('READ_AUTHORIZATION_REQUIRED');
            signal?.throwIfAborted();
            const bytes = await handle.readFile();
            digest = createHash('sha256').update(bytes).digest('hex');
            if (receipt?.expected_sha256 && receipt.expected_sha256 !== digest) throw new Error('FILE_CHANGED: pinned source');
            return bytes;
          },
        } });
        const result = await tool.execute(id, args, signal, update, ctx);
        // The extension privately retains the exact bytes' version for native
        // write CAS. Official schemas have no model-supplied approval/hash field.
        return { ...result, details: { ...result.details, ...(digest ? { sha256: digest } : {}),
          ...(digest && !receipt?.read_only_source ? { write_path: receipt?.path } : {}) } };
      } finally { await handle?.close(); }
    },
  };
}
