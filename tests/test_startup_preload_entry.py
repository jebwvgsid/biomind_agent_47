from agent_core import pi_entry

def test_exec_preloads_diagnostics_without_changing_public_host_or_input(monkeypatch, tmp_path):
    runtime = tmp_path / 'runtime'; original = ['/synthetic/node', str(runtime / 'dist/host.js'), '--mode', 'json', '--prompt', 'literal $(never-run)']
    monkeypatch.setattr(pi_entry, 'RUNTIME', runtime)
    monkeypatch.setattr(pi_entry, 'build_pi_command', lambda args: (original, tmp_path))
    calls = []
    assert pi_entry.launch_pi([], execute=lambda exe, argv: calls.append((exe, argv))) == 0
    assert calls == [('/synthetic/node', ['/synthetic/node', '--import', str(runtime / 'dist/startup-preload.js'), *original[1:]])]
    assert original[1] == str(runtime / 'dist/host.js')
