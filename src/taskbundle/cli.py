"""Task Bundle command-line interface."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from taskbundle import __version__
from taskbundle.artifacts import verify_artifact_records
from taskbundle.authoring import check_bundle
from taskbundle.config import Bundle, load_bundle
from taskbundle.diagnostics import diagnose_command
from taskbundle.engine.docker import DockerClient
from taskbundle.errors import (
    ConfigurationError,
    ErrorKind,
    ExitCode,
    InvalidTaskError,
    TaskBundleError,
)
from taskbundle.evidence import export_command_evidence
from taskbundle.lifecycle.initialize import initialize_task
from taskbundle.lifecycle.run import run_task
from taskbundle.lifecycle.validate import validate_task
from taskbundle.models import CommandReport, EvaluatorIsolation
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
INSPECTION_COMMANDS = ("artifacts", "diagnose", "export", "history", "logs", "report")
REPORT_LIFECYCLE_COMMANDS = ("new", "init", "validate", "run")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


def _interrupt_on_sigterm(_signum: int, _frame: FrameType | None) -> None:
    """Route service-manager termination through normal cleanup and reporting."""

    raise KeyboardInterrupt


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
        profile = data.get("profile", {})
        if profile:
            console.print(
                f"Starter profile: [bold]{profile.get('selected', 'unknown')}[/bold] "
                f"({profile.get('source', 'configured')})"
            )
        readiness = data.get("readiness", {})
        for item in readiness.get("todo", []):
            console.print(f"[yellow]TODO[/yellow] {item}")
        console.print(
            "Next: define evaluator tests and candidate paths, edit the generated placeholders, "
            "then run [bold]task validate --static[/bold]."
        )
    elif report.command == "init":
        reuse = "reused" if data["reused"] else "built"
        console.print(f"Evaluator image: [bold]{data['image_tag']}[/bold] ({reuse})")
        console.print(f"Evaluator image ID: {data['image_id']}")
        console.print(f"Solver image: [bold]{data['solver_image_tag']}[/bold] ({reuse})")
        console.print(f"Solver image ID: {data['solver_image_id']}")
        console.print(f"Smoke command passed in {data['smoke']['duration_seconds']:.3f}s")
    elif report.command == "validate" and data.get("mode") == "static":
        table = Table("Check", "Status", "Detail")
        for check in data["checks"]:
            status = (
                "[yellow]warning[/yellow]"
                if check["status"] == "warning"
                else "[green]pass[/green]"
            )
            detail = check["detail"]
            if check.get("recommendation"):
                detail = f"{detail}\nNext: {check['recommendation']}"
            table.add_row(check["name"], status, detail)
        console.print(table)
        console.print(
            f"Static bundle contract passed with {data['warning_count']} warning(s); "
            "Docker was not used."
        )
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
    elif report.command == "check":
        table = Table("Check", "Status", "Detail")
        for check in data["checks"]:
            status = (
                "[yellow]warning[/yellow]"
                if check["status"] == "warning"
                else "[green]pass[/green]"
            )
            detail = check["detail"]
            if check.get("recommendation"):
                detail = f"{detail}\nNext: {check['recommendation']}"
            table.add_row(check["name"], status, detail)
        console.print(table)
        console.print(
            f"Static bundle contract passed with {data['warning_count']} warning(s); "
            "Docker was not used."
        )
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
    elif report.command == "diagnose":
        summary = data["summary"]
        console.print(
            f"Target: [bold]{data['command']['id']}[/bold] "
            f"{data['command']['command_name']} ({summary['status']})"
        )
        console.print(
            f"Evidence: [bold]{summary['artifact_integrity']}[/bold]; "
            f"unexpected attempts: {summary['failing_attempts']}; "
            f"flaky tests: {summary['flaky_tests']}; snapshots: {summary['snapshot_count']}"
        )
        table = Table("Severity", "Category", "Finding")
        for finding in data["findings"]:
            table.add_row(finding["severity"], finding["category"], finding["message"])
        console.print(table)
        if data["next_actions"]:
            console.print("[bold]Next actions[/bold]")
            for index, action in enumerate(data["next_actions"], start=1):
                console.print(f"{index}. {action}")
    elif report.command == "export":
        console.print(f"Evidence archive: [bold]{data['output']}[/bold]")
        console.print(
            f"SHA-256: {data['sha256']} ({data['size_bytes']} bytes, {data['entry_count']} entries)"
        )
        console.print(
            "[yellow]Contains evaluator material and solver logs; do not expose it to a solver "
            "before grading.[/yellow]"
        )
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
    elif report.command == "report":
        mode = data.get("mode")
        if mode == "list":
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
        elif mode == "export":
            console.print(f"Evidence archive: [bold]{data['output']}[/bold]")
            console.print(
                f"SHA-256: {data['sha256']} "
                f"({data['size_bytes']} bytes, {data['entry_count']} entries)"
            )
            console.print(
                "[yellow]Contains evaluator material and solver logs; do not expose it to a "
                "solver before grading.[/yellow]"
            )
        else:
            summary = data["summary"]
            console.print(
                f"Target: [bold]{data['command']['id']}[/bold] "
                f"{data['command']['command_name']} ({summary['status']})"
            )
            console.print(
                f"Evidence: [bold]{summary['artifact_integrity']}[/bold]; "
                f"unexpected attempts: {summary['failing_attempts']}; "
                f"flaky tests: {summary['flaky_tests']}"
            )
            if data.get("html_report"):
                console.print(f"HTML review: [bold cyan]{data['html_report']}[/bold cyan]")
            if data.get("report_index"):
                console.print(f"All versions: [cyan]{data['report_index']}[/cyan]")
            findings = Table("Severity", "Category", "Finding")
            for finding in data["findings"]:
                findings.add_row(finding["severity"], finding["category"], finding["message"])
            console.print(findings)
            if data["next_actions"]:
                console.print("[bold]Next actions[/bold]")
                for index, action in enumerate(data["next_actions"], start=1):
                    console.print(f"{index}. {action}")
            if data.get("events"):
                console.print("[bold]Lifecycle events[/bold]")
                for event in data["events"]:
                    console.print(
                        f"{event['occurred_at']} [{event['level']}] "
                        f"{event['phase']}: {event['message']}"
                    )


def _render_failure(report: CommandReport) -> None:
    assert report.error is not None
    console.print(f"[red]✗[/red] {report.error.message}")
    console.print(f"Command ID: [bold]{report.command_id}[/bold]")
    if report.error.hint:
        console.print(f"Hint: {report.error.hint}")
    if report.error.details:
        console.print_json(data=report.error.details)


def _shell_command(*parts: str | Path) -> str:
    return shlex.join(str(part) for part in parts)


def _target_id_from_report(report: CommandReport) -> str:
    targets: list[object] = [report.data.get("command")]
    commands = report.data.get("commands")
    if isinstance(commands, list) and commands:
        targets.append(commands[0])
    if report.error is not None:
        targets.append(report.error.details.get("command"))
    for target in targets:
        if isinstance(target, dict):
            target_id = target.get("id")
            if isinstance(target_id, str):
                return target_id
    return report.command_id


def _quantity(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def _result_summary(report: CommandReport) -> str:
    if report.error is not None:
        return report.error.message
    data = report.data
    if report.command == "new":
        profile = data.get("profile", {}).get("selected", "custom")
        todo_count = len(data.get("readiness", {}).get("todo", []))
        return (
            f"Created {_quantity(len(data.get('created_files', [])), 'bundle file')} "
            f"with the {profile} starter and {_quantity(todo_count, 'authoring TODO')}."
        )
    if report.command == "check":
        return f"Static contract passed with {data.get('warning_count', 0)} warning(s)."
    if report.command == "init":
        action = "Reused" if data.get("reused") else "Built"
        return f"{action} evaluator and solver images; the smoke command passed."
    if report.command == "validate":
        if data.get("mode") == "static":
            return f"Static contract passed with {data.get('warning_count', 0)} warning(s)."
        attempts = int(data.get("attempt_count", 0))
        flaky = len(data.get("flaky", []))
        evaluator_isolation = data.get("evaluator_isolation", "not recorded")
        return (
            f"Truth table passed across {_quantity(attempts, 'test attempt')} with "
            f"{_quantity(flaky, 'flaky test')} using {evaluator_isolation} evaluator isolation."
        )
    if report.command == "run":
        solver = data.get("solver", {})
        return (
            f"Solver resolved={'yes' if data.get('resolved') else 'no'}; "
            f"captured {solver.get('patch_bytes', 0)} patch bytes."
        )
    if report.command == "history":
        return f"Listed {_quantity(int(data.get('count', 0)), 'recorded command')}."
    if report.command == "logs":
        event_count = len(data.get("events", []))
        test_count = len(data.get("test_results", []))
        artifact_count = len(data.get("artifacts", []))
        return (
            f"Loaded {_quantity(event_count, 'event')}, "
            f"{_quantity(test_count, 'test attempt')}, and "
            f"{_quantity(artifact_count, 'artifact')}."
        )
    if report.command == "diagnose":
        summary = data.get("summary", {})
        failing = int(summary.get("failing_attempts", 0))
        flaky = int(summary.get("flaky_tests", 0))
        return (
            f"Found {_quantity(failing, 'unexpected attempt')}, "
            f"{_quantity(flaky, 'flaky test')}, and "
            f"artifact integrity={summary.get('artifact_integrity', 'unknown')}."
        )
    if report.command == "artifacts":
        return f"Verified {_quantity(int(data.get('count', 0)), 'artifact')}."
    if report.command == "export":
        entries = int(data.get("entry_count", 0))
        quantity = _quantity(entries, "evidence entry", "evidence entries")
        return f"Wrote {quantity} to {data.get('output')}."
    if report.command == "doctor":
        checks = data.get("checks", [])
        return f"Passed {sum(bool(check.get('ok')) for check in checks)}/{len(checks)} checks."
    if report.command == "report":
        mode = data.get("mode")
        if mode == "list":
            return f"Listed {_quantity(int(data.get('count', 0)), 'lifecycle report')}."
        if mode == "export":
            return f"Wrote verified evidence to {data.get('output')}."
        summary = data.get("summary", {})
        return (
            f"Diagnosed {data.get('command', {}).get('id', 'the selected command')}; "
            f"artifact integrity={summary.get('artifact_integrity', 'unknown')}."
        )
    return f"{report.command} completed successfully."


def _next_commands(report: CommandReport, bundle_root: Path) -> list[str]:
    bundle = str(bundle_root)
    target_id = _target_id_from_report(report)

    def lifecycle(command: str, *arguments: str) -> str:
        return _shell_command("task", command, bundle, *arguments)

    def inspect(command: str, *, exact: bool = False) -> str:
        parts: list[str | Path] = ["task", command]
        if exact:
            parts.append(target_id)
        parts.extend(["--bundle", bundle])
        return _shell_command(*parts)

    if report.error is not None:
        commands = [
            inspect("report", exact=True),
            _shell_command("task", "report", target_id, "--bundle", bundle, "--events"),
        ]
        if report.error.kind in {ErrorKind.CONFIGURATION.value, ErrorKind.NOT_FOUND.value}:
            commands.append(_shell_command("task", report.command, "--help"))
        elif report.error.kind == ErrorKind.INVALID_TASK.value:
            commands.append(lifecycle("validate", "--static"))
        elif report.error.kind in {
            ErrorKind.INFRASTRUCTURE.value,
            ErrorKind.INTERNAL.value,
        }:
            commands.append(_shell_command("task", "doctor", bundle))
        elif report.error.kind in {ErrorKind.SOLVER.value, ErrorKind.UNRESOLVED.value}:
            commands.append(
                lifecycle("run", "--solver", "patch", "--candidate-patch", "candidate.patch")
            )
        return commands
    if report.command == "new":
        return [lifecycle("validate", "--static"), lifecycle("init")]
    if report.command == "check":
        return [lifecycle("init"), inspect("report", exact=True)]
    if report.command == "init":
        return [lifecycle("validate"), inspect("report")]
    if report.command == "validate":
        if report.data.get("mode") == "static":
            return [lifecycle("init"), lifecycle("validate"), inspect("report", exact=True)]
        return [
            lifecycle("run", "--solver", "patch", "--candidate-patch", "candidate.patch"),
            lifecycle("run", "--solver", "agent"),
            inspect("report"),
        ]
    if report.command == "run":
        return [inspect("report"), _shell_command("task", "report", "--bundle", bundle, "--list")]
    if report.command == "history":
        if not report.data.get("commands"):
            if (bundle_root / "task.json").is_file():
                return [lifecycle("check")]
            return [_shell_command("task", "new", "--help")]
        return [inspect("logs"), inspect("diagnose")]
    if report.command == "logs":
        return [
            inspect("diagnose", exact=True),
            inspect("artifacts", exact=True),
            inspect("export", exact=True),
        ]
    if report.command == "diagnose":
        return [
            inspect("logs", exact=True),
            inspect("artifacts", exact=True),
            inspect("export", exact=True),
        ]
    if report.command == "artifacts":
        return [inspect("diagnose", exact=True), inspect("export", exact=True)]
    if report.command == "export":
        return [
            _shell_command("unzip", "-l", str(report.data.get("output", "evidence.zip"))),
            inspect("diagnose", exact=True),
        ]
    if report.command == "doctor":
        if (bundle_root / "task.json").is_file():
            return [lifecycle("check")]
        return [_shell_command("task", "new", "--help")]
    if report.command == "report":
        if report.data.get("mode") == "list":
            commands = report.data.get("commands", [])
            if commands:
                return [inspect("report", exact=True)]
            return [lifecycle("validate", "--static")]
        if report.data.get("mode") == "export":
            return [_shell_command("unzip", "-l", str(report.data.get("output", "evidence.zip")))]
        return [
            _shell_command("task", "report", target_id, "--bundle", bundle, "--events"),
            _shell_command("task", "report", "--bundle", bundle, "--list"),
        ]
    return [inspect("logs", exact=True)]


def _render_guidance(report: CommandReport, bundle_root: Path) -> None:
    target_id = _target_id_from_report(report)
    status = "[green]succeeded[/green]" if report.error is None else "[red]failed[/red]"
    console.rule("[bold]Summary & next steps[/bold]")
    console.print(f"Status: {status}  Command: [bold]{report.command}[/bold]")
    console.print(Text.assemble("Bundle: ", (str(bundle_root), "bold")), soft_wrap=True)
    console.print(f"Command ID: [bold]{report.command_id}[/bold]")
    if target_id != report.command_id:
        console.print(f"Target command ID: [bold]{target_id}[/bold]")
    console.print(Text.assemble("Result: ", _result_summary(report)))
    if report.html_report is not None:
        console.print(
            Text.assemble(
                "HTML review: ",
                (str(bundle_root / ".taskbundle" / report.html_report), "bold cyan"),
            ),
            soft_wrap=True,
        )
        console.print(
            Text.assemble(
                "All versions: ",
                (str(bundle_root / ".taskbundle" / str(report.report_index)), "cyan"),
            ),
            soft_wrap=True,
        )
    console.print("[bold]Next commands (copy and paste)[/bold]")
    for command in _next_commands(report, bundle_root):
        console.print(Text(f"  $ {command}", style="cyan"), soft_wrap=True)
    console.print(
        "Tip: inside the bundle directory, `task report` needs no ID or --bundle; "
        "it selects the latest non-inspection lifecycle command."
    )


def _execute(
    *,
    command_name: str,
    bundle_path: Path,
    json_output: bool,
    operation: Operation,
) -> None:
    session = CommandSession.start(command_name=command_name, bundle_path=bundle_path)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _interrupt_on_sigterm)
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
            _render_guidance(report, session.state_dir.parent)
        else:
            _render_failure(report)
            _render_guidance(report, session.state_dir.parent)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
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
    profile: str = typer.Option(
        "auto",
        "--profile",
        help="Starter profile: auto, python, node, go, rust, or custom.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Create a profile-aware task draft, readiness checklist, and HTML authoring report."""

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
            session,
            bundle=bundle,
            repo=repo,
            commit=commit,
            bundle_id=bundle_id,
            profile=profile,
        ),
    )


def _new_operation(
    session: CommandSession,
    *,
    bundle: Path,
    repo: str,
    commit: str,
    bundle_id: str,
    profile: str,
) -> dict[str, Any]:
    data = scaffold_bundle(
        root=bundle,
        repo=repo,
        commit=commit,
        bundle_id=bundle_id,
        profile=profile,
    )
    session.attach_bundle(data["bundle_id"])
    session.event("info", "scaffold", "Bundle scaffold created.", {"files": data["created_files"]})
    return data


def _load_for_lifecycle(session: CommandSession, bundle_path: Path) -> Bundle:
    bundle = load_bundle(bundle_path)
    session.attach_bundle(bundle.manifest.id)
    session.event("info", "bundle", "Bundle manifest and required files are valid.")
    return bundle


def _target_command(session: CommandSession, command_id: str | None) -> dict[str, Any]:
    requested_id = command_id or "latest"
    target = (
        session.database.get_latest_command(
            exclude_id=session.command_id,
            exclude_names=INSPECTION_COMMANDS,
        )
        if requested_id == "latest"
        else session.database.get_command(requested_id)
    )
    if target is None:
        if requested_id == "latest":
            message = "No prior non-inspection command was found."
            hint = "Run `task new`, `task init`, `task validate`, or `task run` first."
        else:
            message = f"Command ID was not found: {requested_id}"
            hint = "Run `task report --list` to list available lifecycle command IDs."
        raise TaskBundleError(
            message,
            kind=ErrorKind.NOT_FOUND,
            exit_code=ExitCode.CONFIGURATION,
            hint=hint,
        )
    bundle_id = target.get("bundle_id")
    if isinstance(bundle_id, str):
        session.attach_bundle(bundle_id)
    session.event(
        "info",
        "query",
        "Target command selected.",
        {"requested": requested_id, "target_command_id": target["id"]},
    )
    return target


def _target_report_command(session: CommandSession, command_id: str | None) -> dict[str, Any]:
    if command_id is not None:
        target = _target_command(session, command_id)
        if target["command_name"] not in REPORT_LIFECYCLE_COMMANDS:
            raise ConfigurationError(
                f"Command is not a lifecycle report: {command_id}",
                hint="Run `task report --list` to select a new, init, validate, or run command.",
            )
        return target

    selected: dict[str, Any] | None = next(
        (
            command
            for command in session.database.list_commands(limit=500)
            if command["id"] != session.command_id
            and command["command_name"] in REPORT_LIFECYCLE_COMMANDS
        ),
        None,
    )
    if selected is None:
        raise TaskBundleError(
            "No prior lifecycle report was found.",
            kind=ErrorKind.NOT_FOUND,
            exit_code=ExitCode.CONFIGURATION,
            hint="Run `task new`, `task init`, `task validate`, or `task run` first.",
        )
    bundle_id = selected.get("bundle_id")
    if isinstance(bundle_id, str):
        session.attach_bundle(bundle_id)
    session.event(
        "info",
        "query",
        "Lifecycle report selected.",
        {"requested": "latest", "target_command_id": selected["id"]},
    )
    return selected


@app.command("check", hidden=True)
def check_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Run fast language-neutral contract checks without Docker."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        result = check_bundle(loaded)
        session.event(
            "info",
            "check",
            "Static authoring checks completed without Docker.",
            {"warning_count": result["warning_count"]},
        )
        return result

    _execute(command_name="check", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("init")
def init_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    force_rebuild: bool = typer.Option(
        False,
        "--force-rebuild",
        help="Ignore matching metadata and rebuild evaluator and solver images without cache.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Materialize the exact repository and build evaluator and redacted solver images."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        return initialize_task(bundle=loaded, session=session, force_rebuild=force_rebuild)

    _execute(command_name="init", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("validate")
def validate_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    static: bool = typer.Option(
        False,
        "--static",
        help="Run only fast authoring and portability checks without Docker.",
    ),
    repetitions: int | None = typer.Option(
        None,
        "--repetitions",
        min=1,
        max=20,
        help="Override the manifest's validation repetition count.",
    ),
    evaluator_isolation: EvaluatorIsolation | None = typer.Option(
        None,
        "--evaluator-isolation",
        help="Override evaluator reuse: phase or test-attempt.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Verify PASS_TO_PASS and FAIL_TO_PASS tests on the unmodified baseline."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        if static:
            result = {"mode": "static", **check_bundle(loaded)}
            session.event(
                "info",
                "check",
                "Static authoring checks completed without Docker.",
                {"warning_count": result["warning_count"]},
            )
            return result
        return validate_task(
            bundle=loaded,
            session=session,
            repetitions=repetitions,
            evaluator_isolation=evaluator_isolation,
        )

    _execute(
        command_name="validate", bundle_path=bundle, json_output=json_output, operation=operation
    )


@app.command("run")
def run_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    solver: str = typer.Option(
        "agent",
        "--solver",
        help="Solver adapter: agent, patch, or stub.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="OpenRouter model; overrides OPENROUTER_MODEL and the built-in default.",
    ),
    candidate_patch: Path | None = typer.Option(
        None,
        "--candidate-patch",
        help="Existing patch to apply in the sanitized solver and grade in fresh evaluators.",
    ),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Reserved flag; direct solver-container networking is always rejected.",
    ),
    api_key_env: str = typer.Option(
        "OPENROUTER_API_KEY",
        "--api-key-env",
        help="Environment variable containing the OpenRouter API key.",
    ),
    env_file: Path = typer.Option(
        Path(".env"),
        "--env-file",
        help="Dotenv file for OpenRouter settings; shell variables take precedence.",
    ),
    agent_max_steps: int = typer.Option(
        24,
        "--agent-max-steps",
        min=1,
        max=100,
        help="Maximum OpenRouter agent turns.",
    ),
    repetitions: int | None = typer.Option(
        None,
        "--repetitions",
        min=1,
        max=20,
        help="Override the manifest's repetition count for preflight and grading.",
    ),
    evaluator_isolation: EvaluatorIsolation | None = typer.Option(
        None,
        "--evaluator-isolation",
        help="Override evaluator reuse: phase or test-attempt.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Run a sanitized solver, enforce its patch policy, and grade in fresh evaluators."""

    def operation(session: CommandSession) -> dict[str, Any]:
        loaded = _load_for_lifecycle(session, bundle)
        return run_task(
            bundle=loaded,
            session=session,
            solver_name=solver,
            candidate_patch=candidate_patch,
            allow_network=allow_network,
            agent_model=model,
            agent_api_key_env=api_key_env,
            agent_env_file=env_file,
            agent_max_steps=agent_max_steps,
            repetitions=repetitions,
            evaluator_isolation=evaluator_isolation,
        )

    _execute(command_name="run", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("report")
def report_command(
    command_id: str | None = typer.Argument(
        None,
        help="Lifecycle command ID; defaults to the latest new/init/validate/run report.",
    ),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    list_results: bool = typer.Option(
        False,
        "--list",
        help="List lifecycle report versions instead of opening one report.",
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="Maximum reports to list."),
    include_events: bool = typer.Option(
        False,
        "--events",
        help="Include the factual lifecycle event timeline in terminal and JSON output.",
    ),
    export_output: Path | None = typer.Option(
        None,
        "--export",
        help="Write a verified deterministic evidence ZIP to this path.",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing export ZIP."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Understand, list, verify, inspect, or export lifecycle results and HTML reviews."""

    def operation(session: CommandSession) -> dict[str, Any]:
        if list_results:
            if command_id is not None or include_events or export_output is not None or force:
                raise ConfigurationError(
                    "--list cannot be combined with COMMAND_ID, --events, --export, or --force."
                )
            commands = [
                command
                for command in session.database.list_commands(limit=500)
                if command["id"] != session.command_id
                and command["command_name"] in REPORT_LIFECYCLE_COMMANDS
            ][:limit]
            if commands and isinstance(commands[0].get("bundle_id"), str):
                session.attach_bundle(commands[0]["bundle_id"])
            session.event("info", "query", "Lifecycle reports listed.", {"count": len(commands)})
            return {"mode": "list", "commands": commands, "count": len(commands)}

        if force and export_output is None:
            raise ConfigurationError("--force is only valid together with --export.")
        if include_events and export_output is not None:
            raise ConfigurationError("--events and --export select different report output modes.")

        target = _target_report_command(session, command_id)
        target_id = str(target["id"])
        events = session.database.get_events(target_id)
        test_results = session.database.get_test_results(target_id)
        artifact_records = session.database.get_artifacts(target_id)

        if export_output is not None:
            exported = export_command_evidence(
                state_dir=session.state_dir,
                destination=export_output,
                command=target,
                events=events,
                test_results=test_results,
                artifact_records=artifact_records,
                force=force,
            )
            session.event(
                "info",
                "export",
                "Verified lifecycle evidence exported from task report.",
                {"target_command_id": target_id, "output": exported["output"]},
            )
            return {"mode": "export", **exported}

        diagnosis = diagnose_command(
            state_dir=session.state_dir,
            command=target,
            events=events,
            test_results=test_results,
            artifact_records=artifact_records,
        )
        html_record = next(
            (record for record in artifact_records if record["kind"] == "html_report"),
            None,
        )
        html_path = (
            session.state_dir / str(html_record["relative_path"])
            if html_record is not None
            else None
        )
        report_index = session.state_dir / "reports" / "index.html"
        data = {
            "mode": "show",
            **diagnosis,
            "event_count": len(events),
            "events": events if include_events else [],
            "test_results": test_results,
            "html_report": (
                str(html_path) if html_path is not None and html_path.is_file() else None
            ),
            "report_index": str(report_index) if report_index.is_file() else None,
        }
        if diagnosis["summary"]["artifact_integrity"] != "verified":
            raise InvalidTaskError(
                f"One or more artifacts failed integrity verification for {target_id}.",
                hint="Repair the missing or mismatched evidence before trusting this report.",
                details=data,
            )
        session.event(
            "info",
            "query",
            "Lifecycle report diagnosed and artifact integrity verified.",
            {
                "target_command_id": target_id,
                "artifact_integrity": diagnosis["summary"]["artifact_integrity"],
                "html_report": data["html_report"],
            },
        )
        return data

    _execute(
        command_name="report",
        bundle_path=bundle,
        json_output=json_output,
        operation=operation,
    )


@app.command("history", hidden=True)
def history_command(
    bundle: Path = typer.Argument(Path("."), help="Task bundle directory."),
    limit: int = typer.Option(20, min=1, max=500, help="Maximum commands to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """List recent command records for a bundle."""

    def operation(session: CommandSession) -> dict[str, Any]:
        commands = [
            row
            for row in session.database.list_commands(limit=limit + 1)
            if row["id"] != session.command_id
        ][:limit]
        if commands and isinstance(commands[0].get("bundle_id"), str):
            session.attach_bundle(commands[0]["bundle_id"])
        session.event("info", "query", "Command history queried.", {"count": len(commands)})
        return {"commands": commands, "count": len(commands)}

    _execute(
        command_name="history", bundle_path=bundle, json_output=json_output, operation=operation
    )


@app.command("logs", hidden=True)
def logs_command(
    command_id: str | None = typer.Argument(
        None,
        help="Command ID to inspect; defaults to the latest non-inspection command.",
    ),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Show a command, its events, test results, and artifacts."""

    def operation(session: CommandSession) -> dict[str, Any]:
        target = _target_command(session, command_id)
        target_id = str(target["id"])
        data = {
            "command": target,
            "events": session.database.get_events(target_id),
            "test_results": session.database.get_test_results(target_id),
            "artifacts": session.database.get_artifacts(target_id),
        }
        session.event("info", "query", "Command logs queried.", {"target_command_id": target_id})
        return data

    _execute(command_name="logs", bundle_path=bundle, json_output=json_output, operation=operation)


@app.command("diagnose", hidden=True)
def diagnose_command_cli(
    command_id: str | None = typer.Argument(
        None,
        help="Command ID to diagnose; defaults to the latest non-inspection command.",
    ),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Triage failures, test observations, snapshots, and evidence integrity."""

    def operation(session: CommandSession) -> dict[str, Any]:
        target = _target_command(session, command_id)
        target_id = str(target["id"])
        data = diagnose_command(
            state_dir=session.state_dir,
            command=target,
            events=session.database.get_events(target_id),
            test_results=session.database.get_test_results(target_id),
            artifact_records=session.database.get_artifacts(target_id),
        )
        session.event(
            "info",
            "query",
            "Command evidence diagnosed.",
            {
                "target_command_id": target_id,
                "finding_count": len(data["findings"]),
                "artifact_integrity": data["summary"]["artifact_integrity"],
            },
        )
        return data

    _execute(
        command_name="diagnose",
        bundle_path=bundle,
        json_output=json_output,
        operation=operation,
    )


@app.command("export", hidden=True)
def export_command(
    command_id: str | None = typer.Argument(
        None,
        help=(
            "Command ID whose evidence should be exported; defaults to the latest "
            "non-inspection command."
        ),
    ),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="ZIP destination; defaults to .taskbundle/exports/<command-id>.zip.",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing output file."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """Export a deterministic ZIP after verifying all recorded artifacts."""

    def operation(session: CommandSession) -> dict[str, Any]:
        target = _target_command(session, command_id)
        target_id = str(target["id"])
        destination = output or session.state_dir / "exports" / f"{target_id}.zip"
        data = export_command_evidence(
            state_dir=session.state_dir,
            destination=destination,
            command=target,
            events=session.database.get_events(target_id),
            test_results=session.database.get_test_results(target_id),
            artifact_records=session.database.get_artifacts(target_id),
            force=force,
        )
        session.event(
            "info",
            "export",
            "Verified command evidence exported.",
            {
                "target_command_id": target_id,
                "output": data["output"],
                "sha256": data["sha256"],
            },
        )
        return data

    _execute(
        command_name="export",
        bundle_path=bundle,
        json_output=json_output,
        operation=operation,
    )


@app.command("artifacts", hidden=True)
def artifacts_command(
    command_id: str | None = typer.Argument(
        None,
        help=(
            "Command ID whose artifacts should be verified; defaults to the latest "
            "non-inspection command."
        ),
    ),
    bundle: Path = typer.Option(Path("."), "--bundle", help="Task bundle directory."),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON."),
) -> None:
    """List and verify a command's artifact hashes and sizes."""

    def operation(session: CommandSession) -> dict[str, Any]:
        target = _target_command(session, command_id)
        target_id = str(target["id"])
        verification = verify_artifact_records(
            state_dir=session.state_dir,
            records=session.database.get_artifacts(target_id),
        )
        data = {"command": target, **verification}
        session.event(
            "info",
            "query",
            "Command artifacts verified.",
            {
                "target_command_id": target_id,
                "count": verification["count"],
                "valid": verification["valid"],
            },
        )
        if not verification["valid"]:
            raise InvalidTaskError(
                f"One or more artifacts failed integrity verification for {target_id}.",
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
            try:
                docker = DockerClient(runner, executable=docker_executable)
                versions = docker.versions()
                detail = f"client {versions.client}; server {versions.server}"
                if docker.readiness.auto_started:
                    detail += f"; started {docker.readiness.provider_label} automatically"
                    if docker.readiness.profile is not None:
                        detail += f" (profile {docker.readiness.profile})"
                checks.append(
                    {
                        "name": "docker-daemon",
                        "ok": True,
                        "detail": detail,
                    }
                )
            except TaskBundleError as error:
                detail = error.message
                if error.hint:
                    detail += f" {error.hint}"
                checks.append({"name": "docker-daemon", "ok": False, "detail": detail})

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
