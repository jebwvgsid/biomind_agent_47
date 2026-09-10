# Validation history (complete pytest diagnostics; large source inventories omitted)

## l1-tests.log

```text
F.......                                                                 [100%]
=================================== FAILURES ===================================
____________ test_provenance_projection_does_not_mutate_nested_step ____________

tmp_path = PosixPath('/tmp/pytest-of-liusiqi/pytest-294/test_provenance_projection_doe0')

    def test_provenance_projection_does_not_mutate_nested_step(tmp_path):
        state = fixture_state(tmp_path)
        state["steps"][0]["artifacts"] = [{"artifact_id": "nested", "path": str(tmp_path / "tiny"),
                                           "metadata": {"nested": [1, 2]}}]
        before = deepcopy(state["steps"])
        tracker._provenance_artifact_definitions(state)
>       assert state["steps"] == before
E       AssertionError: assert [{'step_id': ...ts': [], ...}] == [{'step_id': ...ts': [], ...}]
E
E         At index 0 diff: {'step_id': 'inspect-input', 'title': 'Inspect synthetic input', 'action': 'execute', 'inputs': [], 'outputs': [], 'status': 'pending', 'dependencies': [], 'step_effect': {'schema_version': 1, 'produces_observations': [{'identity': 'check'}]}, 'tool': '', 'execution_capability_id': '', 'operation': '', 'capability_contract_hash': '', 'skill_ids': [], 'skills_declared': False, 'workflow_stage_ids': [], 'batch_id': '', 'purpose': 'analysis', 'produces_bindings': [], 'produces_observations': [], 'explicit_side_effects': [], 'workspace_mutations': [], 'depe...
E
E         ...Full output truncated (2 lines hidden), use '-vv' to show

tests/test_runtime_optimization.py:34: AssertionError
=========================== short test summary info ============================
FAILED tests/test_runtime_optimization.py::test_provenance_projection_does_not_mutate_nested_step
1 failed, 7 passed in 0.98s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## l1-tests-2.log

```text
..........F..F..F...F...........                                         [100%]
=================================== FAILURES ===================================
_ ArtifactProvenanceTests.test_artifact_links_step_action_attempt_and_consumers _

self = <test_execution_provenance.ArtifactProvenanceTests testMethod=test_artifact_links_step_action_attempt_and_consumers>

    def test_artifact_links_step_action_attempt_and_consumers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            _, attempt, finished = self._run_action(state, root / "result.tsv", "one\n")
>           artifact_id = finished["produced_artifact_ids"][0]
                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           IndexError: list index out of range

tests/test_execution_provenance.py:145: IndexError
_ ArtifactProvenanceTests.test_missing_artifact_has_structured_unknown_producer _

self = <test_execution_provenance.ArtifactProvenanceTests testMethod=test_missing_artifact_has_structured_unknown_producer>

    def test_missing_artifact_has_structured_unknown_producer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _state(Path(tmpdir))
>           artifact_id = state["steps"][0]["output_artifact_ids"][0]
                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E           IndexError: list index out of range

tests/test_execution_provenance.py:222: IndexError
_ ArtifactProvenanceTests.test_same_path_creates_new_version_without_changing_logical_id _

self = <test_execution_provenance.ArtifactProvenanceTests testMethod=test_same_path_creates_new_version_without_changing_logical_id>

    def test_same_path_creates_new_version_without_changing_logical_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root)
            _, first_attempt, first = self._run_action(state, root / "result.tsv", "one\n")
            _, second_attempt, second = self._run_action(state, root / "result.tsv", "two\n")
            self.assertEqual(first["produced_artifact_ids"], second["produced_artifact_ids"])
>           self.assertNotEqual(first["artifact_version_ids"], second["artifact_version_ids"])
E           AssertionError: [] == []

tests/test_execution_provenance.py:186: AssertionError
_______ RendererStageTests.test_stage_attempt_is_distinct_from_plan_step _______

self = <test_execution_provenance.RendererStageTests testMethod=test_stage_attempt_is_distinct_from_plan_step>

    def test_stage_attempt_is_distinct_from_plan_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = _state(root, steps=[{
                "step_id": "step-1", "title": "renderer", "step_type": "renderer_composite",
                "renderer_id": "renderer.test", "status": "running",
                "outputs": [str(root / "db.bin")],
                "artifacts": [{"artifact_id": "db", "path": str(root / "db.bin")}],
            }])
            manifest_path = root / "manifest.json"
            manifest = {
                "schema_version": 3, "outdir": str(root), "renderer": {"id": "renderer.test"},
                "provenance": {"task_id": "task-a", "step_id": "step-1", "producer_action_id": "render-a"},
                "actions": [{"id": "build", "script": "build.sh", "expected_artifact_ids": ["db"]}],
                "artifacts": [{"artifact_id": "db", "path": "db.bin", "producer": "build.sh"}],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            normalized = register_renderer_manifest(state, "step-1", manifest, manifest_path=manifest_path, task_id="task-a", producer_action_id="render-a")
            stage_id = normalized["stages"][0]["stage_id"]
            action = record_provenance_event(state, "action_bound", {"action": {
                "action_id": "run-build", "step_id": "step-1", "type": "execute",
                "command_role": "primary", "action_source": "manifest_runner",
                "manifest_id": normalized["manifest_id"], "manifest_action_id": "build",
            }})
            attempt = record_provenance_event(state, "attempt_started", {"action_id": action["action_id"]})
            self.assertEqual(attempt["stage_id"], stage_id)
            self.assertNotEqual(attempt["stage_id"], attempt["step_id"])
            output = root / "db.bin"
            output.write_text("built\n", encoding="utf-8")
            finished = record_provenance_event(state, "execution_finished", {
                "attempt_id": attempt["attempt_id"],
                "execution_record": {
                    "execution_id": f"exec-{attempt['attempt_id']}",
                    "exit_code": 0,
                    "execution_status": "succeeded",
                    "command_role": "primary",
                    "produced_artifacts": [str(output)],
                },
            })
>           self.assertEqual(len(finished["produced_artifact_ids"]), 1)
E           AssertionError: 0 != 1

tests/test_execution_provenance.py:308: AssertionError
=========================== short test summary info ============================
FAILED tests/test_execution_provenance.py::ArtifactProvenanceTests::test_artifact_links_step_action_attempt_and_consumers
FAILED tests/test_execution_provenance.py::ArtifactProvenanceTests::test_missing_artifact_has_structured_unknown_producer
FAILED tests/test_execution_provenance.py::ArtifactProvenanceTests::test_same_path_creates_new_version_without_changing_logical_id
FAILED tests/test_execution_provenance.py::RendererStageTests::test_stage_attempt_is_distinct_from_plan_step
4 failed, 28 passed in 28.32s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## l1-tests-3.log

```text
................................                                         [100%]
32 passed in 28.30s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## l2-tests.log

```text
........FFFF......F.....                                                 [100%]
=================================== FAILURES ===================================
___ test_validate_and_context_pair_each_load_once_without_mutating_snapshot ____

tmp_path = PosixPath('/tmp/pytest-of-liusiqi/pytest-297/test_validate_and_context_pair0')

    def test_validate_and_context_pair_each_load_once_without_mutating_snapshot(tmp_path):
        state = fixture_state(tmp_path)
        original = deepcopy(state)
>       agent, scope = local_agent(tmp_path)
                       ^^^^^^^^^^^^^^^^^^^^^

tests/test_runtime_local_reads.py:15:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
tests/runtime_optimization_support.py:22: in local_agent
    cls = next(n for n in source_tree().body if isinstance(n, ast.ClassDef) and n.name == "BioAnalystAgent")
                          ^^^^^^^^^^^^^
tests/runtime_optimization_support.py:16: in source_tree
    return ast.parse((Path(__file__).parents[1] / "bio_agents.py").read_text())
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

source = '"""\nbio_agents.py  ―  BioAnalyst Agent V4\n======================================\n新增 / 重构内容：\n  • MemoryManager   —...nterrupt:\n        print("\\n👋 Bye")\n    except Exception:\n        import traceback\n        traceback.print_exc()\n'
filename = '<unknown>', mode = 'exec', type_comments = False
feature_version = -1

    def parse(source, filename='<unknown>', mode='exec', *,
              type_comments=False, feature_version=None):
        """
        Parse the source into an AST node.
        Equivalent to compile(source, filename, mode, PyCF_ONLY_AST).
        Pass type_comments=True to get back type comments where the syntax allows.
        """
        flags = PyCF_ONLY_AST
        if type_comments:
            flags |= PyCF_TYPE_COMMENTS
        if feature_version is None:
            feature_version = -1
        elif isinstance(feature_version, tuple):
            major, minor = feature_version  # Should be a 2-tuple.
            if major != 3:
                raise ValueError(f"Unsupported major version: {major}")
            feature_version = minor
        # Else it should be an int giving the minor version for 3.x.
>       return compile(source, filename, mode, flags,
                       _feature_version=feature_version)
E         File "<unknown>", line 44676
E           if recovery_generated:
E       IndentationError: unexpected indent

/public/home/liusiqi/anaconda3/envs/biomind/lib/python3.12/ast.py:52: IndentationError
__________ test_actual_candidate_call_passes_isolated_local_snapshot ___________

    def test_actual_candidate_call_passes_isolated_local_snapshot():
>       node = next(n for n in ast.walk(source_tree()) if isinstance(n, ast.Call)
                                        ^^^^^^^^^^^^^
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "_bind_pending_action"
                    and any(k.arg == "plan_state" and isinstance(k.value, ast.Call)
                            and isinstance(k.value.func, ast.Name) and k.value.func.id == "deepcopy"
                            and isinstance(k.value.args[0], ast.Name) and k.value.args[0].id == "active_plan_state"
                            for k in n.keywords))

tests/test_runtime_local_reads.py:32:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
tests/runtime_optimization_support.py:16: in source_tree
    return ast.parse((Path(__file__).parents[1] / "bio_agents.py").read_text())
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

source = '"""\nbio_agents.py  ―  BioAnalyst Agent V4\n======================================\n新增 / 重构内容：\n  • MemoryManager   —...nterrupt:\n        print("\\n👋 Bye")\n    except Exception:\n        import traceback\n        traceback.print_exc()\n'
filename = '<unknown>', mode = 'exec', type_comments = False
feature_version = -1

    def parse(source, filename='<unknown>', mode='exec', *,
              type_comments=False, feature_version=None):
        """
        Parse the source into an AST node.
        Equivalent to compile(source, filename, mode, PyCF_ONLY_AST).
        Pass type_comments=True to get back type comments where the syntax allows.
        """
        flags = PyCF_ONLY_AST
        if type_comments:
            flags |= PyCF_TYPE_COMMENTS
        if feature_version is None:
            feature_version = -1
        elif isinstance(feature_version, tuple):
            major, minor = feature_version  # Should be a 2-tuple.
            if major != 3:
                raise ValueError(f"Unsupported major version: {major}")
            feature_version = minor
        # Else it should be an int giving the minor version for 3.x.
>       return compile(source, filename, mode, flags,
                       _feature_version=feature_version)
E         File "<unknown>", line 44676
E           if recovery_generated:
E       IndentationError: unexpected indent

/public/home/liusiqi/anaconda3/envs/biomind/lib/python3.12/ast.py:52: IndentationError
_______________ test_fake_provider_wait_refresh_and_cas_conflict _______________

tmp_path = PosixPath('/tmp/pytest-of-liusiqi/pytest-297/test_fake_provider_wait_refres0')

    def test_fake_provider_wait_refresh_and_cas_conflict(tmp_path):
        first = fixture_state(tmp_path, "first")
        second = fixture_state(tmp_path, "second")
>       agent, scope = local_agent(tmp_path)
                       ^^^^^^^^^^^^^^^^^^^^^

tests/test_runtime_local_reads.py:52:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
tests/runtime_optimization_support.py:22: in local_agent
    cls = next(n for n in source_tree().body if isinstance(n, ast.ClassDef) and n.name == "BioAnalystAgent")
                          ^^^^^^^^^^^^^
tests/runtime_optimization_support.py:16: in source_tree
    return ast.parse((Path(__file__).parents[1] / "bio_agents.py").read_text())
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

source = '"""\nbio_agents.py  ―  BioAnalyst Agent V4\n======================================\n新增 / 重构内容：\n  • MemoryManager   —...nterrupt:\n        print("\\n👋 Bye")\n    except Exception:\n        import traceback\n        traceback.print_exc()\n'
filename = '<unknown>', mode = 'exec', type_comments = False
feature_version = -1

    def parse(source, filename='<unknown>', mode='exec', *,
              type_comments=False, feature_version=None):
        """
        Parse the source into an AST node.
        Equivalent to compile(source, filename, mode, PyCF_ONLY_AST).
        Pass type_comments=True to get back type comments where the syntax allows.
        """
        flags = PyCF_ONLY_AST
        if type_comments:
            flags |= PyCF_TYPE_COMMENTS
        if feature_version is None:
            feature_version = -1
        elif isinstance(feature_version, tuple):
            major, minor = feature_version  # Should be a 2-tuple.
            if major != 3:
                raise ValueError(f"Unsupported major version: {major}")
            feature_version = minor
        # Else it should be an int giving the minor version for 3.x.
>       return compile(source, filename, mode, flags,
                       _feature_version=feature_version)
E         File "<unknown>", line 44676
E           if recovery_generated:
E       IndentationError: unexpected indent

/public/home/liusiqi/anaconda3/envs/biomind/lib/python3.12/ast.py:52: IndentationError
_________________ test_refresh_boundaries_remain_in_production _________________

    def test_refresh_boundaries_remain_in_production():
>       source = ast.unparse(source_tree())
                             ^^^^^^^^^^^^^

tests/test_runtime_local_reads.py:84:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
tests/runtime_optimization_support.py:16: in source_tree
    return ast.parse((Path(__file__).parents[1] / "bio_agents.py").read_text())
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

source = '"""\nbio_agents.py  ―  BioAnalyst Agent V4\n======================================\n新增 / 重构内容：\n  • MemoryManager   —...nterrupt:\n        print("\\n👋 Bye")\n    except Exception:\n        import traceback\n        traceback.print_exc()\n'
filename = '<unknown>', mode = 'exec', type_comments = False
feature_version = -1

    def parse(source, filename='<unknown>', mode='exec', *,
              type_comments=False, feature_version=None):
        """
        Parse the source into an AST node.
        Equivalent to compile(source, filename, mode, PyCF_ONLY_AST).
        Pass type_comments=True to get back type comments where the syntax allows.
        """
        flags = PyCF_ONLY_AST
        if type_comments:
            flags |= PyCF_TYPE_COMMENTS
        if feature_version is None:
            feature_version = -1
        elif isinstance(feature_version, tuple):
            major, minor = feature_version  # Should be a 2-tuple.
            if major != 3:
                raise ValueError(f"Unsupported major version: {major}")
            feature_version = minor
        # Else it should be an int giving the minor version for 3.x.
>       return compile(source, filename, mode, flags,
                       _feature_version=feature_version)
E         File "<unknown>", line 44676
E           if recovery_generated:
E       IndentationError: unexpected indent

/public/home/liusiqi/anaconda3/envs/biomind/lib/python3.12/ast.py:52: IndentationError
_ StateContractAuditStoreTests.test_consumer_inventory_has_no_authoritative_alias_reader _

self = <test_state_contract_audit_v2.StateContractAuditStoreTests testMethod=test_consumer_inventory_has_no_authoritative_alias_reader>

    def test_consumer_inventory_has_no_authoritative_alias_reader(self) -> None:
        report = build_report()

>       self.assertTrue(report["summary"]["passed"], report["violations"])
E       AssertionError: False is not true : [{'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_adjudicate_post_review_recovery', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_build_resume_runner_state', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_knowledge_claim_registry', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_knowledge_conflict_update', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_resolve_resume_node', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_structured_final_artifacts', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_task_spec_for_state', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_validate_generate_context', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}, {'code': 'CANONICAL_TASKSPEC_NOT_FIRST', 'file': 'bio_agents.py', 'function': '_wiki_terminal_snapshot', 'passed': False, 'canonical_position': -1, 'first_compatibility_position': -1}]

tests/test_state_contract_audit_v2.py:21: AssertionError
=========================== short test summary info ============================
FAILED tests/test_runtime_local_reads.py::test_validate_and_context_pair_each_load_once_without_mutating_snapshot
FAILED tests/test_runtime_local_reads.py::test_actual_candidate_call_passes_isolated_local_snapshot
FAILED tests/test_runtime_local_reads.py::test_fake_provider_wait_refresh_and_cas_conflict
FAILED tests/test_runtime_local_reads.py::test_refresh_boundaries_remain_in_production
FAILED tests/test_state_contract_audit_v2.py::StateContractAuditStoreTests::test_consumer_inventory_has_no_authoritative_alias_reader
5 failed, 19 passed in 15.83s
OFFLINE_CANDIDATE_UNCHANGED=False
```

## l2-tests-2.log

```text
........................                                                 [100%]
24 passed in 32.44s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## l2-tests-3.log

```text
...........                                                              [100%]
11 passed in 6.54s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## l2-tests-4.log

```text
............                                                             [100%]
12 passed in 7.13s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## l2-tests-final.log

```text
............                                                             [100%]
12 passed in 6.88s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## benchmark-prepare.log

```text
.                                                                        [100%]
1 passed in 0.71s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## benchmark-before.log

```text
.                                                                        [100%]
1 passed in 20.41s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## benchmark-after.log

```text
.                                                                        [100%]
1 passed in 20.81s
OFFLINE_CANDIDATE_UNCHANGED=False
```

## benchmark-after-2.log

```text
.                                                                        [100%]
1 passed in 20.64s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## e1-tests.log

```text
...................................                                      [100%]
35 passed in 9.17s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## e1-tests-final.log

```text
..............F                                                          [100%]
=================================== FAILURES ===================================
_____ test_actual_generate_material_is_provider_visible_and_snapshot_bound _____

    def test_actual_generate_material_is_provider_visible_and_snapshot_bound():
        # Execute the exact production template/material statements with a fake
        # message class, without importing config, startup or provider clients.
        from langchain_core.messages import HumanMessage
        from agent_core.workflow.generate_handoff import (GenerateEpisodeBasis,
            start_generate_reasoning_episode, freeze_generate_provider_request)
        from runtime_optimization_support import source_tree
        tree = source_tree()
        assign = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "formal_emission_template" for t in n.targets))
        insertion = next(n for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
                         and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "insert"
                         and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id == "generate_material_messages"
                         and "formal_emission_template" in ast.unparse(n))
        messages = []
        namespace = dict(declared_effect_emission_contract=declared_effect_emission_contract,
            generate_approved_snapshot={"steps": [{"step_id": "a", **effect(["approved-a"], ["side-a"])}]},
            current_step_id="a", generate_material_messages=messages, HumanMessage=HumanMessage, json=json)
        with patch("agent_core.plan_tracker.load_plan_state", side_effect=AssertionError("unexpected state load")):
            exec(compile(ast.Module(body=[assign, insertion], type_ignores=[]), "production-template-material", "exec"), namespace)
        payload = json.loads(messages[0].content)
        assert payload["declarations"]["produces_artifacts"][0]["identity"] == "approved-a"
        assert payload["python_template"] and "diagnostic" in " ".join(payload["rules"])
        # All episode-start branches consume the same exact material list.
        starts = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id in {"start_generate_after_recovery", "start_generate_after_authority_change", "start_next_generate_step", "start_generate_reasoning_episode"}
                  and n.lineno > assign.lineno and n.lineno < assign.lineno + 110]
        assert starts and all(any(k.arg == "inherited_materials" and ast.unparse(k.value) == "generate_material_messages" for k in n.keywords)
                              for n in starts)


        basis = GenerateEpisodeBasis(schema_version=1, task_id="fake-task", task_revision="task-v1",
            plan_id="approved-plan", plan_revision="plan-v1", selected_skill_revision="skills-v1",
            step_id="a", execution_identity="fake-attempt", recovery_seed_id="")
>       episode = start_generate_reasoning_episode(basis=basis, system_message="Generate approved action",
            seed_message={"current_step": "a"}, inherited_materials=messages)

tests/test_declared_effect_emission.py:122:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
agent_core/workflow/generate_handoff.py:704: in start_generate_reasoning_episode
    return _new_episode(
agent_core/workflow/generate_handoff.py:685: in _new_episode
    inherited_materials=_inherited_materials(inherited_materials),
                        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

values = [HumanMessage(content='{"available": true, "declarations": {"explicit_side_effects": [{"identity": "side-a", "required...ative."], "schema_version": 1}', additional_kwargs={}, response_metadata={}, name='approved_effect_emission_template')]

    def _inherited_materials(
        values: Sequence[FrozenGenerateMessage | BaseMessage],
    ) -> tuple[FrozenGenerateMessage, ...]:
        result: list[FrozenGenerateMessage] = []
        for index, value in enumerate(values):
            if isinstance(value, FrozenGenerateMessage):
                item = value
            elif isinstance(value, BaseMessage):
                if not str(getattr(value, "id", None) or "").strip():
>                   raise GenerateEpisodeError(
                        "GENERATE_INHERITED_MATERIAL_ID_REQUIRED", str(index)
                    )
E                   agent_core.workflow.generate_handoff.GenerateEpisodeError: GENERATE_INHERITED_MATERIAL_ID_REQUIRED: 0

agent_core/workflow/generate_handoff.py:623: GenerateEpisodeError
=========================== short test summary info ============================
FAILED tests/test_declared_effect_emission.py::test_actual_generate_material_is_provider_visible_and_snapshot_bound
1 failed, 14 passed in 12.62s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## e1-tests-3.log

```text
...............                                                          [100%]
15 passed in 6.21s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## final-targeted-tests.log

```text
............................................................             [100%]
60 passed in 42.96s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## benchmark-final-after.log

```text
.                                                                        [100%]
1 passed in 21.70s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## benchmark-template.log

```text
.                                                                        [100%]
1 passed in 0.83s
OFFLINE_CANDIDATE_UNCHANGED=True
```

## e1-freshness-tests.log

```text
.............................                                            [100%]
29 passed in 14.73s
OFFLINE_CANDIDATE_UNCHANGED=True
```
