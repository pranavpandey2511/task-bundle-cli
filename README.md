# Task Bundle CLI

Task Bundle CLI builds and grades coding-task bundles locally. It pins a repository revision, verifies the task against a reference patch, gives a solver a redacted source tree, then grades only the solver's captured patch in a fresh evaluator.

It is designed for reviewable, repeatable local evaluation—not for executing untrusted workloads in a shared production environment.

## What it provides

- A Python CLI for creating, validating, running, and reviewing task bundles.
- Docker-isolated solver and evaluator environments.
- Patch-based grading with protected-path enforcement.
- A local SQLite evidence ledger, JSON reports, and self-contained HTML reports.
- Two validated SWE-bench Pro Ansible examples:
  - [`swe-bench-pro-ansible`](examples/swe-bench-pro-ansible/)
  - [`swe-bench-pro-ansible-safe-eval`](examples/swe-bench-pro-ansible-safe-eval/)

## Quick start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git
- Docker CLI plus a running compatible daemon (Docker Desktop, Colima, or Docker Engine)

```bash
uv sync --frozen
uv run task doctor .
```

On macOS, a lightweight Colima setup is:

```bash
brew install colima docker
colima start
```

Then confirm Docker is usable:

```bash
docker run --rm hello-world
```

### Run the included bundle

This deterministic path grades the included reference patch. It requires Docker but does not require an API key or send source to a model provider.

```bash
BUNDLE=examples/swe-bench-pro-ansible

uv run task validate "$BUNDLE" --static
uv run task init "$BUNDLE"
uv run task validate "$BUNDLE"
uv run task run "$BUNDLE" \
  --solver patch \
  --candidate-patch "$BUNDLE/gold.patch"
uv run task report --bundle "$BUNDLE"
```

The second included bundle follows the same commands; substitute `examples/swe-bench-pro-ansible-safe-eval`.

## Core workflow

| Command | What it does | Docker required |
| --- | --- | --- |
| `task new` | Creates an editable, profile-aware task draft. | No |
| `task validate --static` | Checks the manifest, patches, paths, and authoring contract. | No |
| `task init` | Materializes the pinned repository and builds evaluator and redacted solver images. | Yes |
| `task validate` | Proves the baseline and reference-patch expectations. | Yes |
| `task run` | Invokes a solver, captures its patch, applies policy, and grades it. | Yes |
| `task report` | Verifies evidence, shows the result, and optionally exports it. | No |
| `task doctor` | Checks Python, Git, Docker CLI, and daemon availability. | No |

For all command options, run `uv run task COMMAND --help`.

### Choose a solver

| Solver | Best for | Requirement |
| --- | --- | --- |
| `patch` | Deterministic, offline grading of an existing patch. | `--candidate-patch PATH` |
| `agent` *(default)* | Asking the built-in OpenRouter-backed agent to solve the task. | `OPENROUTER_API_KEY` |
| `stub` | Exercising setup and unresolved-result handling without a change. | None |

Example agent setup:

```bash
cp .env.example .env
# Set OPENROUTER_API_KEY and optionally OPENROUTER_MODEL in .env.
uv run task run examples/swe-bench-pro-ansible --solver agent
```

The host—not the solver container—contacts OpenRouter. Model requests can include the task description, requested source excerpts, and command output; use `patch` when source must remain local.

## How grading works

```text
task init      pinned source ──► evaluator image + redacted solver image
task validate  baseline: expected failures ──► gold patch: expected passes
task run       solver patch ──► fresh evaluator ──► resolved or unresolved
```

The evaluator keeps the full task material. The solver receives only the sanitized repository source and description. Its container is destroyed after patch capture; a fresh evaluator grades that patch. A task resolves only when every selected test observation matches its expectation.

By default, a phase runs its selected tests in one fresh evaluator container. Use `--evaluator-isolation test-attempt` to start a separate container for every test, and `--repetitions N` for repeated evaluation.

## Safety and evidence

The solver and evaluator use separate Docker contexts. They have no host bind mounts, Docker socket, or network access, and run with a read-only root filesystem, disposable workspace, resource limits, dropped capabilities, and `no-new-privileges`.

This is a strong local source-visibility boundary, not a complete defense against arbitrary hostile task code. Task-authored Dockerfiles and evaluator inputs are trusted. See [DESIGN.md](DESIGN.md) for the full trust boundary and production-hardening guidance.

Each lifecycle command records evidence in the bundle's ignored `.taskbundle/` directory:

```text
.taskbundle/
├── taskbundle.db                 # commands, events, attempts, artifact metadata
├── commands/<command-id>/        # JSON, HTML, logs, patches, snapshots
├── reports/latest.html           # newest reviewer report
└── exports/<command-id>.zip      # optional portable evidence package
```

Use `task report --bundle BUNDLE --events` for the factual timeline, or add `--export PATH` to create a verified evidence ZIP. Reports can contain evaluator material, logs, and candidate diffs; keep them reviewer-side.

## Author a bundle

Create a draft, edit it, then follow the core workflow above:

```bash
uv run task new my-task \
  --id my-task \
  --repo https://github.com/example/project.git \
  --commit 0123456789abcdef0123456789abcdef01234567
```

```text
my-task/
├── task.json
├── description.md
├── environment/Dockerfile
├── gold.patch
└── tests/
    ├── hidden.patch
    └── solver-view.patch
```

Important authoring rules:

- Pin `repository.commit` to a full 40-character Git SHA.
- Keep evaluator-selected tests out of the solver view: inject them through `tests/hidden.patch` or remove base-resident evaluator files through `tests/solver-view.patch`.
- Give every selected test a unique marker, dedicated evaluator-owned path, timeout, and expected outcome.
- Limit candidate changes with `candidate.allowed_patch_paths`; explicitly deny protected subpaths when needed.
- Keep solver networking disabled.

`task validate --static` checks the authoring contract; `init` and full `validate` prove the repository, image, and test behavior. The generated profiles cover Python, Node, Go, Rust, and custom repositories, but the bundle author owns the runtime commands and Dockerfile.

## Results and exits

Human-readable output is sent to stderr. Add `--json` after successful argument parsing for one machine-readable `CommandReport` on stdout.

| Exit code | Meaning |
| --- | --- |
| `0` | The requested expectation succeeded. |
| `1` | Invalid task expectation, failed evidence integrity, or unresolved candidate. |
| `2` | CLI or bundle configuration error. |
| `3` | Infrastructure, interruption, or unexpected internal error. |
| `4` | Solver failure, timeout, malformed patch, or patch-policy violation. |

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv build --offline

# Optional real-Docker integration test
TASKBUNDLE_RUN_DOCKER_TESTS=1 uv run pytest -q -m docker
```

The project was last validated in this checkout on 2026-08-10: 135 regular tests passed (one Docker test skipped), real Docker integration passed with both isolation modes, both included bundles completed their positive-control patch runs, and the offline wheel and source-distribution build succeeded.

## Further reading

- [Design decisions, security model, and known limits](DESIGN.md)
- [Future direction for broader repository support](GENERAL_REPOSITORY_SUPPORT.md)
- [Primary example result](examples/swe-bench-pro-ansible/evaluation.json)
- [Safe-eval example result](examples/swe-bench-pro-ansible-safe-eval/evaluation.json)
