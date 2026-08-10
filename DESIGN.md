# Design notes

## Scope

Task Bundle CLI is intentionally a local evaluation tool, not a hosted benchmark platform. Its job is to take one versioned task bundle through a linear, inspectable lifecycle: materialize an exact repository, prove the task's expectations, run one solver, grade its patch, and preserve evidence. It does not add queues, remote workers, accounts, a scheduler, or a second application database. Those systems would add operational weight without improving the assignment's core correctness.

The main components have narrow ownership:

| Component | Responsibility |
| --- | --- |
| Typer CLI | Parse commands and present human or JSON results. |
| Pydantic models | Enforce the versioned bundle and report contracts. |
| Lifecycle modules | Orchestrate initialization, validation, and solving. |
| Git adapter | Materialize exact commits, validate patches, and sanitize solver source. |
| Docker adapter | Build images and run isolated disposable containers. |
| SQLite ledger | Store commands, factual events, attempts, and artifact metadata. |
| Artifact/report layer | Hash evidence, diagnose results, render static HTML, and export ZIPs. |

This separation is deliberate. The CLI does not interpret a Python traceback, understand a JavaScript test framework, or call a model provider. The bundle owns repository-specific behavior; the engine owns lifecycle, isolation, evidence, and stable failure semantics.

## Decisions at a glance

| Decision | Why | Main cost |
| --- | --- | --- |
| Task-owned Dockerfile and shell commands | Supports arbitrary repositories without framework-specific adapters. | The author must write trustworthy commands. |
| Full commit and schema-v3 manifest | Makes task inputs explicit and rejectable before execution. | Older schema drafts require migration. |
| Separate evaluator and solver images | Removes selected test source and original Git history before solving. | Two builds take more time and disk space. |
| Whole-file evaluator ownership | Gives a language-neutral, verifiable secrecy rule. | Bucket and public tests cannot share a file. |
| Explicit candidate path allow-list | Prevents changing tests or runner/configuration files to manufacture a pass. | Authors must maintain the allow-list. |
| Fresh container per attempt | Prevents one attempt's files or processes from affecting another. | Validation is intentionally slower. |
| No solver network | Prevents refetching public evaluator material after redaction. | Networked agents must produce a patch outside the sandbox. |
| SQLite plus content-hashed files | Keeps evidence queryable, portable, and easy to inspect locally. | It is designed for one local operator, not distributed writers. |
| Static HTML reports | Makes review easy without adding a server or new source of truth. | Reports are trusted evaluator artifacts and must remain hidden from solvers. |
| Stable exit codes and structured errors | Makes failures useful to both humans and automation. | The CLI must preserve error classification across every layer. |

## Repository and test generality

The CLI is language-neutral. Each bundle supplies a Dockerfile, container workdir, smoke command, and named PASS_TO_PASS and FAIL_TO_PASS shell commands. This keeps runtimes, system packages, package managers, and test frameworks beside the task instead of growing unreliable auto-detection in the engine. `task new` offers ecosystem starters to reduce setup time, but the result is explicitly a draft. Only `task init` can prove the image builds, and only full `task validate` can prove the tests behave as declared.

A task author is trusted to make each command run the corresponding test. Inferring that relationship safely across arbitrary languages is not realistic. The manifest therefore requires a test ID, evaluator-owned path, globally unique single-line source marker, assertion-failure exit codes, and timeout. Initialization can prove the path and marker are absent from the solver; evaluation can prove the marker is present before the command runs. It cannot prove that an arbitrary shell command selects only that test. The Python scaffold reduces runner manipulation by using an absolute isolated interpreter and disabling pytest configuration, conftest discovery, and plugin autoload.

Selected bucket tests must live in evaluator-owned files. Public and non-bucket tests remain in other files and stay visible to the solver. Mixed bucket/public files are unsupported because a language-neutral tool cannot reliably remove individual functions while preserving imports, fixtures, parametrization, generated cases, and file semantics. The included Ansible bundle uses its complete selected-test file as the protected unit: `solver-view.patch` removes that file from the solver tree, while unrelated Ansible tests remain available.

## Lifecycle and correctness

`task init` clones the exact 40-character commit, initializes submodules at their recorded commits, verifies a clean detached checkout, and prepares a build context containing only `Dockerfile` and `source/`. It first builds the evaluator image from the complete tree. It then applies the solver-view redaction in temporary host staging, confirms protected paths are gone, removes root and nested Git metadata, and creates a deterministic, remote-free synthetic Git baseline. The same Dockerfile builds a distinct solver image from that sanitized source.

Initialization verifies both immutable image IDs in disposable containers. The evaluator repository must be pristine at the configured commit. The solver repository must be pristine at the synthetic commit and contain no protected paths, source markers, foreign Git metadata, remotes, or marker bytes in its readable filesystem, Git history, or environment. Verification fails closed: a scan timeout or tool error is not interpreted as absence. The marker and evaluator patches are used only by the trusted initialization process and never copied into the later agent container.

`task validate` checks two truth tables:

| Phase | PASS_TO_PASS | FAIL_TO_PASS |
| --- | --- | --- |
| Baseline plus evaluator tests | pass | fail |
| Gold patch plus evaluator tests | pass | pass |

Every named attempt uses a fresh evaluator container and volume. Only exit code `0` is a pass. A configured assertion exit, normally `1`, is a fail. A timeout is a timeout, and every other nonzero exit is an error. This distinction prevents a missing dependency or crashed runner from satisfying an expected baseline failure. Repeated observations must all match and be stable; inconsistent outcomes are reported as flaky and invalidate the task.

`task run` repeats the baseline before starting a solver, so a stale or broken task cannot produce an accepted result. The solver works in the sanitized image and receives only the remaining repository source, a clean synthetic Git baseline, the solver-visible description path, and explicitly named environment variables. After the solver exits, the engine stages tracked and non-ignored untracked changes, captures a binary/full-index patch relative to the synthetic base, and destroys the solver container.

The captured patch is parsed before grading. Old and new paths from normal changes, binary changes, renames, copies, mode changes, and Git-quoted filenames are considered. Ambiguous or malformed input is rejected. Protected paths and anything outside `candidate.allowed_patch_paths` are rejected. Only then is the candidate patch streamed into fresh evaluator containers after hidden tests have been applied and the temporary hidden-patch file has been deleted. The run resolves only when all post-solver attempts pass without flakiness.

## Test secrecy and trust boundary

The trust model is explicit:

| Actor or input | Trust level | Reason |
| --- | --- | --- |
| Bundle author, manifest, Dockerfile, test commands, evaluator patches | Trusted | They define the environment and grading policy. |
| CLI implementation and local Docker daemon | Trusted infrastructure | They enforce the boundary and retain evaluator evidence. |
| Solver command and candidate patch | Untrusted | They may inspect or modify anything the solver boundary exposes. |
| Candidate implementation during tests | Untrusted but executed by the evaluator command | It shares the task's language/runtime process unless the task author isolates it further. |

Three patches have different roles. `gold.patch` is the trusted reference implementation. `tests/hidden.patch` adds evaluator material that is absent from the pinned repository. `tests/solver-view.patch` removes complete base-resident evaluator files before the solver image is built. Static validation permits evaluator patches to touch only declared protected paths, requires their combined paths to cover the protected set, keeps the gold patch disjoint, and requires every gold path to fit the candidate allow-list. Initialization then verifies the solver-view patch actually leaves all protected paths absent.

The candidate allow-list is separate from secrecy. Hiding tests prevents pre-solve inspection; path policy prevents a solver from replacing test files, configuration, runner-shadow modules, or unrelated source to force a green exit. The author should keep the allow-list as narrow as the legitimate solution surface. Generated build outputs that should not become candidate changes must be ignored or suppressed; otherwise they are correctly treated as part of the solver patch.

This boundary protects evaluator source before solving, but it is not a proof against every adversarial program. Candidate code and assertions can share one language process, so candidate code could terminate or corrupt that process unless a task uses an external assertion mechanism. Similarly, an implementation intentionally written to inspect evaluator files at grading time is beyond what a generic exit-code harness can cryptographically distinguish. High-assurance tasks should run critical assertions in an external process that candidate code cannot control.

## Runtime isolation

Solver, evaluator, smoke, and grading containers run without host bind mounts, a Docker socket, or network access. The repository lives in a disposable Docker volume. The root filesystem is read-only; `/tmp`, home, cache, config, and data directories use a size-bounded tmpfs. Docker drops all Linux capabilities, enables `no-new-privileges`, and applies CPU, memory, PID, and timeout limits. The entrypoint is forced to `/bin/sh`, avoiding task-image entrypoints that could intercept harness commands.

Trusted evaluator operations override `PATH` with manifest-validated absolute directories outside the writable workdir and `/tmp`. Initialization confirms those directories and required tools resolve to executable, non-writable locations. Patch streaming and critical file removal use absolute tools. Solver commands intentionally use the solver image's normal environment; they are untrusted and do not receive the harness's trusted-path privilege.

Networking is always `none` under schema v3. Allowing a solver to access a public origin would let it refetch redacted tests, making local file deletion meaningless. The manifest therefore accepts only `solver_network: false`, and the compatibility `--allow-network` flag returns a configuration error. A remote model remains usable: produce a candidate patch outside this boundary, then grade it with the patch adapter.

Docker is a practical local sandbox, not a formal hostile-code boundary. Containers share the host kernel. The task-authored Dockerfile runs during a trusted build step with Docker build's normal privileges and possible dependency-network access. A production multi-tenant deployment should use disposable worker hosts or microVMs, image allow-listing and scanning, host-level egress enforcement, external watchdogs, quotas, and short-lived credentials. Those controls are documented instead of being implied by a few extra container flags.

## Reproducibility and determinism

Repositories use full commits rather than branches or tags. Submodules are initialized and checked at their pinned revisions. The build fingerprint binds the release version and build-context schema, bundle ID, normalized repository URL and commit, workdir, trusted evaluator PATH, Dockerfile hash, solver-view hash, and the full protected-path/marker secrecy contract. Evaluator and solver tags are cache conveniences; cached metadata is accepted only when inspected immutable image IDs still match.

Gold and hidden-test changes do not alter image contents, so they do not force an image rebuild. They do alter execution provenance. At the start of validate and run, the parsed manifest source, description, Dockerfile, and all patches are read once, stored as trusted-input artifacts, hashed, and reused for the whole command. This prevents a mid-command edit from changing baseline and grading differently. Execution provenance also records image IDs, synthetic solver commit, runtime limits, repetitions, solver adapter, network policy, solver-command hash, and candidate-input hash.

The release version is part of build identity, so behavior-changing releases must bump it; this repository moves the strengthened schema-v3 boundary to version `0.2.0`. A context-schema constant separately invalidates images when staging semantics change. Timestamps, command IDs, container IDs, and durations are evidence rather than deterministic inputs.

Cross-machine reproducibility still depends on bundle discipline. The static validator warns, rather than fails, when external base images are not digest-pinned or repositories use local paths. Dependency lockfiles, package registries, CPU architecture, locale, and upstream availability remain task concerns. The included example pins its base-image digest and Python dependency versions, but adapts the official amd64-only environment to a native ARM64-compatible reconstruction; that adaptation is recorded in `dataset.json` rather than presented as the official image.

## Evidence and observability

Every command that reaches the lifecycle wrapper gets a sortable command ID before work starts. SQLite stores sanitized arguments, status, exit code, timestamps, structured factual events, individual test observations, and artifact metadata. Files store trusted-input snapshots, checkout/build/smoke logs, patch checks and applications, test output, repository snapshots, solver stdout/stderr, captured patches, provenance, and final JSON reports. Every artifact record includes its relative path, size, and SHA-256 digest.

`task report` selects the latest lifecycle command by default and does not rerun untrusted code. It verifies artifact integrity, groups attempts, identifies mismatches, flakiness, timeouts, runner errors, and lifecycle failures, and gives evidence-linked next actions. It can still inspect the ledger when the current manifest is malformed. `--events` adds the recorded timeline, `--list` exposes immutable report versions, and `--export` refuses unverified inputs before creating a deterministic ZIP for that recorded command.

Each `new`, `init`, `validate`, and `run` result also produces a self-contained static HTML review, including failure results. HTML is derived from the ledger and escaped evidence, not a new execution path. Immutable reports live under their command IDs; mutable `index.html` and `latest.html` files provide navigation. This design delivers a reviewer-friendly interface without adding FastAPI, React, WebSockets, or another database. Reports and exports can contain hidden tests and solver output, so they remain on the trusted reviewer side of the boundary.

Repository snapshots currently capture pristine evaluator state at the start of each attempt, pristine solver state before solving, and solver state after non-timeout completion. Patch logs record hidden, gold, and candidate staging. The system does not yet capture a repository snapshot after every evaluator patch or after every test, nor does it record container CPU or peak-memory telemetry. Those additions would improve forensic depth but are not required to decide task correctness.

## UX and error model

The documented surface is six commands: `new`, `init`, `validate`, `run`, `report`, and `doctor`. Older `check`, `history`, `logs`, `diagnose`, `artifacts`, and `export` commands remain hidden compatibility aliases. `validate --static` owns the fast edit loop; `report` owns selection, diagnosis, integrity verification, event viewing, history, and evidence export. This keeps the common workflow small without deleting useful engine capabilities.

Parser errors show the relevant usage and help command. Expected lifecycle failures use typed errors with a stable kind, exit code, concise message, optional recovery hint, and structured details. Human output goes to stderr and ends with a result summary plus copy-paste next commands. `--json` writes one `CommandReport` to stdout for automation. Configuration and invalid-task errors are kept distinct from solver failures and infrastructure failures, so CI and humans can choose the correct recovery path.

Command arguments are sanitized before persistence. Repository URLs, solver commands, tokens, passwords, and secret-like values are redacted; provenance stores solver-command and candidate hashes instead. Secret environment values are never intentionally stored, although an untrusted solver can print a forwarded secret into its own captured stdout or stderr. The safest policy is not to forward secrets to untrusted code.

## Performance tradeoffs

Correctness and clean-state evidence currently take priority over throughput. Attempts run sequentially and each one creates and removes a container and volume. Full validation executes baseline plus golden phases, and every run repeats the baseline before solving. These costs are intentional: batching or reusing a mutated workspace would weaken attribution and let state leak across tests.

The safe shortcuts are Docker-free static validation, content-addressed image reuse, and a lower `--repetitions` value while authoring. Final validation should restore the manifest's repetition count. If profiling later shows container startup dominates, bounded parallel attempts are the next reasonable optimization, provided result ordering remains deterministic and every attempt retains an independent container, volume, log, and database record. A local bare Git mirror and remote image cache could reduce preparation time without changing the trust boundary.

## Deliberate omissions and next hardening steps

The current implementation meets the local assignment, but it does not claim to solve these larger problems:

1. **Hostile multi-tenancy:** move execution to disposable workers or microVMs before accepting mutually untrusted users and tasks.
2. **Adversarial in-process grading:** move critical assertions outside the candidate-controlled language process.
3. **Strict supply-chain identity:** add an opt-in profile that requires image digests and ecosystem lockfiles, then record OCI platform, selected in-image tool versions, and an SBOM.
4. **Deeper evaluator forensics:** capture metadata snapshots after hidden/gold/candidate staging and after the test, then derive a comparison timeline from existing evidence.
5. **Safe external sharing:** add a redacted support-export format and retention policy; current HTML and ZIP output are evaluator-private.
6. **Distributed execution:** if required, preserve bundle/report schemas while moving orchestration to workers and the ledger to a shared transactional database.

These are explicit boundaries rather than partially implemented abstractions. The current design remains a lean, reproducible vertical slice whose security and evidence claims can be inspected directly.
