import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { scriptedProvider } from '../testing/scripted-provider.js';

test('narrow fixture stream preserves text, tool steps, completion and error', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-fixture-import-'));
  try {
    const file = path.join(root, 'steps.json');
    fs.writeFileSync(file, JSON.stringify([
      { content: [{ type: 'text', text: 'hello' }], chunks: ['he', 'llo'] },
      { content: [{ type: 'toolCall', id: 'read-1', name: 'read', arguments: { path: '/synthetic' } }] },
      { error: 'SYNTHETIC_FAILURE' },
    ]));
    const provider = scriptedProvider(file);
    const model = { ...provider.models[0], api: provider.api, provider: 'biomind-fixture' };
    const first = provider.streamSimple(model, { messages: [] }, {});
    const events = []; for await (const event of first) events.push(event);
    assert.equal(events.filter(x => x.type === 'text_delta').map((x: any) => x.delta).join(''), 'hello');
    assert.equal((await first.result()).stopReason, 'stop');
    const tool = provider.streamSimple(model, { messages: [] }, {});
    for await (const _ of tool) { /* Drain the actual public event stream. */ }
    const toolResult = await tool.result();
    assert.equal(toolResult.stopReason, 'toolUse');
    assert.deepEqual(toolResult.content, [{ type: 'toolCall', id: 'read-1', name: 'read', arguments: { path: '/synthetic' } }]);
    const error = provider.streamSimple(model, { messages: [] }, {});
    for await (const _ of error) { /* Error must end the stream too. */ }
    assert.equal((await error.result()).stopReason, 'error');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('preload and imported diagnostic copies share origin, whitelist and bounded output', () => {
  const url = new URL('../startup-diagnostics.js', import.meta.url).href;
  const preload = new URL('../startup-preload.js', import.meta.url).pathname;
  const script = `const {startup}=await import(${JSON.stringify(url)});startup.mark('main','end',{build:'build-one',prompt:'PRIVATE'});for(let i=0;i<1000;i++)startup.mark('resources','begin');`;
  const result = spawnSync(process.execPath, ['--import', preload, '--input-type=module', '-e', script, 'json'], {
    encoding: 'utf8', env: { PATH: process.env.PATH, BIOMIND_STARTUP_ATTEMPT_ID: 'fixture-attempt' }, timeout: 10000,
  });
  assert.equal(result.status, 0); assert.equal(result.stdout, '');
  const lines = result.stderr.trim().split('\n');
  assert.equal(lines.length, 64); assert.ok(Buffer.byteLength(result.stderr) <= 32768);
  assert.ok(!result.stderr.includes('PRIVATE'));
  const records = lines.map(line => JSON.parse(line.slice('[biomind-startup-v1] '.length)));
  assert.equal(records[0].stage, 'node_start'); assert.equal(records[1].stage, 'main');
  assert.ok(records.every(row => row.attemptId === 'fixture-attempt'));
  assert.ok(records.every((row, i) => i === 0 || row.elapsedMs >= records[i-1].elapsedMs));
});
