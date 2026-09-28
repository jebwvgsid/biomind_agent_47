# Authoring template (not a discoverable Skill)

```yaml
---
name: example-method
description: Describe when to use this scientific method and its actual inputs and outputs.
compatibility: Requires the software documented in references/dependencies.md; does not install automatically.
---
```


# Method

Record selection conditions, input columns, output files and scientific assumptions here.
Resolve scripts/references/assets from this SKILL.md location. Keep sample inputs and
outputs relative to the analysis project; never write results into the installation.
Do not assume SKILL_DIR is automatically set: resolve it from the actual Skill location.

```bash
python "${SKILL_DIR}/scripts/run_example.py" --input "${INPUT}" --output "${OUTPUT}"
```

Replace this example with the real script interface. Describe dependencies in
references/dependencies.md and use standard requirements/environment files when appropriate.
Do not include local environment names, credentials, private runtime objects, model tool
protocols or compulsory planning gates. Preserve authorship and applicable licenses.
