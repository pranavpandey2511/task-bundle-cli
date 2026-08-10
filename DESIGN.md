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

| Decision                               | Reason                                                            | Cost                                                                |
| -------------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------- |
| Full Git commit and schema-v3 manifest | Reject mutable or ambiguous task inputs.                          | Authors must supply explicit metadata.                              |
| Task-owned Dockerfile and commands     | Work across languages and test frameworks.                        | Command correctness remains an author responsibility.               |
| Separate evaluator and solver images   | Remove selected tests and original Git history before solving.    | Two image builds use more time and disk.                            |
| Whole-file evaluator ownership         | Provides a verifiable, language-neutral secrecy rule.             | Public and private tests cannot share a file.                       |
| Candidate path allow-list              | Prevent test, runner, or unrelated-file manipulation.             | The solution surface must be maintained explicitly.                 |
| Optional host-mediated OpenRouter agent | Keep model credentials and API traffic outside untrusted code.     | Agent output is model-dependent and consumes external API credits.  |
| Fresh container per attempt            | Prevent state leakage between tests and phases.                   | Evaluation is intentionally slower.                                 |
| Static HTML reports over a web app     | Give reviewers a useful interface without another execution path. | Reports are local evaluator artifacts, not a collaboration service. |

## Lifecycle and correctness

`task init` clones the exact commit, checks submodules and repository cleanliness, and builds an evaluator image from the complete source. It applies `tests/solver-view.patch` in temporary staging, removes protected files and all original Git metadata, creates a remote-free synthetic Git baseline, and builds a separate solver image. Initialization accepts those images only after verifying their immutable IDs and confirming the solver image contains no protected paths, declared test markers, foreign Git metadata, or remotes.

`task validate` proves two independent truth tables:

| Phase                           | PASS_TO_PASS | FAIL_TO_PASS |
| ------------------------------- | ------------ | ------------ |
| Baseline with evaluator tests   | pass         | fail         |
| Gold patch with evaluator tests | pass         | pass         |

Only exit code `0` is a pass. Configured assertion exits, normally `1`, are failures; timeouts and unexpected nonzero exits remain distinct errors. Every test is repeated in a fresh container and volume. All repetitions must agree, so a flaky test or broken runner cannot accidentally validate a task.

`task run` repeats the baseline before invoking one solver adapter. `agent` is the default and is the only adapter that needs optional OpenRouter configuration; the host gives that model structured tools for the sanitized container. `patch` instead grades a locally supplied patch, while `stub` deliberately makes no change to exercise the unresolved path. Every adapter produces the same review boundary: the engine captures a binary/full-index Git patch, destroys the solver container, rejects protected or out-of-policy paths, then grades the patch in fresh evaluator containers. A run resolves only when every post-solver observation is a stable pass.

For example, `task run my-task --solver patch --candidate-patch fix.patch` is a fully local, reproducible positive-control path. `task run my-task --solver agent --model provider/model-id` is an optional live attempt that can consume credits and sends the problem plus model-requested source excerpts to OpenRouter. `task run my-task --solver stub` is useful when reviewing failure messages and reports without a candidate change.

## Test secrecy and trust boundary

`gold.patch`, `tests/hidden.patch`, `tests/solver-view.patch`, the manifest, and reviewer evidence are trusted evaluator material. The solver receives none of them. Hidden tests add evaluator files missing from the base commit; the solver-view patch removes base-resident evaluator files. Public and non-bucket tests remain visible. Initialization scans the sanitized filesystem, environment, and synthetic Git history and fails closed if a protected marker is found or the scan cannot complete.

Secrecy and candidate policy solve different problems. Redaction prevents the solver from reading selected tests before solving. The allow-list prevents it from replacing tests, configuration, runner-shadow modules, or unrelated code to manufacture a pass. Authors should therefore keep `candidate.allowed_patch_paths` as narrow as the legitimate implementation surface. When an implementation surface must be broad, `candidate.disallowed_patch_paths` can carve out literal descendant files or directories; a deny rule wins over an allow rule. Evaluator-owned paths remain an unconditional hard ban, and globs are not supported.

The bundle author, Dockerfile, evaluator commands, CLI, and local Docker daemon are trusted. The solver and candidate patch are not. Docker protects the host and separates phases, but a generic exit-code harness cannot prove arbitrary candidate code behaved honestly inside a shared language process. High-assurance tasks should keep critical assertions in an external process that candidate code cannot control.

## Runtime isolation

Solver and evaluator containers have no host bind mount, Docker socket, or network. They use a disposable work volume, read-only root filesystem, bounded tmpfs directories, CPU/memory/PID limits, dropped capabilities, `no-new-privileges`, a forced shell entrypoint, and explicit timeouts. OpenRouter requests originate from the trusted host process; the API key is never copied into the container. Evaluator operations use validated absolute tool paths outside the writable repository and `/tmp`.

This is a practical local boundary, not formal hostile multi-tenancy. Containers share the host kernel, and trusted Dockerfile builds may access dependency networks. A production service should add disposable workers or microVMs, image policy and scanning, host-level egress controls, external watchdogs, quotas, and short-lived credentials.

## Reproducibility and determinism

Repositories use full commits, and build identity includes CLI version, staging schema, repository URL and commit, bundle ID, workdir, Dockerfile hash, solver-view hash, and protected-path/marker rules. Cached tags are only conveniences; recorded immutable image IDs must still match.

At the start of validation and run, the manifest, description, Dockerfile, and evaluator patches are snapshotted and hashed. Those trusted copies are reused for the command, preventing a mid-run edit from changing later phases. Provenance also records repetitions, runtime limits, image IDs, solver adapter, network policy, requested model, and candidate-input hash. Agent evidence adds the actual routed model, step count, and token usage; model generation itself is not deterministic, so the captured patch and evaluator results remain the reproducible review boundary.

Exact cross-machine behavior still depends on bundle discipline: digest-pinned base images, locked dependencies, compatible architectures, locale, and upstream availability. Timestamps, command IDs, container IDs, and durations are evidence, not deterministic inputs.

## Evidence and observability

Every lifecycle command gets a sortable command ID. SQLite stores sanitized arguments, status, exit code, events, and per-test observations. Content-hashed files retain trusted input snapshots, build and test logs, patch checks, repository snapshots, solver output, captured patches, provenance, and final JSON. `task report` reads this evidence without rerunning untrusted code, verifies artifact integrity, diagnoses mismatches, flakiness, timeouts, and runner failures, and suggests the next command.

### HTML reports

Every `new`, `init`, `validate`, and `run` result—including failures—produces a self-contained static HTML report. The immutable review for a command is stored at:

```text
.taskbundle/commands/<command-id>/report.html
```

The report presents the outcome, expected-versus-observed tests, diagnosis, bounded log excerpts, candidate changes, provenance, factual events, and links to exact artifacts. It is rendered from escaped ledger evidence and never invokes a solver or test command, so it remains a presentation layer over the same source of truth rather than a second evaluator.

`.taskbundle/reports/index.html` lists all report versions newest first, while `.taskbundle/reports/latest.html` redirects to the newest review. The CLI prints these paths in its summary, and `task report --bundle BUNDLE` locates them while verifying the recorded evidence. Because HTML can contain hidden-test names, evaluator logs, diffs, and solver output, it must remain on the trusted reviewer side. The deterministic ZIP export has the same sharing restriction.

## UX and error model

The documented surface is `new`, `init`, `validate`, `run`, `report`, and `doctor`. Parser mistakes show usage and help. Lifecycle failures have a stable kind and exit code, a concise message, an actionable hint when possible, structured details, a command ID, and evidence paths. Human output goes to stderr and ends with copy-paste next steps; `--json` provides one machine-readable report on stdout after argument parsing succeeds.

Arguments are sanitized before persistence. Repository URLs, tokens, passwords, and secret-like values are redacted. The OpenRouter key is read from the host environment or `.env`, used only in the authorization header, and never persisted or exposed to agent tools. Reports remain private evaluator evidence because they can contain source excerpts, model output, and candidate changes.

## Performance tradeoffs

Correctness and attribution currently take priority over throughput: attempts are sequential, each uses a fresh container and volume, validation runs baseline and golden phases, and a solver run repeats the baseline. Safe authoring shortcuts are Docker-free static validation, content-addressed image reuse, and temporarily lowering repetitions. Bounded parallel attempts are the clearest future optimization if each attempt keeps independent state, deterministic result ordering, and complete evidence.

## Deliberate limits

The agent is one bounded OpenRouter model/tool loop, not a LangGraph workflow. The current flow has no branching agents, checkpoint resume, or human approval nodes, so another orchestration and persistence layer would duplicate the existing lifecycle and SQLite ledger. LangGraph becomes appropriate if those requirements are added.

The current implementation does not claim hostile multi-tenant isolation, adversarial in-process grading, complete supply-chain attestation, per-container resource telemetry, redacted public report exports, or distributed execution. Those features should be added only when their requirements are real; the current design remains a lean local evaluator with explicit, inspectable guarantees.
