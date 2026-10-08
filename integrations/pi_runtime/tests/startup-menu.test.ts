import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { SessionManager } from '@earendil-works/pi-coding-agent';
import { createHistorySessionSelector } from '../session-launcher.js';

test('all-history entry immediately shows another project from an empty launch directory', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'biomind-startup-all-'));
  const previous = process.env.PI_CODING_AGENT_DIR;
  let picker: Awaited<ReturnType<typeof createHistorySessionSelector>> | undefined;
  try {
    const cwd = path.join(root, 'launch'), project = path.join(root, 'project'), agent = path.join(root, 'agent');
    for (const directory of [cwd, project, agent]) fs.mkdirSync(directory);
    process.env.PI_CODING_AGENT_DIR = agent;
    const saved = SessionManager.create(project);
    saved.appendMessage({ role: 'user', content: 'GLOBAL_HISTORY_TARGET', timestamp: Date.now() });
    saved.appendMessage({ role: 'assistant', content: [{ type: 'text', text: 'fixture' }],
      api: 'openai-completions', provider: 'fixture', model: 'fixture', timestamp: Date.now(), stopReason: 'stop',
      usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } });
    const filename = saved.getSessionFile()!, before = fs.readFileSync(filename);
    let selected: string | undefined;
    picker = await createHistorySessionSelector(cwd, undefined,
      { initialScope: 'all', onSelect: value => { selected = value; } });
    for (let attempt = 0; attempt < 200 && !picker.component.getSessionList().getSelectedSessionPath(); attempt++)
      await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(picker.component.getSessionList().getSelectedSessionPath(), filename);
    assert.match(picker.component.render(140).join('\n'), /Resume Session \(All\)/);
    picker.component.handleInput('\r');
    assert.equal(selected, filename);
    assert.deepEqual(fs.readFileSync(filename), before);
  } finally {
    picker?.dispose();
    if (previous === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = previous;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
