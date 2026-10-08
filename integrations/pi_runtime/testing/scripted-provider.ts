/** Test dependency only: Pi and the production BioMind extension remain real. */
import fs from 'node:fs';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
export function scriptedProvider(filename: string) {
  const steps = JSON.parse(fs.readFileSync(filename, 'utf8')) as any[];
  if (!Array.isArray(steps) || !steps.length) throw new Error('Fixture must be a nonempty list');
  let cursor = 0;
  return {
    api: 'openai-completions' as const, apiKey: 'synthetic-test-only', baseUrl: 'https://invalid.example.invalid',
    models: [{ id: 'scripted', name: 'BioMind synthetic provider', reasoning: false, input: ['text'] as ('text' | 'image')[],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 8192 }],
    streamSimple(model: any, context: any, options: any) {
      const stream = createAssistantMessageEventStream(); const template = steps[cursor++];
      // Explicit fixture opt-in: resolve identities from real production tool
      // receipts, never invent a run_ref or bypass a tool/service operation.
      let entry = template;
      if (template?.resolve_last_run_ref) {
        const receipt = [...context.messages].reverse().filter((item: any) => item.role === 'toolResult')
          .map((item: any) => { try { return JSON.parse(item.content.find((part: any) => part.type === 'text')?.text ?? '{}'); } catch { return {}; } })
          .find((item: any) => item.run_ref);
        if (!receipt?.run_ref) throw new Error('FIXTURE_REAL_RUN_REF_MISSING');
        entry = { ...template, content: template.content.map((part: any) => part.type === 'toolCall'
          ? { ...part, arguments: { ...part.arguments, run_ref: receipt.run_ref,
            ...(part.arguments.after_cursor === '$last_cursor' ? { after_cursor: receipt.next_cursor } : {}) } } : part) };
      }
      // Explicit synthetic trace only; never persist provider headers/options.
      if (steps[0]?.trace_path) fs.appendFileSync(steps[0].trace_path, JSON.stringify({ request: cursor, context }) + '\n');
      queueMicrotask(async () => {
        const message: any = { role: 'assistant', api: model.api, provider: model.provider, model: model.id, timestamp: Date.now(),
          content: entry?.content ?? [], stopReason: entry?.content?.some((part: any) => part.type === 'toolCall') ? 'toolUse' : 'stop',
          usage: entry?.usage ?? { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        const partial: any = { ...message, content: [] };
        try {
          if (!entry || entry.error) throw new Error(entry?.error ?? 'SCRIPT_EXHAUSTED_UNEXPECTED_MODEL_CALL');
          const lastUser = [...(context.messages ?? [])].reverse().find((value: any) => value.role === 'user');
          if (entry.expect_user_contains && !JSON.stringify(lastUser).includes(entry.expect_user_contains)) throw new Error('FIXTURE_USER_EXPECTATION_FAILED');
          const system = JSON.stringify([context.systemPrompt, ...(context.messages ?? []).filter((value: any) => value.role === 'system')]);
          if (entry.expect_system_contains && !system.includes(entry.expect_system_contains)) throw new Error('FIXTURE_SYSTEM_EXPECTATION_FAILED');
          options?.signal?.throwIfAborted();
          stream.push({ type: 'start', partial: structuredClone(partial) });
          for (let index = 0; index < message.content.length; index++) {
            const part = message.content[index];
            partial.content[index] = part.type === 'text' ? { ...part, text: '' } : part;
            if (part.type !== 'text') continue;
            stream.push({ type: 'text_start', contentIndex: index, partial: structuredClone(partial) });
            const chunks: string[] = entry.chunks && index === 0 ? entry.chunks : [part.text];
            if (chunks.join('') !== part.text) throw new Error('FIXTURE_CHUNKS_MUST_MATCH_TEXT');
            for (const delta of chunks) {
              options?.signal?.throwIfAborted();
              partial.content[index].text += delta;
              stream.push({ type: 'text_delta', contentIndex: index, delta, partial: structuredClone(partial) });
              if (entry.delay_ms) await new Promise<void>((resolve, reject) => {
                const abort = () => { clearTimeout(timer); reject(new Error('FIXTURE_ABORTED')); };
                const timer = setTimeout(() => { options?.signal?.removeEventListener('abort', abort); resolve(); }, entry.delay_ms);
                options?.signal?.addEventListener('abort', abort, { once: true });
                if (options?.signal?.aborted) abort();
              });
            }
            stream.push({ type: 'text_end', contentIndex: index, content: part.text, partial: structuredClone(partial) });
          }
          stream.push({ type: 'done', reason: message.stopReason, message }); stream.end();
        } catch (error) {
          message.content = structuredClone(partial.content);
          message.stopReason = options?.signal?.aborted ? 'aborted' : 'error'; message.errorMessage = String(error);
          stream.push({ type: 'error', reason: message.stopReason, error: message }); stream.end();
        }
      });
      return stream;
    },
  };
}
