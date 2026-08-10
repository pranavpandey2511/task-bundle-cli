# ruff: noqa: E501
"""Self-contained, reviewer-facing HTML reports derived from the command ledger."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from taskbundle.database import Database
from taskbundle.diagnostics import diagnose_command
from taskbundle.errors import ExitCode, InfrastructureError
from taskbundle.models import CommandReport

REPORTABLE_COMMANDS = frozenset({"new", "check", "init", "validate", "run"})
REPORT_INDEX_PATH = "reports/index.html"
LATEST_REPORT_PATH = "reports/latest.html"


def command_html_report_path(command_id: str) -> str:
    return f"commands/{command_id}/report.html"


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _json(value: object) -> str:
    return escape(json.dumps(value, indent=2, sort_keys=True, default=str))


def _duration(started_at: datetime, ended_at: datetime) -> str:
    seconds = max(0.0, (ended_at - started_at).total_seconds())
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {remainder:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m"


def _size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def _artifact_href(command_id: str, relative_path: str) -> str:
    own_prefix = f"commands/{command_id}/"
    if relative_path.startswith(own_prefix):
        return quote(relative_path.removeprefix(own_prefix), safe="/")
    return "../../" + quote(relative_path, safe="/")


def _read_problem(
    state_dir: Path,
    *,
    command_id: str | None = None,
    verified_paths: set[str] | None = None,
) -> str:
    if command_id is not None and verified_paths is not None:
        snapshot = f"commands/{command_id}/trusted-inputs/description.md"
        if snapshot in verified_paths:
            try:
                return (state_dir / snapshot).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                pass
    path = state_dir.parent / "description.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "No readable description.md was available when this report was generated."


def _final_command_record(report: CommandReport, exit_code: ExitCode) -> dict[str, Any]:
    return {
        "id": report.command_id,
        "command_name": report.command,
        "bundle_id": report.bundle_id,
        "arguments": [],
        "started_at": report.started_at.isoformat(),
        "ended_at": report.ended_at.isoformat(),
        "status": report.status.value,
        "exit_code": int(exit_code),
        "error_kind": report.error.kind if report.error else None,
        "error_message": report.error.message if report.error else None,
    }


def _result_payload(report: CommandReport) -> Mapping[str, Any]:
    if report.data:
        return report.data
    if report.error is not None:
        return report.error.details
    return {}


def _outcome(report: CommandReport) -> tuple[str, str, str]:
    payload = _result_payload(report)
    if report.status.value == "failed":
        if report.error is not None and report.error.kind == "unresolved":
            return (
                "Needs revision",
                "warning",
                "The task ran, but the candidate did not resolve it.",
            )
        return "Run failed", "danger", "The lifecycle stopped before a successful result."
    if report.command == "run":
        resolved = bool(payload.get("resolved"))
        if resolved:
            return "Resolved", "success", "The candidate matched every recorded expectation."
        return "Not resolved", "warning", "The run completed without a resolved candidate."
    if report.command == "validate" and payload.get("mode") == "static":
        return "Contract passed", "success", "The task bundle passed its static author checks."
    labels = {
        "new": (
            "Draft created",
            "A profile-aware task draft and authoring checklist were created.",
        ),
        "check": ("Contract passed", "The task bundle passed its static author checks."),
        "init": ("Environment ready", "Evaluator and solver environments were prepared."),
        "validate": ("Task validated", "The baseline and golden expectations were confirmed."),
    }
    label, detail = labels.get(report.command, ("Succeeded", "The command completed successfully."))
    return label, "success", detail


def _test_groups(
    test_results: Sequence[Mapping[str, Any]],
) -> list[tuple[tuple[str, str, str], list[Mapping[str, Any]]]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for result in test_results:
        key = (str(result["phase"]), str(result["suite"]), str(result["test_id"]))
        grouped[key].append(result)
    return sorted(grouped.items())


def _test_summary(test_results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    matched = sum(result["observed"] == result["expected"] for result in test_results)
    return {
        "attempts": len(test_results),
        "matched": matched,
        "unexpected": len(test_results) - matched,
        "timeouts": sum(result["observed"] == "timeout" for result in test_results),
        "runner_errors": sum(result["observed"] == "error" for result in test_results),
    }


def _test_explanation(result: Mapping[str, Any]) -> str:
    expected = str(result["expected"])
    observed = str(result["observed"])
    if expected == observed:
        return "Matched the expected outcome."
    if observed == "timeout":
        return f"Timed out before producing the expected {expected} outcome."
    if observed == "error":
        return f"The test runner errored instead of producing the expected {expected} outcome."
    return f"Expected {expected}, but observed {observed}."


def _summary_cards(
    report: CommandReport,
    test_summary: Mapping[str, int],
    artifact_count: int,
) -> str:
    outcome, tone, detail = _outcome(report)
    cards = [
        ("Outcome", outcome, detail, tone),
        (
            "Expectations matched",
            f"{test_summary['matched']} / {test_summary['attempts']}",
            f"{test_summary['unexpected']} unexpected attempt(s)",
            "success" if test_summary["unexpected"] == 0 else "danger",
        ),
        (
            "Duration",
            _duration(report.started_at, report.ended_at),
            f"Started {report.started_at.astimezone().strftime('%d %b %Y, %H:%M:%S %Z')}",
            "neutral",
        ),
        (
            "Evidence",
            str(artifact_count),
            "content-hashed artifacts linked below",
            "neutral",
        ),
    ]
    return "".join(
        f"""
        <article class="metric metric--{tone}">
          <span class="metric__label">{_h(label)}</span>
          <strong>{_h(value)}</strong>
          <span>{_h(description)}</span>
        </article>
        """
        for label, value, description, tone in cards
    )


def _worked_section(test_results: Sequence[Mapping[str, Any]]) -> str:
    groups = _test_groups(test_results)
    matched: list[str] = []
    attention: list[str] = []
    for (phase, suite, test_id), attempts in groups:
        expected = str(attempts[0]["expected"])
        observations = ", ".join(str(attempt["observed"]) for attempt in attempts)
        text = (
            f"<strong>{_h(test_id)}</strong> in {_h(phase)} / {_h(suite)} "
            f"expected {_h(expected)} and observed {_h(observations)}."
        )
        target = matched if all(a["observed"] == a["expected"] for a in attempts) else attention
        target.append(f"<li>{text}</li>")

    if not groups:
        matched.append("<li>No test attempts were recorded before this lifecycle ended.</li>")
    if not attention:
        attention.append("<li>No unexpected test outcomes were recorded.</li>")
    return f"""
      <div class="split">
        <article class="panel callout callout--success">
          <div class="eyebrow">What worked</div>
          <h2>Expected behavior</h2>
          <ul>{"".join(matched)}</ul>
        </article>
        <article class="panel callout {"callout--danger" if len(attention) > 1 else ""}">
          <div class="eyebrow">What needs attention</div>
          <h2>Unexpected behavior</h2>
          <ul>{"".join(attention)}</ul>
        </article>
      </div>
    """


def _findings_section(
    command_id: str,
    report: CommandReport,
    diagnosis: Mapping[str, Any],
) -> str:
    findings = list(diagnosis["findings"])
    if report.error is not None:
        headline = f"{report.error.message}"
        if report.error.hint:
            headline += f" Recommended next step: {report.error.hint}"
        findings.insert(
            0,
            {"severity": "error", "category": report.error.kind, "message": headline},
        )
    finding_cards = []
    for finding in findings:
        links = "".join(
            f'<a class="evidence-link" href="{_artifact_href(command_id, str(path))}">'
            f"Open {_h(Path(str(path)).name)}</a>"
            for path in finding.get("artifacts", [])
        )
        finding_cards.append(
            f"""
            <article class="finding finding--{_h(finding["severity"])}">
              <div><span class="badge">{_h(finding["severity"])}</span>
              <span class="finding__category">{_h(finding["category"]).replace("_", " ")}</span></div>
              <p>{_h(finding["message"])}</p>
              <div class="finding__links">{links}</div>
            </article>
            """
        )
    actions = "".join(f"<li>{_h(action)}</li>" for action in diagnosis["next_actions"])
    if not actions:
        actions = "<li>No follow-up action was inferred from the recorded evidence.</li>"
    return f"""
      <section id="diagnosis" class="section">
        <div class="section-heading">
          <div><div class="eyebrow">Why this happened</div><h2>Diagnosis</h2></div>
          <span class="integrity">Evidence {_h(diagnosis["summary"]["artifact_integrity"])}</span>
        </div>
        <div class="findings">{"".join(finding_cards)}</div>
        <article class="panel next-actions"><h3>Recommended next actions</h3><ol>{actions}</ol></article>
      </section>
    """


def _tests_section(command_id: str, test_results: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for result in test_results:
        matched = result["observed"] == result["expected"]
        log = result.get("log_artifact")
        evidence = f'<a href="{_artifact_href(command_id, str(log))}">Open log</a>' if log else "—"
        rows.append(
            f"""
            <tr data-result="{"matched" if matched else "unexpected"}">
              <td><span class="result-dot result-dot--{"ok" if matched else "bad"}"></span>
                  {"Matched" if matched else "Unexpected"}</td>
              <td>{_h(result["phase"])}</td><td>{_h(result["suite"])}</td>
              <td><strong>{_h(result["test_id"])}</strong><small>{_h(_test_explanation(result))}</small></td>
              <td>{_h(result["attempt"])}</td><td>{_h(result["expected"])}</td>
              <td>{_h(result["observed"])}</td>
              <td>{float(result["duration_seconds"]):.2f}s</td>
              <td>{"—" if result["exit_code"] is None else _h(result["exit_code"])}</td>
              <td>{evidence}</td>
            </tr>
            """
        )
    empty = "" if rows else '<div class="empty">No test attempts were recorded.</div>'
    table = (
        ""
        if not rows
        else f"""
      <div class="table-wrap"><table>
        <thead><tr><th>Result</th><th>Phase</th><th>Suite</th><th>Test and reason</th>
        <th>Try</th><th>Expected</th><th>Observed</th><th>Time</th><th>Exit</th><th>Evidence</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table></div>
    """
    )
    return f"""
      <section id="tests" class="section">
        <div class="section-heading"><div><div class="eyebrow">Attempt by attempt</div>
          <h2>Test results</h2></div>
          <div class="filters" role="group" aria-label="Filter test results">
            <button class="filter is-active" data-filter="all" aria-pressed="true">All</button>
            <button class="filter" data-filter="matched" aria-pressed="false">Matched</button>
            <button class="filter" data-filter="unexpected" aria-pressed="false">Unexpected</button>
          </div>
        </div>{empty}{table}
      </section>
    """


def _verified_paths(diagnosis: Mapping[str, Any]) -> set[str]:
    verification = diagnosis.get("artifact_verification")
    if not isinstance(verification, Mapping):
        return set()
    checks = verification.get("artifacts")
    if not isinstance(checks, Sequence):
        return set()
    return {
        str(check["relative_path"])
        for check in checks
        if isinstance(check, Mapping) and check.get("status") == "ok"
    }


def _read_verified_text(
    *,
    state_dir: Path,
    relative_path: str,
    verified_paths: set[str],
    limit: int,
    tail: bool = False,
) -> str | None:
    if relative_path not in verified_paths:
        return None
    root = state_dir.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(content) <= limit:
        return content
    marker = f"\n… excerpt truncated; open {Path(relative_path).name} for the complete evidence …\n"
    return marker + content[-limit:] if tail else content[:limit] + marker


def _failure_evidence(
    *,
    state_dir: Path,
    command_id: str,
    test_results: Sequence[Mapping[str, Any]],
    verified_paths: set[str],
) -> str:
    excerpts: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    unexpected_count = sum(result["observed"] != result["expected"] for result in test_results)
    for result in test_results:
        if result["observed"] == result["expected"]:
            continue
        key = (str(result["phase"]), str(result["suite"]), str(result["test_id"]))
        if key in seen or len(excerpts) >= 12:
            continue
        seen.add(key)
        log = result.get("log_artifact")
        if not isinstance(log, str):
            continue
        content = _read_verified_text(
            state_dir=state_dir,
            relative_path=log,
            verified_paths=verified_paths,
            limit=6_000,
            tail=True,
        )
        if content is None:
            continue
        excerpts.append(
            f"""
            <details open class="evidence-excerpt">
              <summary>{_h(result["phase"])} / {_h(result["suite"])} / {_h(result["test_id"])}
                · attempt {_h(result["attempt"])} · observed {_h(result["observed"])}</summary>
              <div class="excerpt-actions"><span>{_h(_test_explanation(result))}</span>
                <a href="{_artifact_href(command_id, log)}">Open complete log</a></div>
              <pre class="log-preview">{_h(content)}</pre>
            </details>
            """
        )
    if not excerpts:
        return ""
    omitted = max(0, unexpected_count - len(excerpts))
    note = (
        f'<p class="section-note">{omitted} additional unexpected attempt(s) remain linked in '
        "the test table.</p>"
        if omitted
        else ""
    )
    return f"""
      <section id="failure-evidence" class="section">
        <div class="section-heading"><div><div class="eyebrow">The actual failure output</div>
          <h2>Why tests failed</h2></div></div>
        <div class="evidence-excerpts">{"".join(excerpts)}</div>{note}
      </section>
    """


def _run_details(*, state_dir: Path, report: CommandReport, verified_paths: set[str]) -> str:
    payload = _result_payload(report)
    solver = payload.get("solver")
    if not isinstance(solver, Mapping):
        return ""
    patch_paths = solver.get("patch_paths")
    if isinstance(patch_paths, Sequence) and not isinstance(patch_paths, str):
        path_list = "".join(f"<li><code>{_h(path)}</code></li>" for path in patch_paths)
    else:
        path_list = "<li>No changed paths were recorded.</li>"
    patch_preview = ""
    patch_artifact = solver.get("patch_artifact")
    if isinstance(patch_artifact, str):
        content = _read_verified_text(
            state_dir=state_dir,
            relative_path=patch_artifact,
            verified_paths=verified_paths,
            limit=12_000,
        )
        if content is not None:
            patch_preview = f"""
              <div class="raw"><details><summary>Candidate diff preview</summary>
                <div class="excerpt-actions"><span>Escaped, read-only preview</span>
                  <a href="{_artifact_href(report.command_id, patch_artifact)}">Open complete patch</a></div>
                <pre class="diff-preview">{_h(content)}</pre></details></div>
            """
    return f"""
      <section id="changes" class="section">
        <div class="section-heading"><div><div class="eyebrow">Candidate output</div>
          <h2>What changed</h2></div></div>
        <div class="split">
          <article class="panel definition-list">
            <div><span>Solver</span><strong>{_h(solver.get("adapter", "unknown"))}</strong></div>
            <div><span>Patch size</span><strong>{_h(solver.get("patch_bytes", 0))} bytes</strong></div>
            <div><span>Exit code</span><strong>{_h(solver.get("exit_code", "not recorded"))}</strong></div>
            <div><span>Duration</span><strong>{_h(solver.get("duration_seconds", "not recorded"))}</strong></div>
          </article>
          <article class="panel"><h3>Changed paths</h3><ul class="code-list">{path_list}</ul></article>
        </div>
        {patch_preview}
      </section>
    """


def _run_context(report: CommandReport) -> str:
    payload = _result_payload(report)
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping):
        return ""
    repository = provenance.get("repository")
    image = provenance.get("image")
    solver_image = provenance.get("solver_image")
    runtime = provenance.get("runtime")
    repository = repository if isinstance(repository, Mapping) else {}
    image = image if isinstance(image, Mapping) else {}
    solver_image = solver_image if isinstance(solver_image, Mapping) else {}
    runtime = runtime if isinstance(runtime, Mapping) else {}
    runtime_summary = (
        f"{runtime.get('cpus', '—')} CPU · {runtime.get('memory', '—')} memory · "
        f"{runtime.get('pids', '—')} PIDs · network "
        f"{'enabled' if runtime.get('solver_network') else 'disabled'}"
    )
    items = [
        ("Repository commit", repository.get("commit", "not recorded")),
        ("Execution fingerprint", provenance.get("execution_fingerprint", "not recorded")),
        ("Evaluator image", image.get("id", "not recorded")),
        ("Solver image", solver_image.get("id", "not recorded")),
        ("Repetitions", provenance.get("repetitions", payload.get("repetitions", "not recorded"))),
        ("Runtime policy", runtime_summary),
    ]
    cards = "".join(
        f'<div class="context-item"><span>{_h(label)}</span><code>{_h(value)}</code></div>'
        for label, value in items
    )
    artifact = provenance.get("artifact")
    link = (
        f'<a href="{_artifact_href(report.command_id, str(artifact))}">Open provenance.json</a>'
        if isinstance(artifact, str)
        else ""
    )
    return f"""
      <section id="context" class="section">
        <div class="section-heading"><div><div class="eyebrow">Can this result be traced?</div>
          <h2>Reproducibility context</h2></div>{link}</div>
        <div class="context-grid">{cards}</div>
      </section>
    """


def _timeline(events: Sequence[Mapping[str, Any]]) -> str:
    items = "".join(
        f"""
        <li class="timeline__item timeline__item--{_h(event["level"])}">
          <div class="timeline__meta"><time>{_h(event["occurred_at"])}</time>
          <span>{_h(event["phase"])}</span></div>
          <p>{_h(event["message"])}</p>
        </li>
        """
        for event in events
    )
    if not items:
        items = '<li class="empty">No lifecycle events were recorded.</li>'
    return f"""
      <section id="timeline" class="section">
        <div class="section-heading"><div><div class="eyebrow">Factual milestones</div>
          <h2>Timeline</h2></div></div>
        <ol class="timeline">{items}</ol>
      </section>
    """


def _artifacts(command_id: str, records: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(
        f"""
        <tr>
          <td><span class="artifact-kind">{_h(record["kind"])}</span></td>
          <td><a href="{_artifact_href(command_id, str(record["relative_path"]))}">
              {_h(str(record["relative_path"]).removeprefix(f"commands/{command_id}/"))}</a></td>
          <td>{_size(int(record["size_bytes"]))}</td>
          <td><code class="hash">{_h(str(record["sha256"])[:12])}…</code></td>
        </tr>
        """
        for record in records
    )
    if not rows:
        rows = '<tr><td colspan="4">No artifacts were recorded.</td></tr>'
    return f"""
      <section id="artifacts" class="section">
        <div class="section-heading"><div><div class="eyebrow">Open the evidence</div>
          <h2>Artifacts</h2></div>
          <label class="search"><span class="sr-only">Filter artifacts</span>
            <input id="artifact-search" type="search" placeholder="Filter files…" autocomplete="off"></label>
        </div>
        <div class="table-wrap"><table id="artifact-table">
          <thead><tr><th>Kind</th><th>File</th><th>Size</th><th>SHA-256</th></tr></thead>
          <tbody>{rows}</tbody>
        </table></div>
      </section>
    """


def render_command_report_html(
    *,
    state_dir: Path,
    report: CommandReport,
    exit_code: ExitCode,
    events: Sequence[Mapping[str, Any]],
    test_results: Sequence[Mapping[str, Any]],
    artifact_records: Sequence[Mapping[str, Any]],
) -> str:
    """Render one immutable review report without executing or inferring new task work."""

    diagnosis = diagnose_command(
        state_dir=state_dir,
        command=_final_command_record(report, exit_code),
        events=events,
        test_results=test_results,
        artifact_records=artifact_records,
    )
    verified_paths = _verified_paths(diagnosis)
    summary = _test_summary(test_results)
    outcome, tone, detail = _outcome(report)
    task_name = report.bundle_id or state_dir.parent.name
    problem = _read_problem(
        state_dir,
        command_id=report.command_id,
        verified_paths=verified_paths,
    )
    generated = report.ended_at.astimezone().strftime("%d %b %Y, %H:%M %Z")
    error_banner = ""
    if report.error is not None:
        hint = f"<p><strong>Next:</strong> {_h(report.error.hint)}</p>" if report.error.hint else ""
        error_banner = f"""
          <aside class="error-banner"><div><span class="badge">{_h(report.error.kind)}</span>
          <h2>{_h(report.error.message)}</h2>{hint}</div><a href="#diagnosis">See diagnosis ↓</a></aside>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{_h(task_name)} · {_h(outcome)} · Task Bundle report</title>
  <style>
    :root {{ --ink:#17233c; --muted:#68738a; --line:#dde3ec; --paper:#fff; --canvas:#f4f2ed;
      --navy:#14213d; --blue:#3457d5; --green:#087f5b; --green-soft:#e9f7f1;
      --amber:#9a6700; --amber-soft:#fff6d8; --red:#b42318; --red-soft:#fff0ee;
      --shadow:0 20px 60px rgba(22,35,60,.09); --radius:18px; }}
    * {{ box-sizing:border-box; }} html {{ scroll-behavior:smooth; }}
    body {{ margin:0; color:var(--ink); background:var(--canvas); font:15px/1.55 Inter,ui-sans-serif,
      -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    a {{ color:var(--blue); text-underline-offset:3px; }} a:hover {{ text-decoration-thickness:2px; }}
    button,input {{ font:inherit; }} code,pre,.hash {{ font-family:"SFMono-Regular",Consolas,monospace; }}
    .shell {{ width:min(1240px,calc(100% - 40px)); margin:0 auto; }}
    .topbar {{ position:sticky; top:0; z-index:5; color:#fff; background:rgba(20,33,61,.96);
      backdrop-filter:blur(14px); border-bottom:1px solid rgba(255,255,255,.12); }}
    .topbar .shell {{ min-height:58px; display:flex; align-items:center; justify-content:space-between; gap:24px; }}
    .brand {{ display:flex; align-items:center; gap:10px; font-weight:800; letter-spacing:-.01em; }}
    .brand__mark {{ width:28px; height:28px; display:grid; place-items:center; border-radius:9px;
      background:#fff; color:var(--navy); }}
    .topbar nav {{ display:flex; gap:18px; overflow:auto; white-space:nowrap; }}
    .topbar nav a {{ color:#dbe4ff; text-decoration:none; font-size:13px; }}
    .hero {{ padding:64px 0 32px; background:linear-gradient(145deg,#14213d 0%,#25365d 68%,#3457d5 150%);
      color:#fff; }}
    .hero-grid {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:40px; align-items:end; }}
    .kicker,.eyebrow {{ font-size:11px; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }}
    .kicker {{ color:#aec0ff; }} .hero h1 {{ margin:10px 0 8px; font-size:clamp(34px,6vw,68px);
      line-height:.98; letter-spacing:-.055em; }}
    .hero__detail {{ max-width:720px; color:#dbe4ff; font-size:17px; }}
    .status {{ min-width:220px; padding:18px; border:1px solid rgba(255,255,255,.2); border-radius:16px;
      background:rgba(255,255,255,.08); }} .status span {{ display:block; color:#cbd6f7; font-size:12px; }}
    .status strong {{ display:block; margin:4px 0; font-size:24px; }}
    .status--success strong {{ color:#79e0bb; }} .status--warning strong {{ color:#ffd66b; }}
    .status--danger strong {{ color:#ff9b92; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:8px 18px; margin-top:22px; color:#cbd6f7; font-size:13px; }}
    .meta code {{ color:#fff; }} main {{ padding:30px 0 72px; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:26px; }}
    .metric,.panel,.section {{ background:var(--paper); border:1px solid var(--line); box-shadow:var(--shadow); }}
    .metric {{ min-height:148px; padding:20px; border-radius:var(--radius); border-top:4px solid #bcc5d4; }}
    .metric--success {{ border-top-color:var(--green); }} .metric--danger {{ border-top-color:var(--red); }}
    .metric--warning {{ border-top-color:#d49a00; }} .metric__label {{ display:block; color:var(--muted);
      font-weight:700; }} .metric strong {{ display:block; margin:13px 0 2px; font-size:29px; letter-spacing:-.03em; }}
    .metric > span:last-child {{ color:var(--muted); font-size:13px; }}
    .error-banner {{ display:flex; align-items:center; justify-content:space-between; gap:24px; padding:22px 24px;
      margin:0 0 26px; border:1px solid #f3b9b3; border-radius:var(--radius); background:var(--red-soft); }}
    .error-banner h2 {{ margin:7px 0 0; font-size:19px; }} .error-banner p {{ margin:7px 0 0; }}
    .split {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; margin-bottom:26px; }}
    .panel {{ padding:24px; border-radius:var(--radius); }} .panel h2,.panel h3 {{ margin:4px 0 12px; }}
    .panel ul,.panel ol {{ padding-left:20px; }} .panel li+li {{ margin-top:8px; }}
    .callout {{ box-shadow:none; }} .callout--success {{ border-left:5px solid var(--green); }}
    .callout--danger {{ border-left:5px solid var(--red); }} .eyebrow {{ color:var(--blue); }}
    .section {{ margin:26px 0; border-radius:22px; overflow:hidden; }}
    .section-heading {{ display:flex; align-items:end; justify-content:space-between; gap:20px; padding:24px 26px 18px; }}
    .section-heading h2 {{ margin:3px 0 0; font-size:25px; letter-spacing:-.025em; }}
    .integrity {{ padding:7px 10px; border-radius:999px; background:var(--green-soft); color:var(--green);
      font-size:12px; font-weight:800; text-transform:uppercase; }}
    .findings {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; padding:0 26px 20px; }}
    .finding {{ padding:17px; border:1px solid var(--line); border-left:4px solid #aab3c2; border-radius:12px; }}
    .finding--error {{ border-left-color:var(--red); background:#fffafa; }}
    .finding--warning {{ border-left-color:#d49a00; background:#fffdf6; }}
    .finding--info {{ border-left-color:var(--blue); }} .finding p {{ margin:8px 0 0; }}
    .finding__category {{ margin-left:7px; color:var(--muted); text-transform:capitalize; }}
    .finding__links {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }}
    .badge {{ display:inline-block; padding:3px 7px; border-radius:6px; background:var(--navy); color:#fff;
      font-size:10px; font-weight:800; letter-spacing:.06em; text-transform:uppercase; }}
    .next-actions {{ margin:0 26px 26px; background:#f7f9fd; box-shadow:none; }}
    .filters {{ display:flex; gap:7px; }} .filter {{ padding:7px 11px; border:1px solid var(--line);
      border-radius:999px; background:#fff; color:var(--ink); cursor:pointer; }}
    .filter.is-active {{ color:#fff; background:var(--navy); border-color:var(--navy); }}
    .table-wrap {{ overflow:auto; border-top:1px solid var(--line); }} table {{ width:100%; border-collapse:collapse;
      min-width:820px; }} th {{ padding:11px 14px; color:var(--muted); background:#f7f8fb; text-align:left;
      font-size:11px; letter-spacing:.06em; text-transform:uppercase; }}
    td {{ padding:14px; border-top:1px solid var(--line); vertical-align:top; }} td small {{ display:block;
      max-width:350px; margin-top:3px; color:var(--muted); }} tbody tr:hover {{ background:#fafbfe; }}
    .result-dot {{ display:inline-block; width:8px; height:8px; margin-right:7px; border-radius:50%; }}
    .result-dot--ok {{ background:var(--green); }} .result-dot--bad {{ background:var(--red); }}
    .definition-list>div {{ display:flex; justify-content:space-between; gap:20px; padding:10px 0;
      border-bottom:1px solid var(--line); }} .definition-list span {{ color:var(--muted); }}
    .code-list code {{ overflow-wrap:anywhere; }} .timeline {{ list-style:none; margin:0; padding:0 26px 26px; }}
    .evidence-excerpts {{ display:grid; gap:14px; padding:0 26px 26px; }} .evidence-excerpt {{ background:#fffafa; }}
    .excerpt-actions {{ display:flex; justify-content:space-between; gap:18px; padding:11px 16px;
      border-top:1px solid var(--line); color:var(--muted); font-size:13px; }}
    .log-preview,.diff-preview {{ max-height:430px; white-space:pre-wrap; overflow:auto; margin:0; padding:18px;
      border-top:1px solid var(--line); background:#101827; color:#e9eef8; font-size:12px; line-height:1.55; }}
    .section-note {{ margin:-10px 26px 24px; color:var(--muted); }}
    .context-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1px;
      padding:1px 26px 26px; background:var(--line); }} .context-item {{ min-width:0; padding:18px;
      background:var(--paper); }} .context-item span {{ display:block; margin-bottom:7px; color:var(--muted);
      font-size:12px; font-weight:800; text-transform:uppercase; }} .context-item code {{ display:block;
      overflow-wrap:anywhere; }}
    .timeline__item {{ position:relative; padding:0 0 20px 25px; border-left:2px solid var(--line); }}
    .timeline__item:last-child {{ padding-bottom:0; }} .timeline__item:before {{ content:""; position:absolute;
      left:-6px; top:5px; width:10px; height:10px; border-radius:50%; background:var(--blue); box-shadow:0 0 0 4px #e8edff; }}
    .timeline__item--error:before {{ background:var(--red); box-shadow:0 0 0 4px var(--red-soft); }}
    .timeline__meta {{ display:flex; gap:12px; color:var(--muted); font-size:12px; }}
    .timeline__meta span {{ color:var(--blue); font-weight:800; text-transform:uppercase; }}
    .timeline p {{ margin:4px 0 0; }} .artifact-kind {{ padding:4px 7px; border-radius:6px;
      background:#eef1f6; font-size:12px; }} .hash {{ color:var(--muted); }}
    .search input {{ width:230px; padding:9px 12px; border:1px solid var(--line); border-radius:10px; }}
    .problem {{ white-space:pre-wrap; max-height:520px; overflow:auto; margin:0; padding:24px 26px;
      border-top:1px solid var(--line); background:#fbfbf9; color:#29354e; }}
    .raw {{ margin:0 26px 26px; }} details {{ border:1px solid var(--line); border-radius:12px; background:#fafbfe; }}
    summary {{ padding:13px 16px; cursor:pointer; font-weight:700; }} details pre {{ overflow:auto; margin:0;
      padding:16px; border-top:1px solid var(--line); font-size:12px; }} .empty {{ padding:24px 26px; color:var(--muted); }}
    footer {{ padding:26px 0 50px; color:var(--muted); font-size:13px; }} .footer-row {{ display:flex;
      justify-content:space-between; gap:20px; border-top:1px solid #d6d4ce; padding-top:20px; }}
    .sr-only {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
      clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
    :focus-visible {{ outline:3px solid #7c99ff; outline-offset:3px; }}
    @media (max-width:900px) {{ .metrics {{ grid-template-columns:repeat(2,1fr); }}
      .hero-grid,.split {{ grid-template-columns:1fr; }} .status {{ min-width:0; }} .findings {{ grid-template-columns:1fr; }} }}
    @media (max-width:620px) {{ .shell {{ width:min(100% - 24px,1240px); }} .topbar nav {{ display:none; }}
      .hero {{ padding-top:38px; }} .metrics {{ grid-template-columns:1fr; }} .section-heading,.error-banner {{ align-items:flex-start;
      flex-direction:column; }} .filters {{ overflow:auto; max-width:100%; }} .search input {{ width:100%; }}
      .footer-row,.excerpt-actions {{ flex-direction:column; }} .context-grid {{ grid-template-columns:1fr; }} }}
    @media print {{ .topbar,.filters,.search {{ display:none; }} body {{ background:#fff; }} .section,.panel,.metric {{
      box-shadow:none; break-inside:avoid; }} .hero {{ padding:28px 0; }} }}
  </style>
</head>
<body>
  <header class="topbar"><div class="shell"><div class="brand"><span class="brand__mark">T</span>
    Task Bundle Review</div><nav><a href="#problem">Problem</a><a href="#diagnosis">Diagnosis</a>
    <a href="#tests">Tests</a><a href="#changes">Changes</a><a href="#context">Context</a>
    <a href="#timeline">Timeline</a><a href="#artifacts">Artifacts</a>
    <a href="../../reports/index.html">All versions</a></nav></div></header>
  <section class="hero"><div class="shell hero-grid"><div><div class="kicker">{_h(report.command)} report · {_h(task_name)}</div>
    <h1>{_h(outcome)}</h1><p class="hero__detail">{_h(detail)}</p>
    <div class="meta"><span>Command <code>{_h(report.command_id)}</code></span><span>Generated {_h(generated)}</span>
    <span>Version: {_h(report.command_id)}</span></div></div>
    <div class="status status--{tone}"><span>Final status</span><strong>{_h(report.status.value)}</strong>
      <span>Exit code {int(exit_code)}</span></div></div></section>
  <main class="shell">
    <section class="metrics">{_summary_cards(report, summary, len(artifact_records) + 2)}</section>
    {error_banner}
    {_worked_section(test_results)}
    <section id="problem" class="section"><div class="section-heading"><div><div class="eyebrow">The assignment</div>
      <h2>Problem statement</h2></div></div><pre class="problem">{_h(problem)}</pre></section>
    {_findings_section(report.command_id, report, diagnosis)}
    {_tests_section(report.command_id, test_results)}
    {_failure_evidence(state_dir=state_dir, command_id=report.command_id, test_results=test_results, verified_paths=verified_paths)}
    {_run_details(state_dir=state_dir, report=report, verified_paths=verified_paths)}
    {_run_context(report)}
    {_timeline(events)}
    {_artifacts(report.command_id, artifact_records)}
    <section class="section"><div class="section-heading"><div><div class="eyebrow">Machine-readable source</div>
      <h2>Raw command result</h2></div><a href="report.json">Open report.json</a></div>
      <div class="raw"><details><summary>Show structured JSON</summary><pre>{_json(report.model_dump(mode="json"))}</pre></details></div>
    </section>
  </main>
  <footer class="shell"><div class="footer-row"><span>Generated from the local SQLite ledger and content-hashed artifacts.</span>
    <a href="../../reports/index.html">← View every report for this task</a></div></footer>
  <script>
    document.querySelectorAll('.filter').forEach((button) => button.addEventListener('click', () => {{
      document.querySelectorAll('.filter').forEach((item) => {{ item.classList.remove('is-active'); item.setAttribute('aria-pressed','false'); }});
      button.classList.add('is-active'); button.setAttribute('aria-pressed','true');
      document.querySelectorAll('#tests tbody tr').forEach((row) => {{
        row.hidden = button.dataset.filter !== 'all' && row.dataset.result !== button.dataset.filter;
      }});
    }}));
    document.querySelector('#artifact-search')?.addEventListener('input', (event) => {{
      const query = event.target.value.toLowerCase();
      document.querySelectorAll('#artifact-table tbody tr').forEach((row) => {{
        row.hidden = !row.textContent.toLowerCase().includes(query);
      }});
    }});
  </script>
</body>
</html>
"""


def _read_command_report(state_dir: Path, command_id: str) -> CommandReport | None:
    path = state_dir / "commands" / command_id / "report.json"
    try:
        return CommandReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _index_summary(report: CommandReport | None, command: Mapping[str, Any]) -> str:
    if report is not None:
        _label, _tone, detail = _outcome(report)
        if report.error is not None:
            return report.error.message
        return detail
    error = command.get("error_message")
    return str(error or "Recorded lifecycle report.")


def _atomic_write(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise InfrastructureError(
            f"Could not publish HTML report navigation at {path}: {error}"
        ) from error


def publish_report_index(
    *, state_dir: Path, database: Database, current_report: CommandReport
) -> None:
    """Refresh mutable task-level navigation over immutable command reports."""

    commands = [
        command
        for command in database.list_commands(limit=500)
        if command["command_name"] in REPORTABLE_COMMANDS
        and any(
            artifact["kind"] == "html_report"
            for artifact in database.get_artifacts(str(command["id"]))
        )
    ]
    commands.sort(key=lambda command: (str(command["started_at"]), str(command["id"])))
    task_name = current_report.bundle_id or state_dir.parent.name
    problem = _read_problem(state_dir)
    cards = []
    for version, command in enumerate(commands, start=1):
        command_id = str(command["id"])
        stored_report = (
            current_report
            if command_id == current_report.command_id
            else _read_command_report(state_dir, command_id)
        )
        status = stored_report.status.value if stored_report else str(command["status"])
        tone = "success" if status == "succeeded" else "danger"
        started_at = str(command["started_at"])
        cards.append(
            f"""
            <article class="report-card report-card--{tone}">
              <div class="report-card__top"><span class="version">v{version:03d}</span>
              <span class="status">{_h(status)}</span></div>
              <div class="command">{_h(command["command_name"])}</div>
              <h2>{_h(_index_summary(stored_report, command))}</h2>
              <p>{_h(started_at)}</p><code>{_h(command_id)}</code>
              <a href="../commands/{quote(command_id)}/report.html">Open report <span>→</span></a>
            </article>
            """
        )
    cards.reverse()
    latest_run = next(
        (command for command in reversed(commands) if command["command_name"] == "run"),
        None,
    )
    latest = latest_run or (commands[-1] if commands else None)
    latest_label = "Open latest run" if latest_run is not None else "Open latest report"
    latest_link = (
        f'<a class="button" href="../commands/{quote(str(latest["id"]))}/report.html">'
        f"{latest_label} →</a>"
        if latest is not None
        else ""
    )
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_h(task_name)} · Task report history</title><style>
  :root {{ --ink:#17233c; --muted:#68738a; --line:#dde3ec; --paper:#fff; --canvas:#f4f2ed;
    --navy:#14213d; --blue:#3457d5; --green:#087f5b; --red:#b42318; --shadow:0 20px 60px rgba(22,35,60,.09); }}
  * {{ box-sizing:border-box; }} body {{ margin:0; color:var(--ink); background:var(--canvas);
    font:15px/1.5 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .shell {{ width:min(1120px,calc(100% - 40px)); margin:auto; }} header {{ color:#fff;
    background:linear-gradient(145deg,#14213d,#2d4070); padding:70px 0 82px; }}
  .eyebrow {{ color:#b9c8ff; font-size:11px; font-weight:800; letter-spacing:.14em; text-transform:uppercase; }}
  h1 {{ margin:10px 0; max-width:800px; font-size:clamp(38px,7vw,76px); line-height:.98; letter-spacing:-.055em; }}
  header p {{ max-width:720px; color:#dbe4ff; font-size:17px; }} .button {{ display:inline-block; margin-top:14px;
    padding:11px 15px; border-radius:10px; color:var(--navy); background:#fff; font-weight:800; text-decoration:none; }}
  main {{ padding:34px 0 70px; }} .intro {{ display:flex; align-items:end; justify-content:space-between; gap:20px;
    margin-bottom:22px; }} .intro h2 {{ margin:0; font-size:26px; }} .intro p {{ margin:4px 0 0; color:var(--muted); }}
  .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:17px; }} .report-card {{ position:relative;
    padding:24px; border:1px solid var(--line); border-top:4px solid var(--green); border-radius:18px;
    background:var(--paper); box-shadow:var(--shadow); }} .report-card--danger {{ border-top-color:var(--red); }}
  .report-card__top {{ display:flex; justify-content:space-between; }} .version,.status {{ padding:4px 8px;
    border-radius:6px; background:#eef1f6; font-size:11px; font-weight:800; text-transform:uppercase; }}
  .command {{ margin-top:22px; color:var(--blue); font-size:12px; font-weight:800; letter-spacing:.1em; text-transform:uppercase; }}
  .report-card h2 {{ min-height:52px; margin:6px 0; font-size:20px; line-height:1.3; }} .report-card p {{ color:var(--muted); }}
  .report-card code {{ display:block; overflow-wrap:anywhere; color:var(--muted); font-size:12px; }}
  .report-card>a {{ display:flex; justify-content:space-between; margin-top:20px; padding-top:15px; border-top:1px solid var(--line);
    color:var(--blue); font-weight:800; text-decoration:none; }} .problem {{ margin-top:32px; padding:24px; border:1px solid var(--line);
    border-radius:18px; background:#fff; }} .problem pre {{ white-space:pre-wrap; max-height:250px; overflow:auto; }}
  .empty {{ padding:40px; border:1px dashed #aeb6c4; border-radius:18px; color:var(--muted); text-align:center; }}
  footer {{ padding:28px 0; color:var(--muted); border-top:1px solid #d6d4ce; }}
  :focus-visible {{ outline:3px solid #7c99ff; outline-offset:3px; }}
  @media(max-width:720px) {{ .shell {{ width:min(100% - 24px,1120px); }} .grid {{ grid-template-columns:1fr; }}
    header {{ padding:46px 0 58px; }} .intro {{ align-items:flex-start; flex-direction:column; }} }}
</style></head><body><header><div class="shell"><div class="eyebrow">Task report history</div>
  <h1>{_h(task_name)}</h1><p>Every lifecycle review is immutable and versioned by its command ID. Open any report to see
  the problem, outcome, diagnosis, exact test attempts, factual timeline, changes, and linked evidence.</p>{latest_link}</div></header>
  <main class="shell"><div class="intro"><div><h2>{len(commands)} review version(s)</h2>
  <p>Newest first · stable dashboard at <code>.taskbundle/reports/index.html</code></p></div></div>
  <section class="grid">{"".join(cards) if cards else '<div class="empty">No reports have been generated yet.</div>'}</section>
  <section class="problem"><div class="eyebrow">Problem statement</div><pre>{_h(problem)}</pre></section></main>
  <footer><div class="shell">Generated locally from recorded command evidence. No web service is required.</div></footer>
</body></html>"""
    _atomic_write(state_dir / REPORT_INDEX_PATH, content)
    if latest is not None:
        target = f"../commands/{quote(str(latest['id']))}/report.html"
        redirect = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0; url={_h(target)}"><title>Open latest task report</title></head>
<body><p><a href="{_h(target)}">Open the latest task report</a></p></body></html>"""
        _atomic_write(state_dir / LATEST_REPORT_PATH, redirect)
