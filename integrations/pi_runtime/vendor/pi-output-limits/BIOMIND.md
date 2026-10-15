# BioMind adapter 0.1.0-biomind.1

This is a local, unpublished adaptation of pi-output-limits 0.1.0 at ff06cbc30135c82f2a89a6ff1b420d6759693acd. The npm source `npm:pi-output-limits` remains upstream; it is never rewritten.

Requires Pi 0.87.1 and a BioMind core containing the 2026-10-15 output-limit changes (baseline core commit 5fe00a1806e5c77c9c42b32d8e60aade34aa2ec2, including Python pagination, receipt projection, reference protection and final-message handling). Installing this package cannot install or patch those core changes.

The adapter defaults to 8192 **bytes** / 300 lines. User settings and trusted project settings override those defaults. The existing contract caps the ordinary text body; status, truncation notes and continuation references are outside this body budget. Structured receipts retain their established field/page budgets. Unknown structured tools and user quotations are not blindly sliced.

`dist/output-limits-extension.ts` is bundled by the existing BioMind build, includes the adapter, and imports only Node builtins and the Pi host API. The tarball contains no second Pi runtime or install scripts. A local directory install records a reference and does not copy or delete your source directory. Keep the extracted package at a stable path.

Do not enable upstream and this adapter together. Disable one using the existing native `extensions: []` resource filter (preserving other declaration fields), or explicitly remove its reference before installing the other. No automatic source replacement.

Build with the core runtime npm build, then `npm pack --ignore-scripts`. Check the actual archive file list and SHA256. This candidate is private and has not been pushed or published; remote one-command distribution is not available.
