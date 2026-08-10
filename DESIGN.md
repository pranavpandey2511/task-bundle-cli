# Design notes

## Scope

Task Bundle CLI is a local judge for coding tasks. It materializes one pinned repository revision, proves the task's baseline and reference behavior, runs a solver, grades the captured patch, and keeps the evidence needed to review the result. It intentionally avoids hosted-platform concerns such as accounts, queues, remote workers, and a second execution service.

The bundle owns repository-specific behavior: its Dockerfile, smoke command, test commands, evaluator patches, and allowed solution paths. The engine owns the repeatable lifecycle, solver/evaluator separation, isolation, patch policy, evidence, and user-facing errors. This keeps the CLI language-neutral without hiding the fact that task authors are responsible for precise, trustworthy test commands.

| Component                 | Responsibility                                          |
| ------------------------- | ------------------------------------------------------- |
| Typer and Pydantic        | CLI parsing plus versioned input/output contracts.      |
| Lifecycle modules         | Initialize, validate, solve, and grade.                 |
| Git and Docker adapters   | Exact source state and disposable execution.            |
| SQLite and artifact files | Durable events, attempts, logs, hashes, and provenance. |
| Reporting layer           | Diagnostics, static HTML review, and evidence export.   |

## What a task bundle contains

A bundle is a small, reviewable task package, not a copy of the target repository. For example, the included Ansible task has this shape:

```text
examples/swe-bench-pro-ansible/
├── task.json                 # pinned repository, tests, limits, patch policy
├── description.md            # problem given to a solver
├── environment/Dockerfile    # reproducible evaluator runtime
├── gold.patch                # known-good reference change
└── tests/
    ├── hidden.patch          # evaluator-only tests absent from the base commit
    └── solver-view.patch     # removes evaluator files from solver source
```

`task.json` declares which tests must already pass and which must change from fail to pass. The bundle also names the narrow paths a candidate may modify. This lets the engine use the same lifecycle for Python, Node, Go, Rust, or another stack while task authors keep ownership of the repository-specific commands and test contract.

The second checked-in bundle, `examples/swe-bench-pro-ansible-safe-eval/`, deliberately uses one PASS_TO_PASS and one FAIL_TO_PASS selector. Both live in one evaluator-owned file, so its solver-view patch deletes that file completely and initialization proves the path and both markers are absent from the sanitized filesystem and synthetic Git history. Its small Python unit-test workload uses a 2 GB container ceiling; this avoids oversubscribing a shared local VM without weakening the engine's isolation controls.

## Decisions at a glance

These choices define what the task author must provide and what the CLI guarantees in return:

1. **Pin every task to exact inputs.** A task names a full Git commit and uses a schema-validated `task.json`. The CLI snapshots and hashes the trusted bundle files before a lifecycle command starts. This prevents a moving branch, an incomplete commit ID, or a mid-run file edit from silently changing what is evaluated. The tradeoff is that task authors must provide explicit, immutable inputs.

2. **Let the bundle describe how its repository works.** The bundle supplies its Dockerfile, dependency setup, smoke command, and test commands. The CLI does not need built-in knowledge of pytest, npm, Cargo, or another framework; it only runs the declared commands and records their outcomes. This is what makes the execution model language-neutral, but the task author remains responsible for making those commands correct and reproducible.

3. **Build separate evaluator and solver images.** The evaluator image contains the complete trusted source needed to grade a solution. The solver image removes selected evaluator files, the original Git history, remotes, and other protected material before the solver starts. A solver therefore cannot inspect the hidden tests or reference patch through its filesystem. The cost is an additional image build and more disk usage.

4. **Treat selected test files as entirely evaluator-owned.** A file containing a selected PASS_TO_PASS or FAIL_TO_PASS check is protected as one unit rather than trying to hide individual functions or lines. Initialization can then prove that the whole path and its markers are absent from the solver image. This gives a simple rule that works across languages, but public and hidden checks cannot share the same file.

5. **Restrict what a candidate patch may change.** Each bundle allow-lists the implementation files or directories that a valid solution may touch. Evaluator files are always protected, and explicit deny rules can carve sensitive paths out of a broader allowed directory. This prevents a solver from replacing tests or runner configuration to manufacture a pass, but the policy must be maintained when the legitimate solution surface changes.

6. **Keep model access on the trusted host.** The optional OpenRouter agent is driven by the CLI on the host; its API key and network traffic never enter the solver container. The `patch` and `stub` solvers provide offline alternatives that use the same grading path. This protects credentials and preserves one evaluation boundary, while live agent runs remain model-dependent and may consume external API credits.

7. **Reuse one evaluator within a phase by default, with strict isolation available.** The default `phase` mode creates one evaluator container and disposable work volume per phase and repetition, then runs every selected test sequentially inside it. Baseline and post-solver always remain separate. `test-attempt` mode retains a fresh container for every individual test attempt when cross-test state is a known risk. This makes RL rollouts substantially cheaper while keeping the stronger diagnostic mode explicit.

8. **Generate reports from recorded evidence, not a second execution service.** Lifecycle commands store events, logs, test observations, hashes, and patches in the local evidence ledger. Static HTML reports only render that recorded data; they never rerun tests or invoke a solver. Review stays simple and auditable, but reports are local evaluator artifacts and can contain private test information.

9. **Start a configured local Docker provider on demand.** When the Docker CLI reports that its daemon is unavailable, the engine starts an explicitly selected Colima or Docker Desktop provider. With an unconfigured default context it discovers an installed provider, preferring lightweight Colima when both are present, and waits for Docker readiness before resuming the original command. An environment override selects Docker Desktop or disables startup. Auto-discovered providers use a command-scoped Docker context, and the CLI never invokes `docker context use`; provider startup may still create or update its own context. A non-Colima `DOCKER_HOST` remains authoritative and is never replaced by discovery.

Together, these decisions keep repository-specific knowledge inside the task bundle while the CLI provides the same isolation, validation, grading, and evidence lifecycle for every task.

## Lifecycle and correctness

`task init` clones the exact commit, checks submodules and repository cleanliness, and builds an evaluator image from the complete source. It applies `tests/solver-view.patch` in temporary staging, removes protected files and all original Git metadata, creates a remote-free synthetic Git baseline, and builds a separate solver image. Initialization accepts those images only after verifying their immutable IDs and confirming the solver image contains no protected paths, declared test markers, foreign Git metadata, or remotes.

`task validate` proves the baseline truth table before a solver is invoked:

| Phase                         | PASS_TO_PASS | FAIL_TO_PASS |
| ----------------------------- | ------------ | ------------ |
| Unmodified baseline repository | pass         | fail         |

Only exit code `0` is a pass. Configured assertion exits, normally `1`, are failures; timeouts and unexpected nonzero exits remain distinct errors. Validation defaults to one repetition. When repetitions are increased, every repetition starts with a fresh evaluator and all observations must agree, so a flaky test or broken runner cannot accidentally validate a task.

Evaluator isolation is separate from repetition count. In `phase` mode, one phase/repetition container runs the P2P commands followed by the F2P commands. A stateful test can therefore influence a later test inside that phase. Task authors should use self-cleaning tests and switch to `--evaluator-isolation test-attempt` when certifying a new task, investigating order dependence, or executing checks known to mutate shared state. The execution fingerprint, JSON result, and HTML report record both choices.

`task run` repeats the baseline before invoking one solver adapter. `agent` is the default and is the only adapter that needs optional OpenRouter configuration; the host gives that model structured tools for the sanitized container. `patch` instead grades a locally supplied patch, while `stub` deliberately makes no change to exercise the unresolved path. Every adapter produces the same review boundary: the engine captures a binary/full-index Git patch, destroys the solver container, rejects protected or out-of-policy paths, then grades the patch in fresh evaluator containers. A run resolves only when every post-solver observation is a stable pass.

For example, `task run my-task --solver patch --candidate-patch fix.patch` is a fully local, reproducible positive-control path. `task run my-task --solver agent --model provider/model-id` is an optional live attempt that can consume credits and sends the problem plus model-requested source excerpts to OpenRouter. `task run my-task --solver stub` is useful when reviewing failure messages and reports without a candidate change.

## Test secrecy and trust boundary

The system has an explicit trust model:

| Trusted | Untrusted |
| --- | --- |
| Task author, manifest, Dockerfile, evaluator commands, CLI, and local Docker daemon | Solver, candidate patch, and code executed from that patch |

### Keeping evaluator material secret

The manifest, gold patch, hidden-test patch, solver-view patch, and reviewer evidence stay outside the solver container. Hidden tests add evaluator-only files, while the solver-view patch removes selected tests that already exist in the base repository. Public tests remain available to the solver.

During initialization, the CLI scans the sanitized filesystem, environment, and synthetic Git history. It rejects the solver image if a protected path or marker is found, or if the scan cannot complete.

Test secrecy and patch policy protect different things:

- **Secrecy** stops the solver from reading selected evaluator tests before solving.
- **Patch policy** stops it from changing tests, runner configuration, or unrelated files to manufacture a pass.

`candidate.allowed_patch_paths` should therefore name only the legitimate implementation surface. `candidate.disallowed_patch_paths` can exclude sensitive descendants from a broader allowed directory. Deny rules win, evaluator-owned paths are always forbidden, and path globs are not supported.

## Runtime isolation

Solver and evaluator containers run with:

- no host bind mounts, Docker socket, or runtime network;
- a read-only root filesystem and disposable work volume;
- bounded temporary storage and an ephemeral home directory;
- CPU, memory, PID, and wall-clock limits;
- all Linux capabilities dropped and `no-new-privileges` enabled.

OpenRouter requests run from the trusted host process, so the API key never enters the container. Evaluator operations use validated absolute tool paths outside the candidate-writable repository and `/tmp`.

This is strong isolation for a local tool, not hostile multi-tenant security. Containers still share the host kernel, and candidate code may influence assertions executed inside the same language process. High-assurance checks should run critical assertions from an external trusted process. A hosted service would also need disposable workers or microVMs, image scanning, host-level egress controls, quotas, watchdogs, and short-lived credentials.

## Reproducibility and determinism

The CLI separates repeatable inputs from run-specific evidence.

### Repeatable inputs

- Repositories are pinned to full Git commits.
- Build identity includes the CLI and staging versions, repository and commit, bundle ID, workdir, Dockerfile, solver-view patch, and secrecy rules.
- Validation and run snapshot and hash the manifest, description, Dockerfile, and evaluator patches before doing any work.
- Immutable image IDs are recorded and verified; cached image tags are only shortcuts.
- Provenance records runtime limits, repetitions, evaluator isolation, image IDs, solver adapter, network policy, model request, and candidate-input hash.

Model generation is not deterministic. For agent runs, the evidence also records the routed model, steps, and token usage, but the captured candidate patch and its evaluator results remain the reviewable boundary.

Exact behavior across machines still depends on the bundle: base images should be pinned by digest, dependencies locked, and architecture and locale controlled. Timestamps, command IDs, container IDs, and durations describe a run; they are not reproducible inputs.

## Evidence and observability

Every lifecycle command receives a sortable command ID. SQLite stores its sanitized arguments, status, exit code, timeline, and test observations. Content-hashed artifact files store input snapshots, build and test logs, patch checks, repository snapshots, solver output, provenance, and final JSON.

`task report` verifies those artifacts and explains mismatches, flakiness, timeouts, and runner errors without executing untrusted code. Every lifecycle result, including a failure, also gets an immutable static HTML report at:

```text
.taskbundle/commands/<command-id>/report.html
```

`.taskbundle/reports/latest.html` opens the newest report, and `.taskbundle/reports/index.html` lists the full history. Reports and ZIP exports can contain hidden-test names, logs, diffs, source excerpts, and model output, so they must remain on the trusted reviewer side.

## UX and error model

The main commands are `new`, `init`, `validate`, `run`, `report`, and `doctor`.

- Parser errors show command usage and help.
- Lifecycle failures provide a stable error kind and exit code, a short explanation, an actionable hint when available, a command ID, and evidence paths.
- Human-readable output goes to stderr and ends with useful next commands.
- `--json` emits one machine-readable report on stdout after parsing succeeds.

Arguments are sanitized before storage. Credentials and secret-like values are redacted. The OpenRouter key is read from the host environment or `.env`, used only for the API request, and never persisted or exposed to solver tools.

## Performance tradeoffs

Evaluation runs sequentially and reuses the immutable evaluator image. The default path creates one container per phase/repetition, runs all selected tests there, and uses one repetition; this minimizes container churn for RL rollouts. Baseline and post-solver remain separate, and solver runs still repeat the baseline before spending solver resources. Authors can opt into repeated sampling and `test-attempt` isolation for stronger flake and state-leak detection. Bounded parallel repetitions are a possible future optimization, provided evidence ordering and resource limits remain deterministic.

The implementation deliberately does not include:

- hostile multi-tenant isolation or adversarial in-process grading;
- full software-supply-chain attestation;
- per-container resource telemetry;
- redacted reports intended for public sharing;
- distributed execution;
- branching agents, checkpoint resume, or human approval workflows.

The agent remains one bounded OpenRouter model-and-tool loop. A workflow framework such as LangGraph would become useful only if branching, resumability, or approval steps become real requirements. Until then, it would duplicate the lifecycle and SQLite persistence already present in the CLI.
