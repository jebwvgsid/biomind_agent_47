# Confirm the project before ordinary interactive startup

Supersedes the earlier cwd-first user-interface default in
`startup_boundary_20261008.md`. The current user preference is to confirm the
project before choosing or creating a conversation.

`host.ts` now routes interactive TTY startup with no selected project, exact
session, continuation, or resume request to the existing
`runStartupProjectMenu()`. A legal launch directory is offered as a candidate;
it no longer skips confirmation. The existing menu owns open/create, project
validation, conversation selection, and the return-to-project action. The
backend and project resources are still initialized only after selection.
Cancellation exits without creating a conversation or starting the backend.

Explicit `--project-dir` / `project open`, configured or explicit session and
continuation choices, and `--resume` keep their prior behavior. The pre-existing
HOME fallback is retained. Interactive `--prompt` and `--no-session` without a
selected project confirm the directory without adding a conversation menu.
JSON and non-TTY commands do not acquire a menu. HOME remains an invalid
analysis root.

No new selector, project store, configuration source, or Pi patch is added.
Node remains 22.22.0, Pi remains 0.87.1 with observer.6. No existing user session
or scientific job is opened, resumed, or modified by this change.

Validation evidence is stored at
`/public/home/liusiqi/03.毕设/BioMind_Project_Confirmation_20261008/evidence/`.

Validation: PASS — complete isolated `npm run build`; 13 existing entry/session
boundary tests; 6 PTY menu scenarios with 20 checks. Those scenarios
cover a legal cwd, switching project before selecting a conversation, returning,
HOME cancellation, prompt/temporary-session cancellation, and explicit project
or session navigation. Only synthetic projects/sessions were used.

The actual installed `/public/home/liusiqi/.local/bin/biomind` also passed a
separate PTY smoke check: project confirmation came first, cancellation exited
zero, terminal state recovered, and neither project nor agent/session files
were written. The only changed emitted product files, `dist/host.js` and its
source map, were installed from the successful isolated full build. Their
hashes match the tested candidate; the native bundle and observer.6 remain
unchanged. No main-worktree full build or startup-performance comparison was
claimed for this change. No remote push or user-session restart occurred.
