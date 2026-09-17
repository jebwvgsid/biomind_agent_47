# S3 → S2 observation delivery integration

Only the four files in `s3-integration-delta.json` differ from the previously
frozen S3 slice. The common parser, sorter, global NodeContext/authority/core,
main repository and historical witnesses were not changed by this increment.

The real joint test exposed that `_without_control_metadata()` intentionally
removes `source`. New helper observations had placed business file hashes and
read scopes under that reserved name, so initial raw output and immutable
receipts retained them but the next provider request did not. The local
`inspect_inputs()` output projection now publishes VCF/phenotype/Q file
identity as `file_identity`. The parser's internal `source` contract is kept.
No global metadata exemption or parallel evidence schema was introduced.

Final validation: **33 passed, 19 subtests passed**, in one unchanged candidate
process: 24 input tests, 8 real Skill/binding/Plan-effect compilation tests, and
1 actual production graph test. The aggregate parent must rerun its own
cumulative source rather than add counts from earlier candidates.

```bash
env -i PATH=/usr/bin:/bin HOME=/tmp \
  TMPDIR=/tmp/biomind-gwas-s1-s5-20260917/s3_joint_evidence/tmp \
  BIOMIND_DISABLE_DOTENV=1 \
  BIOMIND_OFFLINE_RESULTS=/tmp/biomind-gwas-s1-s5-20260917/s3_joint_evidence/results-final.json \
  BIOMIND_CLI_PROGRESS_ARTIFACTS=/tmp/biomind-gwas-s1-s5-20260917/s3_joint_evidence \
  /public/home/liusiqi/anaconda3/envs/biomind/bin/python -B \
  tests/offline_managed_bootstrap.py /tmp/biomind-gwas-s1-s5-20260917/s3-joint -q \
  tests/test_gwas_input_contracts_s3.py tests/test_gwas_skill_contracts_s3.py \
  tests/test_gwas_input_observation_graph_s3.py
```

Run from the `s3-joint` candidate with its dedicated evidence/tmp directory
created. The bootstrap rejects network/subprocess use except the explicitly
registered tiny approved scripts, supplies synthetic config, and disables
unused memory observation/compression inherited from the graph fixture.

The graph uses the real current GWAS Skill and synthetic VCF/phenotype/Q/TFAM:

1. Real Consultant/Planner/approval select an observation-only initial Plan step.
2. Generate registers the actual inspector root/delegate. Runtime bindings are
   exactly empty at both code reviews; scientific runtime is not required.
3. The approved script executes with the **production-provided env unchanged**.
   Root/delegate hashes and environment paths are checked. Real reconciliation,
   Supervisor and CommitStep accept the scoped observation, with no file output.
4. The next actual provider adapter request includes
   `accepted_execution_outcomes` from `accepted_typed_step_commit`. The test
   parses that exact history and compares its `file_identity` hashes/scopes,
   phenotype IDs and Q mapping to the real helper stdout. The Q remains unknown.
5. An intentionally adverse deterministic provider then attempts Q preparation.
   The real sorter exits 2 with `Q_SAMPLE_MAPPING_UNKNOWN` before opening the
   covariate file. Only the first step has a Commit; no scientific file exists.

Counts: Consultant 1, Planner 1, Generate 2; actual helper process results 0 and
2; two ordinary code approval leaves; one Commit. Only provider/model and human
decision leaves are replacements. A launch harness registers the exact actual
script and environment with the offline audit guard, then calls the real
`execution.executor.run_script_path`; it does not preset a success result or
replace environment selection/binding/reconciliation/Commit. Complete requests,
launches, hashes, receipts and outcomes are in
`gwas-input-observation-joint.json`.

The second response is deliberately inconsistent with the observed scientific
precondition to verify fail-closed helper behavior. This proves the helper
rejects an unwarranted output; it does not prove a real model will choose the
correct scientific response, nor claim the overall two-step Plan completed.
The stream stops after the second Execute failure, before invoking Recovery.

`formal-inputs-final.json` separately preserves the final CLI command on the
four read-only task inputs, their before/after identities and new output field.
It still finds 215 matching ordered samples, no phenotype header, and numeric
215x3 Q with unknown correspondence. VCF header and GFF3 prefix limits remain;
no original task output/report/session or scientific result was written.
