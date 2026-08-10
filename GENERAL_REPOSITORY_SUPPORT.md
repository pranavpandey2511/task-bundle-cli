# General Repository Support

## Current status

I have not solved for arbitrary repositories in this assignment. The included end-to-end examples use Python and pytest, so they do not prove that the system works across languages, test frameworks, or repositories with services such as databases.

The current Docker-based boundary gives a useful direction, but general repository support should be treated as future work rather than a completed feature.

The main risk is solving this by adding more language-specific branches to the CLI. A repository may use several languages, a custom build system, or unusual test commands. File detection can help create a starting template, but it is not reliable enough to decide how a task should be built or graded.

## How I would approach it

I would keep the Task Bundle engine small and language-neutral. It should not need to understand pytest, Jest, Cargo, Maven, or any other framework. It should only:

1. Check out an exact Git commit.
2. Build an isolated environment.
3. Run a command with time and resource limits.
4. Classify the result as pass, fail, timeout, or error.
5. Capture and grade the candidate's Git patch.

Each task bundle would provide the repository-specific adapter:

- a Dockerfile or pinned OCI image containing the required languages and dependencies;
- non-interactive smoke and test commands;
- hidden evaluator files and commands;
- allowed candidate paths and resource limits.

This means a Python task could run `pytest`, a Node task could run `npm test`, and a Rust task could run `cargo test`, while the engine follows exactly the same lifecycle. Language profiles should only generate starter configuration; correctness must always come from the bundle's explicit environment and commands.

The important interface is therefore not a list of supported frameworks. It is a small execution contract between the engine and a task bundle. For each check, the bundle would declare:

- the command to run;
- the expected result before the fix and after the reference fix;
- which exit codes mean a normal assertion failure;
- the timeout and resource limits;
- which evaluator files must remain hidden from the solver.

The engine can then validate the same truth table for every repository: existing behavior still passes, the target behavior fails at the base commit, and everything passes after the reference patch. Timeouts and broken runners remain errors rather than being mistaken for test failures.

Repository-specific setup would happen while building the image. This is where the bundle can use lockfiles, install compilers, or prepare generated assets. The resulting image should be identified by an immutable digest. Evaluation would then run without external network access by default, which makes repeated runs more reproducible and reduces dependency on live package registries.

## Illustrative configuration

The following snippets show the direction only. They are not part of the current manifest schema.

A small Node task could declare its environment and checks like this:

```json
{
  "schema_version": 4,
  "environment": {
    "dockerfile": "environment/Dockerfile",
    "workdir": "/workspace",
    "shell": ["/bin/bash", "-lc"]
  },
  "checks": [
    {
      "id": "existing-login-behavior",
      "command": ["npm", "test", "--", "login-existing"],
      "expected": {"baseline": "pass", "golden": "pass", "candidate": "pass"}
    },
    {
      "id": "requested-login-fix",
      "command": ["npm", "test", "--", "login-fix"],
      "expected": {"baseline": "fail", "golden": "pass", "candidate": "pass"},
      "failure_exit_codes": [1],
      "timeout_seconds": 120
    }
  ],
  "candidate": {
    "allowed_patch_paths": ["src/"],
    "disallowed_patch_paths": ["src/test/fixtures/"]
  }
}
```

The repository-specific Dockerfile would own dependency installation:

```dockerfile
FROM node:22-bookworm-slim

WORKDIR /workspace
COPY source/package.json source/package-lock.json ./
RUN npm ci
COPY source/ ./
```

The same task config structure could use `cargo test`, `go test`, or a custom script without changing the engine. In a real bundle, the base image would be pinned by digest and dependency lockfiles would be required.

If a later task genuinely needed a database, that capability could be explicit:

```json
{
  "services": [
    {
      "name": "database",
      "image": "postgres:17@sha256:<digest>",
      "healthcheck": ["pg_isready", "-U", "task"]
    }
  ]
}
```

The engine would create a private network for the evaluator and declared services, wait for health checks, and still block external network access.

## Concrete support boundary

A repository would be supported when it can:

- build reproducibly inside a Linux OCI container;
- run deterministic, non-interactive checks;
- use pinned dependencies;
- represent the solver's changes as a Git patch.

Repositories needing Postgres, Redis, or similar services could later declare isolated sidecars with health checks. Native macOS, Windows-only, GPU, or external-SaaS tasks would require separate execution backends.

Capabilities should be explicit and fail closed. For example, a bundle should declare that it needs a particular CPU architecture or a database sidecar. If the local runner cannot provide that capability, initialization should stop with a clear unsupported-environment error instead of attempting a partial run.

## Next validation

Before redesigning the manifest, I would create real Node, Go, and Rust bundles and run the complete `init -> validate -> solve -> grade` lifecycle. I would also add one mixed-language repository and one small service-backed repository. This would expose real framework assumptions instead of designing only from hypothetical cases.

The first likely changes are:

1. Use a custom profile, rather than Python, when a repository cannot be identified.
2. Support explicit command arguments or a declared shell instead of assuming `/bin/sh` syntax.
3. Support generic evaluator assets, including binary fixtures, instead of only hidden Git patches.
4. Add optional isolated services only after a real example requires them.

These examples would form a small conformance suite. A new language would not require a new engine integration; it would be considered supported when its bundle passes the same lifecycle and isolation checks.

Until those examples pass end to end, arbitrary repository support remains a design direction, not a demonstrated capability.
