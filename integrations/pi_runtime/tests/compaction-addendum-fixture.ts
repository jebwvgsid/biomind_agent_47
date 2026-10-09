/** Capture the actual synthetic provider boundary used by native Pi summary calls. */
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createAssistantMessageEventStream, type AssistantMessage, type TranscriptContext } from '@earendil-works/pi-ai';
import type { ProviderConfig } from '@earendil-works/pi-coding-agent';
import { assertPairs, clone, createHarness, type HarnessOptions } from './extension-harness.js';

export type SummaryKind = 'ordinary' | 'history' | 'prefix';
export interface CapturedSummaryRequest {
  kind: SummaryKind;
  context: TranscriptContext;
  options: { maxTokens?: number; cacheRetention?: string; sessionId?: string; hasSignal: boolean; signalInitiallyAborted: boolean };
}
export interface SyntheticReply {
  content?: AssistantMessage['content'];
  error?: string;
  aborted?: boolean;
  input?: number;
  output?: number;
}
export function summaryKind(context: TranscriptContext): SummaryKind {
  const system = context.messages.filter(message => message.role === 'system');
  if (!system.some(message => String((message as any).content).includes('You are a context summarization assistant.'))) return 'ordinary';
  return JSON.stringify(context.messages.filter(message => message.role === 'user')).includes('<conversation>') ? 'history' : 'prefix';
}
export function requestText(context: TranscriptContext): string {
  return context.messages.flatMap(message => {
    const content = (message as any).content;
    return typeof content === 'string' ? [content] : Array.isArray(content)
      ? content.filter(part => part.type === 'text').map(part => part.text) : [];
  }).join('\n');
}

export async function createAddendumHarness(name: string,
  respond: (request: CapturedSummaryRequest, signal?: AbortSignal) => SyntheticReply | Promise<SyntheticReply>,
  options: Omit<HarnessOptions, 'provider' | 'script' | 'name'> = {}) {
  const captured: CapturedSummaryRequest[] = [], failures: string[] = [];
  const config: ProviderConfig = {
    api: 'openai-completions', apiKey: 'synthetic-no-real-credentials', baseUrl: 'https://invalid.example.invalid',
    models: [{ id: 'scripted', name: 'scripted', reasoning: false, input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 32000, maxTokens: 2048 }],
    streamSimple(model, context, streamOptions) {
      const request: CapturedSummaryRequest = { kind: summaryKind(context), context: clone(context), options: {
        maxTokens: streamOptions?.maxTokens, cacheRetention: streamOptions?.cacheRetention,
        sessionId: streamOptions?.sessionId, hasSignal: !!streamOptions?.signal, signalInitiallyAborted: streamOptions?.signal?.aborted === true,
      } };
      captured.push(request);
      const stream = createAssistantMessageEventStream();
      queueMicrotask(async () => {
        let reply: SyntheticReply;
        try { assertPairs(context.messages); reply = await respond(request, streamOptions?.signal); }
        catch (error) { failures.push(String(error)); reply = { error: String(error) }; }
        const content = reply.content ?? [], input = reply.input ?? 100, output = reply.output ?? 1;
        const stopReason = reply.aborted ? 'aborted' : reply.error ? 'error' : content.some(part => part.type === 'toolCall') ? 'toolUse' : 'stop';
        const message: AssistantMessage = { role: 'assistant', api: model.api, provider: model.provider, model: model.id,
          content, stopReason, timestamp: Date.now(), ...(reply.error ? { errorMessage: reply.error } : {}),
          usage: { input, output, cacheRead: 0, cacheWrite: 0, totalTokens: input + output,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
        if (stopReason === 'aborted' || stopReason === 'error') stream.push({ type: 'error', reason: stopReason, error: message });
        else stream.push({ type: 'done', reason: stopReason, message });
        stream.end();
      });
      return stream;
    },
  };
  const h = await createHarness({ ...options, name,
    provider: { name: 'synthetic-compaction-addendum', modelId: 'scripted', config } });
  let closed = false;
  return { ...h, captured, failures,
    async close() {
      if (closed) return; closed = true;
      try { await h.close(); }
      finally {
        const directory = process.env.BIOMIND_PI_TEST_EVIDENCE;
        if (directory) {
          await mkdir(directory, { recursive: true });
          await writeFile(join(directory, name + '-provider-wire.json'), JSON.stringify({
            scope: 'Real Pi sessions and production BioMind extension; synthetic provider only, no network or scientific execution.',
            captured, failures,
          }, null, 2));
        }
      }
    },
  };
}
