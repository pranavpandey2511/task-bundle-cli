# Task Bundle CLI

Task Bundle CLI builds, validates, and grades coding tasks inside Docker. A bundle pins a Git repository and commit, defines its own environment and test commands, and records every CLI action under a queryable command ID.

This repository includes the complete CLI, a validated task adapted from SWE-bench Pro, a compact evaluation result, and the reasoning behind the implementation:

- CLI: `src/taskbundle/`
- Example bundle: `examples/swe-bench-pro-ansible/`
- Example evaluation: `examples/swe-bench-pro-ansible/evaluation.json`
- Architecture and tradeoffs: `DESIGN.md`

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Git
- A running Docker daemon

Install the locked development environment and check the host:

```bash
uv sync --frozen
uv run task doctor .
```

## Try the included task

The example is the SWE-bench Pro Ansible task recorded in `dataset.json`. Its dataset revision, repository commit, patches, Dockerfile, and individual test commands are checked into the bundle.

Build the image and verify that the baseline and golden patch have the expected behavior:

```bash
uv run task init examples/swe-bench-pro-ansible
uv run task validate examples/swe-bench-pro-ansible
```

Run the checked-in gold patch as a positive-control solver. This proves that the entire solver and grading path works; a real solver would produce its own patch instead.

```bash
uv run task run examples/swe-bench-pro-ansible \
  --solver patch \
  --candidate-patch examples/swe-bench-pro-ansible/gold.patch
```

Every command prints a command ID. Use it to inspect the SQLite ledger, logs, test results, and captured artifacts:

```bash
uv run task history examples/swe-bench-pro-ansible
uv run task logs <command-id> --bundle examples/swe-bench-pro-ansible
uv run task artifacts <command-id> --bundle examples/swe-bench-pro-ansible
```

`artifacts` recalculates every stored file's size and SHA-256 digest. Add `--json` to any command when its output will be consumed by another program.

## Run a solver

Three solver adapters keep local development simple:

```bash
# Produce no patch; useful for checking the expected unresolved path.
uv run task run <bundle> --solver stub

# Grade an existing patch.
uv run task run <bundle> --solver patch --candidate-patch candidate.patch

# Run an agent or script in the isolated task workspace, then grade its Git diff.
uv run task run <bundle> --solver command --solver-cmd '<agent command>'
```

Network access is off by default. It is enabled only when both the bundle manifest and the CLI invocation opt in with `--allow-network`. Named secrets can be passed with repeated `--secret-env NAME`; their values are not written to reports.

## Create a bundle

Scaffold the contract from a repository and exact 40-character commit:

```bash
uv run task new my-task \
  --id my-task \
  --repo https://github.com/example/project.git \
  --commit 0123456789abcdef0123456789abcdef01234567
```

Then edit the generated files:

```text
my-task/
├── task.json                  # repository, runtime, and named test commands
├── description.md            # prompt shown to the solver
├── environment/Dockerfile    # repository-owned language and dependencies
├── gold.patch                # trusted reference solution
└── tests/hidden.patch        # trusted evaluator tests
```

The test commands are plain shell commands, so the CLI is not tied to Python, pytest, or SWE-bench. A JavaScript, Go, Rust, or mixed-language repository can provide its own Dockerfile and commands without changing the CLI.

## Commands

| Command | Purpose |
| --- | --- |
| `task new` | Scaffold an editable bundle. |
| `task init` | Fetch the exact commit, build the image, and run a smoke check. |
| `task validate` | Repeat baseline and golden-patch guardrails. |
| `task run` | Execute a solver, capture its patch, and grade it. |
| `task history` | List command IDs from the local SQLite ledger. |
| `task logs` | Show one command's events, test results, and artifacts. |
| `task artifacts` | Verify the integrity of one command's artifact files. |
| `task doctor` | Check Python, Git, Docker CLI, and daemon availability. |

Stable exit codes make the CLI usable in automation:

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Test expectation failed or task remained unresolved |
| `2` | Configuration error or command ID not found |
| `3` | Infrastructure or internal error |
| `4` | Solver failed |

## Evaluation result

The checked-in [evaluation artifact](examples/swe-bench-pro-ansible/evaluation.json) summarizes an end-to-end run against the real Ansible task. All four regression tests passed before and after the patch. The target test failed at baseline, as expected, and passed after the patch, so the run resolved the task.

The source command ID and immutable hashes are included so the summary can be traced to the full local report. Runtime logs, diffs, repository snapshots, environment metadata, and test output remain in the ignored `.taskbundle/` state directory instead of bloating the submission.

## Evaluation criteria

| Criterion | Evidence in this repository |
| --- | --- |
| Correct flow | Fresh baseline, solver, and post-solver containers enforce the PASS_TO_PASS and FAIL_TO_PASS truth table; mixed and unresolved outcomes are tested. |
| Clear UX | Rich summaries, stable exit codes, structured `--json` reports, recovery hints, and command-ID log queries. |
| Reproducibility | Exact commits, image IDs, input hashes, execution fingerprints, repetitions, and pristine-state checks. |
| Safety | No host mounts, read-only root filesystem, disposable work volume, bounded resources, dropped capabilities, and network-off defaults. |
| Code quality | Strict models, narrow Git/Docker/process adapters, typed errors, unit tests, real Docker integration, Ruff, and mypy. |
| Beyond the minimum | `new`, `doctor`, `history`, `logs`, artifact integrity verification, phase snapshots, and provenance reports. |

## Development

Run the local quality gates with:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest -q
uv build --offline
```

The full suite uses fakes for fast feedback. The opt-in Docker integration test exercises a real local repository and daemon:

```bash
TASKBUNDLE_RUN_DOCKER_TESTS=1 uv run pytest -q -m docker
```

The latest validation completed all unit tests, the Docker integration test, static analysis, and an offline package build. See [DESIGN.md](DESIGN.md) for the security boundary, reproducibility model, observability choices, and deliberate omissions.
