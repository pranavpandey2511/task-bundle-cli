# Design notes

I treated this assignment as a small evaluation tool, not as the start of a hosted platform. The core workflow is intentionally linear: materialize one exact repository revision, prove the task's test expectations, run one solver, and grade the resulting patch. Typer provides the command surface, Pydantic owns the bundle contract, the Docker CLI owns execution, and SQLite stores the lifecycle ledger. Keeping those boundaries visible made the implementation easier to inspect and left out queues, services, and provider-specific abstractions that do not help a task creator validate a bundle locally.

## Repository and test generality

The CLI does not assume Python or pytest. Each bundle owns a Dockerfile, a working directory, a smoke command, and named shell commands for its pass-to-pass and fail-to-pass tests. This puts language runtimes, package managers, system packages, and test frameworks where they belong: beside the task. The evaluator only understands process exit status and the expected transition. The cost is that task authors must write a sound image and precise commands, but that is a better trade than growing framework detection logic that will always miss unusual repositories.

Repositories are pinned to a full Git commit and cloned into a clean build context. `task init` records the resolved commit, image identity, Dockerfile hash, and platform details. Later commands compare that provenance before trusting a reused image. This is stricter than accepting a branch or tag, and it makes setup slightly less convenient, but a mutable reference would undermine comparisons across machines. Dataset and patch hashes in the included SWE-bench Pro bundle provide the same traceability for evaluator-owned inputs.

## Lifecycle and trust boundary

Validation checks two claims separately. At baseline, existing pass-to-pass tests must pass while each fail-to-pass test must fail. With the hidden test patch and trusted gold patch applied, every test must pass. A solver run repeats the baseline as a preflight, runs the solver in a fresh workspace, captures only its Git diff, then grades that diff in another fresh container with the evaluator's hidden tests. Repetitions expose flaky expectations instead of letting one lucky exit code validate a task.

The gold patch and hidden test patch are trusted evaluator material. They are excluded from the Docker build context and never mounted into the solver container. The CLI streams trusted patches into the relevant running container only when they are needed. Separating baseline, golden, solver, and grading containers costs startup time, but it prevents state leakage and gives each phase a clean repository. It also means a solver cannot learn the answer simply by listing a host-mounted bundle directory.

## Isolation

Solver commands run with networking disabled by default, dropped Linux capabilities, `no-new-privileges`, bounded CPU, memory and process counts, a read-only container root filesystem, and a disposable Docker volume for the writable repository. Temporary and home directories use size-bounded `tmpfs` mounts. There is no host workspace mount and no Docker socket in the container. Network access requires two explicit decisions: the manifest must permit it and the operator must pass `--allow-network`.

This is a practical local boundary, not a claim of formal sandboxing. Docker shares the host kernel, an opted-in network can exfiltrate forwarded secrets, and a malicious image build has more opportunity than a locked-down runtime. A production service should add a dedicated worker host or microVM boundary, image allow-listing and scanning, egress policy, short-lived credentials, and external watchdogs. I kept those controls out of this repository because pretending they exist would be worse than documenting the actual boundary clearly.

## Observability and reproducibility

Every invocation receives a command ID before work begins. The SQLite ledger stores status, exit code, structured events, per-test observations, and artifact metadata. The artifact directory keeps the solver patch, baseline and post-solver reports, stdout and stderr, diffs, and repository snapshots taken at important phase boundaries. `task logs` queries the record, while `task artifacts` verifies stored sizes and SHA-256 hashes before the evidence is trusted. This makes both an infrastructure failure and a poor solver result diagnosable without scraping terminal output.

Reports also carry execution fingerprints derived from the commit, image, manifest, patches, commands, repetition count, runtime limits, and network policy. Before baseline, solver, or grading work begins, the CLI rejects an image whose repository is not clean or whose HEAD differs from the configured commit. Exact tool versions inside a task still depend on its Dockerfile, so authors should pin base-image digests and dependency lockfiles when cross-machine identity matters. Timestamps and container IDs will naturally differ; the fingerprint focuses on inputs that should control behavior. The checked-in `evaluation.json` is deliberately smaller than a full report: it answers which tests passed or failed, while retaining the command ID and hashes needed to locate richer local evidence.

## Deliberate tradeoffs

Calling the Docker CLI through a narrow process runner is less elegant than maintaining a long-lived engine client, but it is transparent, easy to fake in unit tests, and adds no daemon SDK dependency. SQLite is similarly appropriate for one operator on one machine; it provides durable queries and transactions without a service. Neither choice is intended for concurrent remote workers. If that became a requirement, I would preserve the bundle and report schemas while moving orchestration to workers and the ledger to a shared database.

The evaluator currently runs each named test independently and creates fresh containers between trust phases. That favors understandable evidence and isolation over maximum speed. Development can be accelerated safely by reusing content-addressed images, narrowing repetitions while authoring, and selecting individual test commands; final validation should restore the declared repetitions. Batching tests, remote image caches, and parallel execution are reasonable next steps once profiles show that container startup dominates, but they would need to preserve per-test attribution and the clean-state guarantees that make the results useful.
