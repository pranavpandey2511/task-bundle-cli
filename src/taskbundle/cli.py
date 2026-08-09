"""Task Bundle command-line interface."""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from taskbundle import __version__
from taskbundle.artifacts import verify_artifact_records
from taskbundle.config import Bundle, load_bundle
from taskbundle.errors import (
    ErrorKind,
    ExitCode,
    InvalidTaskError,
    TaskBundleError,
)
from taskbundle.lifecycle.initialize import initialize_task
from taskbundle.lifecycle.run import run_task
from taskbundle.lifecycle.validate import validate_task
from taskbundle.models import CommandReport
from taskbundle.process import ProcessRunner
from taskbundle.scaffold import scaffold_bundle
from taskbundle.session import CommandSession

app = typer.Typer(
    name="task",
    help="Build, validate, and run isolated coding-task bundles.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console(stderr=True)

Operation = Callable[[CommandSession], dict[str, Any]]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the CLI version and exit.",
    ),
) -> None:
    """Build, validate, and run isolated coding-task bundles."""


def _emit_json(report: CommandReport) -> None:
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))


def _render_success(report: CommandReport) -> None:
    console.print(f"[green]✓[/green] {report.command} succeeded")
    console.print(f"Command ID: [bold]{report.command_id}[/bold]")
    data = report.data
    if report.command == "new":
        console.print(f"Bundle: {data['bundle']}")
        console.print("Next: edit the generated placeholders, then run [bold]task init[/bold].")
    elif report.command == "init":
        reuse = "reused" if data["reused"] else "built"
        console.print(f"Image: [bold]{data['image_tag']}[/bold] ({reuse})")
        console.print(f"Image ID: {data['image_id']}")
        console.print(f"Smoke command passed in {data['smoke']['duration_seconds']:.3f}s")
    elif report.command == "validate":
        table = Table("Phase", "Suite", "Test", "Expected", "Observed", "Stable")
        for phase, suites in data["phases"].items():
            for suite, tests in suites.items():
                for test in tests:
                    table.add_row(
                        phase,
                        suite,
                        test["test_id"],
                        test["expected"],
                        ", ".join(test["observations"]),
                        "yes" if test["stable"] else "no",
                    )
        console.print(table)
    elif report.command == "run":
        solver = data["solver"]
        console.print(
            f"Solver: [bold]{solver['adapter']}[/bold]; "
            f"captured {solver['patch_bytes']} patch bytes"
        )
        console.print(f"Resolved: [bold]{'yes' if data['resolved'] else 'no'}[/bold]")
        table = Table("Suite", "Test", "Expected", "Observed", "Stable")
        for suite, tests in data["post_solver"]["phases"]["post_solver"].items():
            for test in tests:
                table.add_row(
                    suite,
                    test["test_id"],
                    test["expected"],
                    ", ".join(test["observations"]),
                    "yes" if test["stable"] else "no",
                )
        console.print(table)
    elif report.command == "history":
        table = Table("Command ID", "Command", "Status", "Started", "Exit")
        for command in data["commands"]:
            table.add_row(
                command["id"],
                command["command_name"],
                command["status"],
                command["started_at"],
                "" if command["exit_code"] is None else str(command["exit_code"]),
            )
        console.print(table)
    elif report.command == "logs":
        target = data["command"]
        console.print(
            f"Target: [bold]{target['id']}[/bold] {target['command_name']} ({target['status']})"
        )
        for event in data["events"]:
            console.print(
                f"{event['occurred_at']} [{event['level']}] {event['phase']}: {event['message']}"
            )
        if data["test_results"]:
            console.print("[bold]Test results[/bold]")
            table = Table("Phase", "Suite", "Test", "Attempt", "Expected", "Observed", "Exit")
            for result in data["test_results"]:
                table.add_row(
                    result["phase"],
                    result["suite"],
                    result["test_id"],
                    str(result["attempt"]),
                    result["expected"],
                    result["observed"],
                    "" if result["exit_code"] is None else str(result["exit_code"]),
                )
            console.print(table)
        if data["artifacts"]:
            console.print("[bold]Artifacts[/bold]")
            table = Table()
            table.add_column("Kind")
            table.add_column("Artifact", overflow="fold")
            table.add_column("Bytes", justify="right")
            for artifact in data["artifacts"]:
                display_path = artifact["relative_path"].removeprefix(f"commands/{target['id']}/")
                table.add_row(
                    artifact["kind"],
                    display_path,
                    str(artifact["size_bytes"]),
                )
            console.print(table)
    elif report.command == "artifacts":
        table = Table()
        table.add_column("Kind")
        table.add_column("Artifact", overflow="fold")
        table.add_column("Status")
        table.add_column("Bytes", justify="right")
        for artifact in data["artifacts"]:
            display_path = artifact["relative_path"].removeprefix(
                f"commands/{data['command']['id']}/"
            )
            table.add_row(
                artifact["kind"],
                display_path,
                artifact["status"],
                str(artifact["actual_size_bytes"]),
            )
        console.print(table)
        console.print(f"Verified {data['count']} artifacts for {data['command']['id']}")
    elif report.command == "doctor":
        table = Table("Check", "Status", "Detail")
        for check in data["checks"]:
            status = "[green]ok[/green]" if check["ok"] else "[red]failed[/red]"
            table.add_row(check["name"], status, check["detail"])
        console.print(table)


def _render_failure(report: CommandReport) -> None:
    assert report.error is not None
    console.print(f"[red]✗[/red] {report.error.message}")
    console.print(f"Command ID: [bold]{report.command_id}[/bold]")
    if report.error.hint:
        console.print(f"Hint: {report.error.hint}")
    if report.error.details:
        console.print_json(data=report.error.details)


def _execute(
    *,
    command_name: str,
    bundle_path: Path,
    json_output: bool,
    operation: Operation,
) -> None:
    session = CommandSession.start(command_name=command_name, bundle_path=bundle_path)
    try:
        try:
            data = operation(session)
            report = session.succeed(data)
        except TaskBundleError as error:
            report = session.fail(error)
        except KeyboardInterrupt:
            report = session.fail(
                TaskBundleError(
                    "Command interrupted by the user.",
                    kind=ErrorKind.INFRASTRUCTURE,
                    exit_code=ExitCode.INFRASTRUCTURE,
                    hint=(
                        "Disposable containers were cleaned up; inspect the command events "
                        "before retrying."
                    ),
                )
            )
        except Exception as error:  # defensive CLI boundary; details are persisted
            report = session.fail_unexpected(error)

        if json_output:
            _emit_json(report)
        elif report.status.value == "succeeded":
            _render_success(report)
        else:
            _render_failure(report)
    finally:
        session.close()

    exit_code = 0 if report.status.value == "succeeded" else _report_exit_code(report)
    if exit_code:
        raise typer.Exit(exit_code)


def _report_exit_code(report: CommandReport) -> int:
    assert report.error is not None
    mapping = {
        ErrorKind.CONFIGURATION.value: ExitCode.CONFIGURATION,
        ErrorKind.NOT_FOUND.value: ExitCode.CONFIGURATION,
        ErrorKind.INFRASTRUCTURE.value: ExitCode.INFRASTRUCTURE,
        ErrorKind.INTERNAL.value: ExitCode.INFRASTRUCTURE,
        ErrorKind.INVALID_TASK.value: ExitCode.EXPECTATION_FAILED,
        ErrorKind.UNRESOLVED.value: ExitCode.EXPECTATION_FAILED,
        ErrorKind.SOLVER.value: ExitCode.SOLVER,
    }
    return int(mapping.get(report.error.kind, ExitCode.INFRASTRUCTURE))


@app.command("new")
def new_command(
    bundle: Path = typer.Argument(..., help="Directory to scaffold."),
    repo: str = typer.Option(..., "--repo", help="Git repository URL or local path."),
    commit: str = typer.Option(..., "--commit", help="Exact 40-character Git commit."),
    bundle_id: str = typer.Option(..., "--id", help="Stable lowercase bundle identifier."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Scaffold an editable task bundle."""

    try:
        bundle.expanduser().resolve().mkdir(parents=True, exist_ok=True)
    except OSError as error:
        console.print(f"[red]✗[/red] Could not create bundle directory: {error}")
        raise typer.Exit(ExitCode.INFRASTRUCTURE) from error

    _execute(
        command_name="new",
        bundle_path=bundle,
        json_output=json_output,
        operation=lambda session: _new_operation(
            session, bundle=bundle, repo=repo, commit=commit, bundle_id=bundle_id
        ),
    )


def _new_operation(
    session: CommandSession, *, bundle: Path, repo: str, commit: str, bundle_id: str
) -> dict[str, Any]:
    data = scaffold_bundle(root=bundle, repo=repo, commit=commit, bundle_id=bundle_id)
    session.attach_bundle(data["bundle_id"])
    session.event("info", "scaffold", "Bundle scaffold created.", {"files": data["created_files"]})
    return data


def _load_for_lifecycle(session: CommandSession, bundle_path: Path) -> Bundle:
    bundle = load_bundle(bundle_path)
    session.attach_bundle(bundle.manifest.id)
    session.event("info", "bundle", "Bundle manifest and required files are valid.")
    return bundle


@app.command("init")
def init_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    force_rebuild: bool = typer.Option(
        False,
        "--force-rebuild",
        help="Ignore matching build metadata and rebuild the image.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Materialize the exact repository and build its base image."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        return initialize_task(bundle=loaded, session=session, force_rebuild=force_rebuild)

    _execute(command_name="init", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("validate")
def validate_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    repetitions: int | None = typer.Option(
        None,
        "--repetitions",
        min=1,
        max=20,
        help="Override the manifest's validation repetition count.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Validate baseline and golden test expectations."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        return validate_task(bundle=loaded, session=session, repetitions=repetitions)

    _execute(
        command_name="validate", bundle_path=bundle, json_output=json_output, operation=operation
    )


@app.command("run")
def run_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    solver: str = typer.Option("stub", "--solver", help="Solver adapter: stub, patch, or command."),
    solver_command: str | None = typer.Option(
        None, "--solver-cmd", help="Command used by the command solver."
    ),
    candidate_patch: Path | None = typer.Option(
        None, "--candidate-patch", help="Patch file used by the patch solver."
    ),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Permit solver network only when task.json also opts in.",
    ),
    secret_environment_names: list[str] | None = typer.Option(
        None,
        "--secret-env",
        help="Environment-variable name to forward to the solver; repeat as needed.",
    ),
    repetitions: int | None = typer.Option(
        None,
        "--repetitions",
        min=1,
        max=20,
        help="Override the manifest's repetition count for preflight and grading.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Run a solver and grade its captured patch."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        return run_task(
            bundle=loaded,
            session=session,
            solver_name=solver,
            solver_command=solver_command,
            candidate_patch=candidate_patch,
            allow_network=allow_network,
            secret_environment_names=secret_environment_names,
            repetitions=repetitions,
        )

    _execute(command_name="run", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("history")
def history_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    limit: int = typer.Option(20, min=1, max=500, help="Maximum commands to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """List recent command records for a bundle."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = load_bundle(bundle, require_files=False)
        session.attach_bundle(loaded.manifest.id)
        commands = [
            row
            for row in session.database.list_commands(limit=limit + 1)
            if row["id"] != session.command_id
        ][:limit]
        session.event("info", "query", "Command history queried.", {"count": len(commands)})
        return {"commands": commands, "count": len(commands)}

    _execute(
        command_name="history", bundle_path=bundle, json_output=json_output, operation=operation
    )


@app.command("logs")
def logs_command(
    command_id: str = typer.Argument(..., help="Command ID to inspect."),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Show a command, its events, test results, and artifacts."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = load_bundle(bundle, require_files=False)
        session.attach_bundle(loaded.manifest.id)
        target = session.database.get_command(command_id)
        if target is None:
            raise TaskBundleError(
                f"Command ID was not found: {command_id}",
                kind=ErrorKind.NOT_FOUND,
                exit_code=ExitCode.CONFIGURATION,
                hint="Run `task history` to list available command IDs.",
            )
        data = {
            "command": target,
            "events": session.database.get_events(command_id),
            "test_results": session.database.get_test_results(command_id),
            "artifacts": session.database.get_artifacts(command_id),
        }
        session.event("info", "query", "Command logs queried.", {"target_command_id": command_id})
        return data

    _execute(command_name="logs", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("artifacts")
def artifacts_command(
    command_id: str = typer.Argument(..., help="Command ID whose artifacts should be verified."),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """List and verify a command's artifact hashes and sizes."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = load_bundle(bundle, require_files=False)
        session.attach_bundle(loaded.manifest.id)
        target = session.database.get_command(command_id)
        if target is None:
            raise TaskBundleError(
                f"Command ID was not found: {command_id}",
                kind=ErrorKind.NOT_FOUND,
                exit_code=ExitCode.CONFIGURATION,
                hint="Run `task history` to list available command IDs.",
            )
        verification = verify_artifact_records(
            state_dir=session.state_dir,
            records=session.database.get_artifacts(command_id),
        )
        data = {"command": target, **verification}
        session.event(
            "info",
            "query",
            "Command artifacts verified.",
            {
                "target_command_id": command_id,
                "count": verification["count"],
                "valid": verification["valid"],
            },
        )
        if not verification["valid"]:
            raise InvalidTaskError(
                f"One or more artifacts failed integrity verification for {command_id}.",
                hint="Inspect missing, unsafe_path, or mismatch entries before trusting the run.",
                details=data,
            )
        return data

    _execute(
        command_name="artifacts", bundle_path=bundle, json_output=json_output, operation=operation
    )


@app.command("doctor")
def doctor_command(
    bundle: Path = typer.Argument(Path("."), help="Directory used for the command ledger."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Check local Python, Git, Docker CLI, and Docker daemon availability."""

    def operation(session: CommandSession) -> dict[str, Any]:
        runner = ProcessRunner()
        checks: list[dict[str, Any]] = []
        docker_executable = os.environ.get("TASKBUNDLE_DOCKER_BIN", "docker")

        python_ok = sys.version_info >= (3, 12)
        checks.append(
            {
                "name": "python",
                "ok": python_ok,
                "detail": ".".join(str(part) for part in sys.version_info[:3]),
            }
        )

        for name, version_args in (
            ("git", ["git", "--version"]),
            ("docker-cli", [docker_executable, "--version"]),
        ):
            executable = shutil.which(version_args[0])
            if executable is None:
                checks.append({"name": name, "ok": False, "detail": "not found on PATH"})
                continue
            result = runner.run(version_args, timeout_seconds=10)
            detail = (result.stdout or result.stderr).strip()
            checks.append({"name": name, "ok": result.succeeded, "detail": detail})

        if shutil.which(docker_executable) is None:
            checks.append({"name": "docker-daemon", "ok": False, "detail": "CLI unavailable"})
        else:
            daemon = runner.run(
                [docker_executable, "info", "--format", "{{.ServerVersion}}"],
                timeout_seconds=15,
            )
            detail = (daemon.stdout or daemon.stderr).strip()
            checks.append({"name": "docker-daemon", "ok": daemon.succeeded, "detail": detail})

        session.event("info", "doctor", "Environment checks completed.", {"checks": checks})
        failures = [check["name"] for check in checks if not check["ok"]]
        if failures:
            raise TaskBundleError(
                "One or more required environment checks failed.",
                kind=ErrorKind.INFRASTRUCTURE,
                exit_code=ExitCode.INFRASTRUCTURE,
                hint="Install or start the failed dependency, then rerun `task doctor`.",
                details={"checks": checks, "failed": failures},
            )
        return {"checks": checks}

    _execute(
        command_name="doctor", bundle_path=bundle, json_output=json_output, operation=operation
    )


if __name__ == "__main__":
    app()
