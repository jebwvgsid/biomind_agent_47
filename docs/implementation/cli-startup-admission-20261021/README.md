# CLI startup admission contention

Two CLI processes can briefly contend for the same user resource admission gate even when both need only shared resource leases. Previously, the second process immediately received `BUSY`, and the host converted that error to `PI_INITIALIZATION_FAILED`. An isolated reproduction with production build `94ddac30b6803e12` confirmed this path. The original incident did not retain its underlying exception, so this fixes the reproduced defect without claiming retrospective certainty about that incident.

Shared startup now waits for admission gates for at most 5,000 ms, using one monotonic deadline across all gates. Every attempt checks that deadline, including the first attempt on a subsequent gate, even if the lock has become available. The helper's existing 15-second process timeout remains unchanged. Resource locks and exclusive/settings management admission still use immediate, nonblocking acquisition.

The JSON host preserves `BUSY` with `stage` (`resource_admission` or `resource_lock`) and `waitMs` (the configured wait budget, not measured elapsed time). Interactive users receive a fixed explanation and retry guidance. Diagnostics do not include the original exception, credentials or configured paths. Other unknown errors keep the existing redaction policy.

The existing lease protocol remains in place: descriptors are validated before acquisition, ordered admission prevents conflicting initialization, and failure closes this process's descriptors. No lock file is deleted to claim ownership and no `LOCK_UN` is introduced. Inherited resource leases remain held until the last actual holder closes its descriptor.

Regression coverage includes real kernel contention followed by release, a shared deadline across multiple gates, expiry and late wake-up, invalid wait values, and descriptor validation. Independent Node workers also verify simultaneous shared ownership and protection after one exits. Actual JSON host tests check both busy stages, safe error frames, absence of session/domain initialization, and descriptor cleanup.

Validation results are recorded in `VALIDATION.json`. All fixtures use isolated directories and issue no model requests. This commit changes source and tests only; production CLI launchers, the running Studio service, and the installed Windows client are not switched to a new build. A production rollout must publish and verify a new immutable release rather than editing the existing release directory.
