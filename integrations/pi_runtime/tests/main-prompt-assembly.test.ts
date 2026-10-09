/** Real Pi provider-bound assembly; offline synthetic outputs do not evaluate model behavior. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { TranscriptContext } from '@earendil-works/pi-ai';
import { createHarness, FakeDomain, call, text, clone } from './extension-harness.js';
import { requestFromBranch } from '../extensions/biomind.js';

const { getCurrentTools, getCurrentSystemMessage } = await import(new URL('../../node_modules/@earendil-works/pi-ai/dist/utils/transcript.js', import.meta.url).href);
const STATE_BOUNDARY = '\n当前有效状态与偏好：\n';
const PREFERENCE = 'SYNTHETIC_GLOBAL_PREFERENCE: preserve sample identity and report unknown evidence.';
const PROJECT = 'SYNTHETIC_PROJECT_INSTRUCTION: report only the supplied fixture materials.';
const SKILL_DESCRIPTION = 'SYNTHETIC_SKILL_DESCRIPTION: inspect synthetic QC inputs.';
const SKILL_BODY = 'SYNTHETIC_SKILL_BODY: this is an offline assembly fixture, not a scientific workflow.';
const measure = (value: string) => ({ codePoints: [...value].length, utf16CodeUnits: value.length, utf8Bytes: Buffer.byteLength(value, 'utf8') });
const wire = (value: unknown) => JSON.stringify(value);
type Profile = 'native' | 'protected';

function currentSection(context: TranscriptContext) {
  const sections = context.messages.filter(message => message.role === 'system' && (message as any).sections?.['biomind-current']);
  assert.equal(sections.length, 1, 'exactly one live BioMind current section reaches the provider');
  const current = (sections[0] as any).sections['biomind-current'] as string;
  const split = current.indexOf(STATE_BOUNDARY);
  assert(split > 0);
  return { fixed: current.slice(0, split), state: JSON.parse(current.slice(split + STATE_BOUNDARY.length)) };
}

function checkAssembly(context: TranscriptContext, profile: Profile, maintenance: boolean) {
  const { fixed, state } = currentSection(context);
  const merged = getCurrentSystemMessage(context.messages);
  assert(merged.sections.preamble.startsWith('You are an expert coding assistant operating inside pi,'), 'Pi default base prompt is retained');
  assert(merged.sections.rules.includes('Be concise in your responses'));
  assert(merged.sections.project_context.includes(PROJECT));
  assert.equal(state.execution_profile, profile);
  assert.equal(state.preferences.text, PREFERENCE);
  const tools = getCurrentTools(context.messages).map((tool: any) => tool.name).sort();
  if (maintenance) {
    assert(fixed.includes('当前阶段：记忆维护'));
    assert(fixed.includes('不继续或启动分析任务'));
    assert(!fixed.includes('单一主 agent'));
    assert(!fixed.includes('当前 execution:'));
    assert(!fixed.includes('binding_ref'));
    assert.deepEqual(tools, ['maintenance_submit']);
    return { fixed, state, common: undefined, tools };
  }
  assert(merged.sections.skills.includes(SKILL_DESCRIPTION), 'native Skill metadata is retained');
  assert(fixed.includes('单一主 agent'));
  const marker = `\n当前 execution:${profile}`;
  const modeIndex = fixed.indexOf(marker);
  assert(modeIndex > 0);
  const common = fixed.slice(0, modeIndex);
  assert(!common.includes('当前阶段：记忆维护'));
  assert(/安装[^\n]*确认/.test(common), 'installation confirmation remains a COMMON rule in both profiles');
  assert(common.includes('binding_ref') && common.includes('环境'), 'environment binding remains a COMMON rule in both profiles');
  assert(common.includes('bash'), 'per-call shell environment guidance remains shared');
  assert(!fixed.includes(`当前 execution:${profile === 'native' ? 'protected' : 'native'}`), 'mode rules are mutually exclusive');
  assert(!fixed.includes('当前阶段：记忆维护'));
  for (const name of ['read', 'write', 'edit', 'run_command', 'install_tool', 'select_environment', 'register_environment']) assert(tools.includes(name));
  assert.equal(tools.includes('bash'), profile === 'native');
  assert.equal(tools.includes('request_path_access'), profile === 'protected');
  assert(!tools.includes('handoff_generate')); assert(!tools.includes('maintenance_submit'));
  return { fixed, state, common, tools };
}

/** Only replace the known run IDs and actual message timestamp fields; never generic numbers/text. */
function normalizeRequest(context: TranscriptContext, ids: { sessionId: string; requestId: string }, omitFixedRules: boolean) {
  const replaceIds = (value: string) => value.split(ids.sessionId).join('<SESSION_ID>').split(ids.requestId).join('<REQUEST_ID>');
  const visit = (value: any, key?: string): any => {
    if (key === 'timestamp' && (typeof value === 'number' || typeof value === 'string')) return '<MESSAGE_TIMESTAMP>';
    if (typeof value === 'string') return replaceIds(value);
    if (Array.isArray(value)) return value.map(item => visit(item));
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([name, item]) => {
      if (omitFixedRules && name === 'biomind-current' && typeof item === 'string') {
        const index = item.indexOf(STATE_BOUNDARY); assert(index > 0);
        return [name, '<BIOMIND_FIXED_RULES>' + replaceIds(item.slice(index))];
      }
      return [name, visit(item, name)];
    }));
    return value;
  };
  return visit(clone(context));
}

test('real Pi Main prompt assembly preserves modes, native base, Skills, dynamic state and independent maintenance', async t => {
  const fixtureRoot = process.env.BIOMIND_MAIN_PROMPT_FIXTURE_ROOT ?? await mkdtemp(join(tmpdir(), 'biomind-main-assembly-'));
  const skillRoot = join(fixtureRoot, 'skills');
  await mkdir(join(skillRoot, 'assembly-fixture'), { recursive: true });
  await writeFile(join(skillRoot, 'assembly-fixture', 'SKILL.md'), `---\nname: assembly-fixture\ndescription: ${JSON.stringify(SKILL_DESCRIPTION)}\n---\n${SKILL_BODY}\n`);
  const evidence = process.env.BIOMIND_PI_TEST_EVIDENCE;
  const baseline = process.env.BIOMIND_MAIN_PROMPT_BASELINE_DIR;
  const artifacts: any[] = [];
  const commonByProfile = new Map<Profile, string>();
  for (const profile of ['native', 'protected'] as const) {
    const cwd = join(fixtureRoot, 'project-' + profile);
    await mkdir(cwd, { recursive: true }); await writeFile(join(cwd, 'AGENTS.md'), PROJECT + '\n');
    for (const maintenance of [false, true]) await t.test(`${profile}: ${maintenance ? 'maintenance' : 'ordinary and explicit Skill'}`, async () => {
      const name = `main-assembly-${profile}-${maintenance ? 'maintenance' : 'ordinary'}`;
      const domain = new FakeDomain();
      domain.snapshot = { ...domain.snapshot, preferences: { status: 'loaded', text: PREFERENCE },
        history_enabled: maintenance, memory_mode: maintenance ? 'auto' : 'disabled', maintenance: { enabled: maintenance } };
      domain.receipts.set('memory.maintenance.prepare', { status: 'ready', outbox_id: 'synthetic-assembly-outbox',
        sources: [{ source_ref: 'synthetic:assembly-input', content: 'SYNTHETIC_MAINTENANCE_EVIDENCE' }], prompt: 'SYNTHETIC_MAINTENANCE_FOCUS' });
      domain.receipts.set('memory.maintenance.apply', { status: 'noop', written: [], processed: true });
      const h = await createHarness({ name, cwd, domain, executionProfile: profile, nativePrompt: true, skillPaths: [skillRoot],
        script: maintenance ? [text('SYNTHETIC_TASK_ANSWER'), call('synthetic-maintenance-submit', 'maintenance_submit', { operations: [], summary: 'SYNTHETIC_NO_NEW_FACTS' }), text('SYNTHETIC_MAINTENANCE_END')]
          : [text('SYNTHETIC_INITIAL_ANSWER'), text('SYNTHETIC_SKILL_ANSWER')],
        settings: { compaction: { enabled: false } } });
      try {
        assert.deepEqual(h.resourceLoader.getSkills().diagnostics, []);
        assert.equal(h.resourceLoader.getSkills().skills.length, 1);
        await h.prompt('SYNTHETIC_USER_TASK: Explain the supplied fixture; do not execute scientific computation.');
        if (!maintenance) await h.prompt('/skill:assembly-fixture SYNTHETIC_SKILL_ARGUMENT');
        assert.equal(h.requests.length, maintenance ? 3 : 2);
        const ids = { sessionId: h.manager.getSessionId(), requestId: requestFromBranch(h.manager.getBranch())!.requestId };
        const requestArtifacts = h.requests.map((context, index) => {
          const isMaintenance = maintenance && index > 0;
          const parsed = checkAssembly(context, profile, isMaintenance);
          if (parsed.common) {
            const previous = commonByProfile.get(profile);
            if (previous) assert.equal(parsed.common, previous);
            commonByProfile.set(profile, parsed.common);
          }
          if (!maintenance) {
            assert.equal(wire(context).includes(SKILL_BODY), index === 1, 'Skill body enters only through the actual explicit Skill request');
            if (index === 1) assert(wire(context).includes('SYNTHETIC_SKILL_ARGUMENT'));
          }
          return { index, stage: isMaintenance ? 'maintenance' : 'main', raw: context,
            fixedRules: parsed.fixed, commonRules: parsed.common, currentState: parsed.state, tools: getCurrentTools(context.messages),
            normalized: normalizeRequest(context, ids, false), normalizedWithoutFixedRules: normalizeRequest(context, ids, true),
            sizes: { fixedRules: measure(parsed.fixed), rawProviderContextJson: measure(wire(context)),
              normalizedProviderContextJson: measure(wire(normalizeRequest(context, ids, false))) } };
        });
        const artifact: any = { name, profile, maintenance, fixtureRoot,
          scope: 'Actual provider TranscriptContext from real Pi and production BioMind extension; synthetic replies and domain receipts, no network. Full sizes are serialized provider context JSON including messages/tool declarations, not HTTP wire bytes or tokenizer counts.',
          normalization: 'Only exact known session/request UUID values and message timestamp fields are replaced. Fixed rules are additionally replaced by a named marker only in normalizedWithoutFixedRules. Raw captures are retained unchanged.',
          ids, requests: requestArtifacts };
        if (baseline) {
          const before = JSON.parse(await readFile(join(baseline, name + '-assembly.json'), 'utf8'));
          assert.equal(before.fixtureRoot, fixtureRoot, 'before and after use the same fixed project/Skill paths');
          assert.equal(before.requests.length, requestArtifacts.length);
          artifact.comparison = requestArtifacts.map((request, index) => {
            const old = before.requests[index];
            assert.deepEqual(request.normalizedWithoutFixedRules, old.normalizedWithoutFixedRules, 'outside fixed rules, native assembly, tools, Skill content, preferences and state are unchanged after explicit ID/time normalization');
            if (request.stage === 'maintenance') assert.equal(request.fixedRules, old.fixedRules, 'maintenance duties remain independent and unchanged');
            else assert(request.sizes.fixedRules.utf8Bytes < old.sizes.fixedRules.utf8Bytes, 'ordinary fixed instructions are actually smaller');
            if (request.stage === 'main' && profile === 'protected') {
              const marker = '\n当前 execution:protected';
              assert.equal(request.fixedRules.slice(request.fixedRules.indexOf(marker)), old.fixedRules.slice(old.fixedRules.indexOf(marker)), 'protected-only permissions remain unchanged');
            }
            return { index, stage: request.stage, before: old.sizes, after: request.sizes,
              fixedRulesBytesSaved: old.sizes.fixedRules.utf8Bytes - request.sizes.fixedRules.utf8Bytes,
              normalizedFullBytesSaved: old.sizes.normalizedProviderContextJson.utf8Bytes - request.sizes.normalizedProviderContextJson.utf8Bytes,
              normalizedOutsideFixedRulesEqual: true };
          });
        }
        if (evidence) { await mkdir(evidence, { recursive: true }); await writeFile(join(evidence, name + '-assembly.json'), JSON.stringify(artifact, null, 2)); }
        artifacts.push(artifact);
        assert.deepEqual(h.errors, []);
      } finally { await h.close(); }
    });
  }
  assert.equal(commonByProfile.get('native'), commonByProfile.get('protected'), 'both modes receive the same COMMON rules');
  if (evidence) await writeFile(join(evidence, 'main-prompt-assembly-summary.json'), JSON.stringify({
    scope: 'Wiring and serialized-context sizes only; no measured tokenizer cost, latency, cache hits or model behavior.',
    artifacts: artifacts.map(({ name, requests, comparison }) => ({ name, requests: requests.map((request: any) => ({ index: request.index, stage: request.stage, sizes: request.sizes })), comparison })),
  }, null, 2));
});
