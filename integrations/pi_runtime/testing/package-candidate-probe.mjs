/** Offline native Pi package discovery/reload; does not bind a session or start Python. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { syncBuiltinESMExports } from 'node:module';
import childProcess from 'node:child_process';
import { DefaultResourceLoader, SettingsManager } from '@earendil-works/pi-coding-agent';

const [candidateRoot, output] = process.argv.slice(2);
assert(candidateRoot && output, 'usage: package-candidate-probe.mjs CANDIDATE_ROOT OUTPUT.json');
const root = process.env.BIOMIND_PI_TEST_ROOT;
assert(root && path.isAbsolute(root), 'synthetic test root required');
const cwd = path.join(root, 'package-project'); const agentDir = path.join(root, 'package-agent');
await fs.mkdir(path.join(cwd, '.pi'), { recursive: true }); await fs.mkdir(agentDir, { recursive: true });
const packages = ['biomind-bioinformatics-skills', 'biomind-pi-extension'].map(name => path.join(path.resolve(candidateRoot), name));
// Same packages declared globally and locally. Pi performs precedence/deduplication.
await fs.writeFile(path.join(agentDir, 'settings.json'), JSON.stringify({ packages }));
await fs.writeFile(path.join(cwd, '.pi', 'settings.json'), JSON.stringify({ packages }));
const settings = SettingsManager.create(cwd, agentDir, { projectTrusted: true });
const loader = new DefaultResourceLoader({ cwd, agentDir, settingsManager: settings,
  noPromptTemplates: true, noThemes: true, noContextFiles: true });
let childSpawns = 0;
childProcess.spawn = () => { childSpawns++; throw new Error('PACKAGE_DISCOVERY_MUST_NOT_SPAWN_DOMAIN'); };
syncBuiltinESMExports();
const runs = [];
for (let round = 0; round < 2; round++) {
  await loader.reload();
  const loaded = loader.getExtensions(); const skills = loader.getSkills();
  assert.deepEqual(loaded.errors, []);
  assert.equal(loaded.extensions.length, 1, 'one explicit extension, no helper autoload or duplicate registration');
  const extension = loaded.extensions[0];
  const toolNames = [...extension.tools.keys()];
  assert(toolNames.includes('read') && toolNames.includes('write') && toolNames.includes('edit'));
  assert.equal(new Set(toolNames).size, toolNames.length);
  assert.equal(skills.skills.length, 88);
  assert.equal(new Set(skills.skills.map(skill => skill.name)).size, 88);
  assert.deepEqual(skills.diagnostics, []);
  runs.push({ round, extensions: loaded.extensions.map(value => value.path), toolNames,
    skillCount: skills.skills.length, diagnostics: skills.diagnostics, serviceProcessesStarted: childSpawns });
}
assert.equal(childSpawns, 0, 'loading/reloading resources must not launch a second domain service');
const peers = Object.fromEntries(['@earendil-works/pi-coding-agent', '@earendil-works/pi-ai', '@earendil-works/pi-agent-core', 'typebox']
  .map(name => [name, import.meta.resolve(name)]));
for (const packagePath of packages) {
  const manifest = JSON.parse(await fs.readFile(path.join(packagePath, 'package.json'), 'utf8'));
  assert(!manifest.dependencies); assert(!manifest.scripts); assert.equal(manifest.private, true);
  await assert.rejects(fs.access(path.join(packagePath, 'node_modules')));
}
await fs.writeFile(output, JSON.stringify({ status: 'PASS', source: 'actual Pi 0.87.1 DefaultResourceLoader',
  runs, hostPeerResolution: peers, installations: 0, domainProcesses: 0, modelCalls: 0,
  scope: 'package discovery and extension registration/reload only; user runtime installation NOT_RUN' }, null, 2));
console.log('PASS package discovery/reload: 88 Skills, 1 extension, no duplicate tools or Python processes');
