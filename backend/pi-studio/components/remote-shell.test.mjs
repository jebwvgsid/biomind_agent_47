import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { studioControlsAllowed } from "../lib/remote-ui-capabilities.ts";
const source = file => readFileSync(new URL(file, import.meta.url), "utf8");
test("shell capabilities fail closed for missing, failed and remote profiles", () => {
  for (const profile of [null, undefined, {}, {profile:"biomind"}, {profile:"unknown"}, "pi", {error:"unavailable"}]) assert.equal(studioControlsAllowed(profile), false);
  assert.equal(studioControlsAllowed({profile:"pi"}), true);
});
test("source contract: remote shell suppresses Studio settings, command palette, Office and local services", () => {
  const shell = source("./AppShell.tsx");
  for (const component of ["ModelsConfig", "SkillsConfig", "PluginsConfig", "UploadsManager", "CommandPalette", "SettingsPanel", "SkillsPanel"]) {
    const index = shell.lastIndexOf("<" + component);
    assert(index > 0); assert.match(shell.slice(Math.max(0, index - 100), index), /supportsStudioControls/);
  }
  assert.match(shell, /readOnly=\{!supportsStudioControls\}/);
  assert.match(shell, /!supportsStudioControls \? <RemoteFilePreview/);
  assert.match(shell, /supportsStudioControls && terminalOpen/);
  assert.match(shell, /supportsStudioControls \? fileTabs : fileTabs.filter\(tab => tab.kind === "file"\)/);
  for (const marker of ["// Poll the agent-facing browser marker", "// Poll the agent-facing open-file-request marker", "// Report the right-panel active file"]) {
    const part=shell.slice(shell.indexOf(marker)); assert.match(part.slice(part.indexOf("useEffect"), part.indexOf("fetch(")), /if \(!supportsStudioControls\) return/);
  }
  assert.match(source("./ActivityBar.tsx"), /allowStudioControls && ITEMS.slice/);
});
test("source contract: sidebar mutations, file uploads and Git probes are unavailable in remote scope", () => {
  const sidebar=source("./SessionSidebar.tsx"), explorer=source("./FileExplorer.tsx"), preview=source("./RemoteFilePreview.tsx");
  assert.match(sidebar, /const commitRename = useCallback\(async \(\) => \{\s*if \(!allowStudioControls\) return/);
  assert.match(sidebar, /const performDelete = useCallback\(async \(\) => \{\s*if \(!allowStudioControls\) return/);
  assert.match(sidebar, /allowStudioControls && <button[\s\S]*?onClick=\{handleCreateTeam\}/);
  assert.match(explorer, /if \(readOnly \|\| files.length === 0/);
  assert.match(explorer, /if \(readOnly\) return;\s*let cancelled = false;\s*fetchGitStatus/);
  assert.doesNotMatch(preview, /new EventSource|dangerouslySetInnerHTML|\/api\/univer|method: "POST"/);
  assert.match(preview, /return \(\) => abort.abort\(\)/);
});
