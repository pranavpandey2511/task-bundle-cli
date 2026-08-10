# Task Bundle CLI

Task Bundle CLI is a local judge for coding tasks. It pins one repository revision, proves the task's baseline and reference-solution behavior, runs a solver without exposing evaluator tests, grades only the captured candidate patch, and records reviewable JSON and HTML evidence.

This repository contains:

- the Python CLI in `src/taskbundle/`;
- a validated SWE-bench Pro Ansible bundle in `examples/swe-bench-pro-ansible/`;
- a checked-in pass/fail summary in `examples/swe-bench-pro-ansible/evaluation.json`;
- architecture, decisions, tradeoffs, and known limits in `DESIGN.md`.

## Install

Requirements:

- Python 3.12 or newer;
- [uv](https://docs.astral.sh/uv/);
- Git;
- the Docker CLI and a running Docker-compatible daemon.

```bash
uv sync --frozen
uv run task doctor .
```

The examples below use `uv run task` so they work directly from this checkout. An installed package exposes the same CLI as `task`.

## Set up Docker first

Task Bundle CLI uses the local Docker CLI to build images and start isolated containers. Installing only the `docker` command is not enough: it must be able to reach a running daemon. Check that before running the lifecycle:

```bash
docker version
docker run --rm hello-world
uv run task doctor .
```

### macOS: Colima

[Colima](https://github.com/abiosoft/colima) is a lightweight Docker runtime for macOS. With Homebrew, install both the runtime and Docker client, then start the runtime:

```bash
brew install colima docker
colima start
docker run --rm hello-world
uv run task doctor .
```

Colima normally configures the Docker client when it starts. If `docker version` cannot reach the daemon, inspect `docker context ls`, make sure the Colima context is selected, and rerun `colima start` before retrying `task doctor`.

### Docker Desktop or other platforms

[Docker Desktop](https://docs.docker.com/desktop/) is a supported alternative on macOS, Windows, and Linux: install it, start it, then run the same three checks above. On a Linux host, install [Docker Engine](https://docs.docker.com/engine/install/) for the distribution and ensure the current user can access the daemon. The CLI only requires a Docker-compatible local engine; it does not require Docker Desktop specifically.

## Run the included bundle end to end

The included bundle is a real SWE-bench Pro Ansible task. Its repository commit, digest-pinned base image, test commands, evaluator patches, reference patch, and compact evaluation result are checked in.

```bash
# 1. Fast contract checks; no clone or Docker container is needed.
uv run task validate examples/swe-bench-pro-ansible --static

# 2. Build and verify separate evaluator and sanitized solver images.
uv run task init examples/swe-bench-pro-ansible

# 3. Prove the baseline and reference-solution truth tables.
uv run task validate examples/swe-bench-pro-ansible

# 4. Use the checked-in gold patch as a positive-control solver output.
uv run task run examples/swe-bench-pro-ansible \
  --solver patch \
  --candidate-patch examples/swe-bench-pro-ansible/gold.patch

# 5. Verify and explain the newest lifecycle result.
uv run task report --bundle examples/swe-bench-pro-ansible

# Optional: inspect factual events or export a portable evidence package.
uv run task report --bundle examples/swe-bench-pro-ansible --events
uv run task report --bundle examples/swe-bench-pro-ansible \
  --export /tmp/ansible-task-evidence.zip
```

Each lifecycle summary prints the exact generated HTML path. Open `.taskbundle/reports/latest.html` for the newest review or `.taskbundle/reports/index.html` to browse all immutable command reports.

With the bundle's three configured repetitions, validation records 30 attempts: five tests across baseline and golden phases, repeated three times. The positive-control run records 30 more attempts across baseline and post-solver phases. The expected compact result is:

| Phase | PASS_TO_PASS | FAIL_TO_PASS | Outcome |
| --- | --- | --- | --- |
| Baseline | 4 pass | 1 fails as expected | Valid task baseline |
| Golden | 4 pass | 1 passes | Reference solution is valid |
| Post-solver | 4 pass | 1 passes | Task resolved |

The checked-in [`evaluation.json`](examples/swe-bench-pro-ansible/evaluation.json) contains the review-friendly test summary and immutable run identifiers. Full logs, patch-application evidence, repository snapshots, provenance, and HTML reports are generated under the bundle's ignored `.taskbundle/` directory so normal runs do not bloat Git history.

## How the lifecycle works

```text
task.json + trusted bundle files
              │
              ├─ task init ─────► evaluator image (complete pinned source)
              │             └───► solver image (evaluator files removed)
              │
              ├─ task validate ─► baseline: P2P pass, F2P fail
              │             └───► golden:   P2P pass, F2P pass
              │
              └─ task run ──────► sanitized solver → captured Git patch
                            └───► fresh evaluators + hidden tests → result
```

Each test attempt gets a fresh container and volume. A run repeats the baseline before invoking the solver, destroys the solver container after patch capture, rejects protected or unauthorized changed paths, and grades the patch in fresh evaluator containers. A run resolves only when every post-solver observation is a stable pass.

Only configured assertion-failure exit codes count as a legitimate `fail` observation; the default is exit code `1`. Timeouts and other nonzero exits are recorded as `timeout` or `error`, so a broken test runner cannot accidentally validate a FAIL_TO_PASS expectation.

## Commands

| Command | Purpose |
| --- | --- |
| `task new` | Create an editable, profile-aware bundle draft. |
| `task init` | Materialize the pinned commit and build verified evaluator and solver images. |
| `task validate` | Run static author checks or prove baseline/golden behavior. |
| `task run` | Execute a solver, capture its patch, enforce path policy, and grade it. |
| `task report` | Select, diagnose, integrity-check, show HTML review paths, or export evidence. |
| `task doctor` | Check Python, Git, the Docker CLI, and the Docker daemon. |

Run `uv run task COMMAND --help` for all options. The primary workflow is:

```bash
uv run task new my-task \
  --id my-task \
  --repo https://github.com/example/project.git \
  --commit 0123456789abcdef0123456789abcdef01234567

# Edit the generated draft, then:
uv run task validate my-task --static
uv run task init my-task
uv run task validate my-task
uv run task run my-task --solver patch --candidate-patch candidate.patch
uv run task report --bundle my-task
```

Every human-readable command that reaches the lifecycle boundary ends with `Summary & next steps`: status, resolved bundle path, command ID, result, report paths when available, and copy-paste follow-up commands. `task report` defaults to the latest `new`, `init`, `validate`, or `run` record, so from inside a bundle the usual inspection command is simply:

```bash
uv run task report
```

### Solver adapters

```bash
# Make no changes and exercise the normal unresolved path.
uv run task run my-task --solver stub

# Grade a patch produced elsewhere.
uv run task run my-task \
  --solver patch \
  --candidate-patch candidate.patch

# Run an offline agent or script inside the sanitized solver container.
uv run task run my-task \
  --solver command \
  --solver-cmd '<offline solver command>'
```

The adapters are mutually exclusive. `patch` requires `--candidate-patch`; `command` requires `--solver-cmd`; `stub` accepts neither. In-container solver networking is deliberately disabled. A networked or remote model can produce a patch outside this boundary, then the patch adapter can grade it locally.

`--secret-env NAME` forwards an existing host environment variable by name to a command solver without storing its value in arguments or reports. Avoid forwarding secrets unless necessary: an untrusted solver can still print a value into its own captured output.

## Author a bundle

`task new` selects an editable Python, Node, Go, Rust, or custom starter. `--profile auto` detects common manifests only for a local repository; remote or unrecognized repositories use the Python starter. Detection is a convenience, not proof that the generated Dockerfile is correct.

```text
my-task/
├── task.json
├── description.md
├── environment/
│   └── Dockerfile
├── gold.patch
└── tests/
    ├── hidden.patch
    └── solver-view.patch
```

The important contract is:

- `repository.commit` is a full 40-character Git commit;
- the Dockerfile owns the language runtime and dependencies;
- every PASS_TO_PASS and FAIL_TO_PASS selection has an ID, shell command, dedicated evaluator-owned path, unique source marker, timeout, and accepted assertion-failure codes;
- selected evaluator tests live only in dedicated evaluator files; public and non-bucket tests stay at other paths;
- `tests/hidden.patch` injects evaluator tests that are absent at the base commit;
- `tests/solver-view.patch` deletes complete base-resident evaluator files from the solver source;
- `tests.additional_protected_paths` lists generated or derived evaluator material;
- `candidate.allowed_patch_paths` contains only implementation files or subtrees and must contain every path changed by `gold.patch`;
- validation repetitions default to three and may be overridden from 1 through 20;
- solver networking must remain `false`.

`task validate --static` checks schemas, required files, path and symlink safety, trusted patch relationships, marker leakage into the description, candidate path policy, hashes, Docker base-image pinning, repository portability, and repetition settings. It does not claim the Git commit exists, the Dockerfile builds, or the declared tests behave correctly; `task init` and full `task validate` prove those facts.

The CLI is language-neutral: smoke and test commands are bundle-owned shell commands. Authors must ensure each command genuinely selects its declared test. Prefer an absolute, immutable test runner and disable candidate-controlled configuration or plugin discovery. The generated Python profile and included Ansible example demonstrate this pattern.

## Test secrecy and candidate policy

The solver image is built from a separate sanitized source tree. Before that image is accepted, initialization verifies that:

- every evaluator-owned path is absent;
- every declared marker is absent from the readable filesystem, Git history, and environment;
- original root and nested Git metadata are gone;
- the synthetic solver repository has no remotes and is pristine;
- evaluator tools resolve outside the writable repository and `/tmp`.

The solver still sees all non-evaluator source and public tests. It never receives `task.json`, the gold patch, either evaluator patch, the original Git object database, or generated reviewer reports. Patch capture includes tracked changes, solver commits, and non-ignored untracked files. Binary changes, renames, copies, quoted paths, protected paths, and out-of-policy paths are parsed and checked before grading.

This is a strict source-visibility contract, not a claim that a language-neutral harness can prove arbitrary hostile candidate code behaved honestly inside the test process. See `DESIGN.md` for the exact trust boundary and production-hardening path.

## Errors, JSON, and exit codes

Typer reports unknown commands, missing options, invalid ranges, and type errors with the relevant usage line and `COMMAND --help` hint. Lifecycle failures return a structured error with:

- `kind` for automation;
- a concise `message`;
- an actionable `hint` when one is known;
- evidence-linked `details`;
- a durable command ID and HTML/JSON report when reporting was possible.

Human output is written to stderr. Once argument parsing succeeds, add `--json` to emit one machine-readable `CommandReport` on stdout, with no human footer. Parser-level usage mistakes use Typer's concise stderr diagnostics. Solver command text and secret values are excluded from stored reports; the solver-command hash and requested secret names are retained for provenance.

| Exit | Meaning |
| --- | --- |
| `0` | The requested expectation succeeded. |
| `1` | Task expectations were invalid, evidence integrity failed, or the candidate was unresolved. |
| `2` | CLI/bundle configuration error or unknown command ID. |
| `3` | Infrastructure, interruption, or unexpected internal failure. |
| `4` | Solver failure, timeout, malformed patch, or candidate path-policy violation. |

If `task.json` becomes malformed after a run, `task report --bundle BUNDLE` can still read the local ledger and explain prior commands. Use `--events` for the factual lifecycle timeline, `--list` for older versions, or pass an explicit command ID.

## Evidence and local state

```text
.taskbundle/
├── taskbundle.db
├── cache/<build-fingerprint>/build.json
├── commands/<command-id>/
│   ├── report.json
│   ├── report.html
│   ├── provenance.json
│   └── logs, patches, snapshots, and result JSON
├── reports/
│   ├── index.html
│   └── latest.html
└── exports/<command-id>.zip
```

SQLite stores commands, events, individual attempts, and artifact metadata. Artifacts carry size and SHA-256 records. `task report` verifies every artifact before presenting it; `--export` refuses tampered evidence and produces a byte-stable ZIP for the selected recorded command. The export and HTML review contain trusted evaluator material and must not be exposed to a solver before grading.

### Review the HTML reports

Lifecycle HTML reports are static reviewer views derived from the ledger. They show the outcome, expected-versus-observed attempts, diagnosis, bounded log excerpts, candidate changes, reproducibility context, factual events, and links to exact evidence. Reports are created for successful and failed `new`, `init`, `validate`, and `run` commands.

- `.taskbundle/commands/<command-id>/report.html` is the immutable report for one command;
- `.taskbundle/reports/latest.html` points to the newest report;
- `.taskbundle/reports/index.html` lists all report versions newest first.

The HTML is self-contained and needs no application server. It never reruns tests or solver code; it renders escaped data already stored in SQLite and artifact files. Reports may include hidden-test information, solver output, and candidate diffs, so review them locally and do not expose them to a solver or publish them without redaction.

## Isolation summary

Solver and evaluator containers have no host bind mount or Docker socket. Runtime containers use a disposable workspace volume, read-only root filesystem, size-bounded tmpfs, CPU/memory/PID limits, dropped capabilities, `no-new-privileges`, a forced shell entrypoint, explicit timeouts, and no network. Solver state is destroyed before its patch is graded in fresh evaluators.

Docker remains a local shared-kernel boundary. Task-authored Dockerfiles and evaluator inputs are trusted. Hostile multi-tenant execution should move to disposable workers or microVMs and add image policy, host-level egress controls, external watchdogs, and short-lived credentials. Detailed rationale and deferred work are in `DESIGN.md`.

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv build --offline

# Opt-in real Docker lifecycle integration.
TASKBUNDLE_RUN_DOCKER_TESTS=1 uv run pytest -q -m docker
```

The regular suite uses deterministic fakes for fast coverage. The Docker test builds both images, proves evaluator material and old Git history are absent from the solver, confirms public tests remain visible, exercises unresolved and resolved runs, rejects a runner-shadow patch, verifies evidence hashes, and confirms container cleanup.

### Verified in this checkout

The release evidence was refreshed on 2026-08-10 with CLI 0.2.0:

- static validation passed all seven authoring checks with no warnings;
- the regular suite passed 103 tests with one opt-in Docker test skipped;
- the real Docker integration passed independently;
- the included Ansible bundle passed `init`, all 30 validation attempts, and all 30 positive-control run attempts with no mismatch or flaky observation;
- run `20260810T044632829714Z-614c9b61` resolved, and its portable evidence export verified all 167 recorded artifacts with no integrity failure.

The compact, reviewable result is checked into `examples/swe-bench-pro-ansible/evaluation.json`; the larger local ledger and logs remain intentionally ignored.
