import test from 'node:test';
import { readdirSync } from 'node:fs';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, stat, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHarness, FakeDomain, call, text, assertPairs, deadline } from './extension-harness.js';
import { nativeRead } from '../native-read.js';
import { loadSkillsFromDir } from '@earendil-works/pi-coding-agent';

async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), 'pi-native-skills-'));
  const cwd = path.join(root, '分析 项目'); const skills = path.join(root, '外部 skills');
  await mkdir(cwd); await mkdir(path.join(skills, 'fixture', 'references'), { recursive: true });
  const skill = path.join(skills, 'fixture', 'SKILL.md');
  await writeFile(skill, '---\nname: fixture\ndescription: DESCRIPTION_A\n---\nBODY_PRIVATE_A\nRead references/ref.txt\n');
  await writeFile(path.join(skills, 'fixture', 'references', 'ref.txt'), 'REFERENCE_PRIVATE_A\n');
  return { root, cwd, skills, skill };
}
async function authorize(filename: string) {
  const s = await stat(filename, { bigint: true });
  return { status: 'completed', path: filename, device: String(s.dev), inode: String(s.ino), mtime_ns: String(s.mtimeNs) };
}
function domain() { const d = new FakeDomain(); d.receipts.set('files.read_authorize', (p: any) => authorize(p.path)); return d; }
function serialized(value: any) { return JSON.stringify(value); }
function tools(h: any) { return h.session.getActiveToolNames().sort(); }
function checkTools(h: any) { const names = tools(h); assert(names.includes('read')); for (const old of ['read_file','read_skill','list_skills','write_file','bash']) assert(!names.includes(old), old); }

test('P1 native catalog metadata, explicit expansion, read reference and current toolsets', async () => {
  const f = await fixture(); const d = domain();
  const h = await createHarness({ name: 'P1-catalog-explicit', ...f, domain: d, skillPaths: [f.skills], nativePrompt: true,
    script: [text('catalog only'), call('ref', 'read', { path: path.join(f.skills, 'fixture/references/ref.txt') }), text('loaded'), call('handoff', 'handoff_generate', { goal: 'toolset only' }), text('Generate'), text('new Consult')] });
  try {
    checkTools(h); await h.prompt('catalog');
    const first = serialized(h.requests[0]); assert(first.includes('DESCRIPTION_A')); assert(!first.includes('BODY_PRIVATE_A')); assert(!first.includes('REFERENCE_PRIVATE_A'));
    await h.prompt('/skill:fixture explicit argument');
    const explicit = serialized(h.requests[1]); assert(explicit.includes('BODY_PRIVATE_A')); assert(explicit.includes('explicit argument'));
    assert(serialized(h.requests[2]).includes('REFERENCE_PRIVATE_A')); checkTools(h);
    await h.prompt('handoff'); checkTools(h); assert(tools(h).includes('run_command')); assert(tools(h).includes('write')); assert(tools(h).includes('edit'));
    await h.prompt('new'); checkTools(h); for (const name of ['run_command', 'write', 'edit']) assert(!tools(h).includes(name));
    assert(d.calls.some(x => x.method === 'files.read_authorize' && x.privileged && (x.params.trusted_skill_dirs as string[]).includes(path.dirname(f.skill))));
  } finally { await h.close(); }
});

test('P1 native reload refreshes metadata/additions/body; disabled skill stays explicit-only; collisions diagnosed', async () => {
  const f = await fixture();
  const h = await createHarness({ name: 'P1-reload', ...f, domain: domain(), skillPaths: [f.skills], nativePrompt: true, script: [text('before'), text('after'), text('explicit hidden')] });
  try {
    await h.prompt('before');
    await writeFile(f.skill, '---\nname: fixture\ndescription: DESCRIPTION_B\n---\nBODY_PRIVATE_B\n');
    for (const [folder, header] of [['hidden','name: hidden\ndescription: HIDDEN_DESC\ndisable-model-invocation: true'], ['zduplicate','name: fixture\ndescription: COLLISION'], ['missing','name: missing']] as const) {
      await mkdir(path.join(f.skills,folder)); await writeFile(path.join(f.skills,folder,'SKILL.md'), `---\n${header}\n---\nHIDDEN_BODY\n`);
    }
    await h.session.reload(); checkTools(h); await h.prompt('after');
    const after = serialized(h.requests.at(-1)); assert(after.includes('DESCRIPTION_B')); assert(!after.includes('HIDDEN_DESC'));
    assert(h.resourceLoader.getSkills().diagnostics.length >= 2);
    await h.prompt('/skill:hidden explicit'); assert(serialized(h.requests.at(-1)).includes('HIDDEN_BODY'));
    assert.equal(h.getDomainCreationCount(), 1);
  } finally { await h.close(); }
});

test('P1 official read Unicode line paging, 400/401/1000, empty/missing, PNG and symlink race guard', async () => {
  const f = await fixture(); const long = path.join(f.cwd, '中文 空格.txt');
  await writeFile(long, Array.from({length:1100}, (_,i)=>`${i+1}:中文`).join('\n'));
  await writeFile(path.join(f.cwd,'empty'), '');
  const png = path.join(f.cwd,'image.png'); await writeFile(png, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC', 'base64'));
  const tool = nativeRead(f.cwd, authorize);
  const run = (args: any) => tool.execute('read', args, undefined, undefined, { cwd: f.cwd });
  for (const n of [400,401,1000]) { const r = await run({path:long,limit:n}); assert(r.content[0].type==='text' && r.content[0].text.includes(`${n}:中文`)); }
  const p = await run({path:long,offset:401,limit:1}); assert(p.content[0].type==='text' && p.content[0].text.startsWith('401:中文'));
  assert((await run({path:'empty'})).content[0].type==='text');
  await assert.rejects(run({path:'missing'}));
  const image = await run({path:png}); assert(image.content.some(x=>x.type==='image'));
  await symlink(long,path.join(f.cwd,'link')); await assert.rejects(run({path:'link'}));
  const scopedAlias = nativeRead(f.cwd, async requested => ({...await authorize(long), requested_path: requested}));
  const aliased = await scopedAlias.execute('trusted-alias',{path:'link',limit:1},undefined,undefined,{cwd:f.cwd});
  assert(aliased.content[0].type==='text' && aliased.content[0].text.startsWith('1:中文'));
  const wrongRequest = nativeRead(f.cwd, async requested => ({...await authorize(long), requested_path: requested + '-wrong'}));
  await assert.rejects(wrongRequest.execute('forged',{path:long},undefined,undefined,{cwd:f.cwd}), /READ_AUTHORIZATION_REQUIRED/);
  const bad = nativeRead(f.cwd, async name => ({...await authorize(name), inode:'0'}));
  await assert.rejects(bad.execute('changed',{path:long},undefined,undefined,{cwd:f.cwd}), /FILE_CHANGED/);
});

test('P1 S08 actual Pi compaction then reread Skill and reference with complete pairs', async () => {
  const f = await fixture(); const h = await createHarness({ name: 'P1-S08', ...f, domain: domain(), nativePrompt:true, skillPaths:[f.skills],
    script: [call('body-before','read',{path:f.skill}),call('ref-before','read',{path:path.join(f.skills,'fixture/references/ref.txt')}),text('before '+ 'method evidence '.repeat(700)),text('summary of allowed facts'),call('body-after','read',{path:f.skill}),call('ref-after','read',{path:path.join(f.skills,'fixture/references/ref.txt')}),text('after')] });
  try {
    await h.prompt('read and remember'); await deadline(h.session.compact());
    assert(h.manager.getBranch().some(x=>x.type==='compaction'));
    await writeFile(path.join(f.skills,'fixture/references/ref.txt'),'REFERENCE_AFTER_COMPACT');
    await h.prompt('reread after compaction');
    assert(serialized(h.requests.at(-1)).includes('REFERENCE_AFTER_COMPACT')); h.requests.forEach(x=>assertPairs(x.messages)); checkTools(h);
  } finally { await h.close(); }
});

test('P1 all current Skills load with official Pi diagnostics and unique names', () => {
  const root = path.resolve(import.meta.dirname, '../../../..', 'skills');
  const result = loadSkillsFromDir({ dir: root, source: 'explicit' });
  const manifests = readdirSync(root, { recursive: true, withFileTypes: true }).filter(entry => entry.isFile() && entry.name === 'SKILL.md');
  assert(manifests.length > 0); assert.equal(result.skills.length, manifests.length);
  assert.equal(new Set(result.skills.map(x=>x.name)).size, manifests.length);
  assert.deepEqual(result.diagnostics,[]);
});
