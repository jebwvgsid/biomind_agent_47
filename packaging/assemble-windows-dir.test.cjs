"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

test("existing Windows output is rejected before moving files or writing evidence", t => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "biomind-assembly-guard-"));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const output = path.join(temporary, "existing-payload");
  fs.mkdirSync(output);
  fs.writeFileSync(path.join(output, "keep.txt"), "existing-artifact");
  const evidence = path.join(temporary, "receipt.json");
  const result = spawnSync(process.execPath, [path.join(__dirname, "assemble-windows-dir.cjs"),
    "--output", output, "--stage", path.join(temporary, "unused-stage"), "--evidence", evidence],
    { encoding: "utf8", timeout: 15000 });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /fresh Windows output directory/);
  assert.equal(fs.readFileSync(path.join(output, "keep.txt"), "utf8"), "existing-artifact");
  assert.deepEqual(fs.readdirSync(temporary), ["existing-payload"]);
  assert.equal(fs.existsSync(evidence), false);
});
