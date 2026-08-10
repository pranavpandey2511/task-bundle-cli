"""Patch inspection for the evaluator/solver trust boundary."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from taskbundle.config import Bundle
from taskbundle.errors import InvalidTaskError


class PatchFormatError(ValueError):
    """A patch is not an inspectable Git-style unified diff."""


_HUNK_HEADER = re.compile(
    r"^@@ -(?:0|[1-9][0-9]*)(?:,([0-9]+))? "
    r"\+(?:0|[1-9][0-9]*)(?:,([0-9]+))? @@(?:.*)$"
)
_ESCAPES = {
    "a": b"\a",
    "b": b"\b",
    "t": b"\t",
    "n": b"\n",
    "v": b"\v",
    "f": b"\f",
    "r": b"\r",
    '"': b'"',
    "\\": b"\\",
}


def _decode_quoted_path(value: str) -> str:
    """Decode Git's C-style quoted path representation."""

    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise PatchFormatError(f"invalid quoted path: {value}")
    decoded = bytearray()
    index = 1
    while index < len(value) - 1:
        character = value[index]
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue

        index += 1
        if index >= len(value) - 1:
            raise PatchFormatError(f"unterminated path escape: {value}")
        escaped = value[index]
        if escaped in _ESCAPES:
            decoded.extend(_ESCAPES[escaped])
            index += 1
            continue
        if escaped not in "01234567":
            raise PatchFormatError(f"unsupported path escape: \\{escaped}")
        end = index
        while end < min(index + 3, len(value) - 1) and value[end] in "01234567":
            end += 1
        octet = int(value[index:end], 8)
        if octet > 0xFF:
            raise PatchFormatError(f"path escape is not one byte: {value[index:end]}")
        decoded.append(octet)
        index = end
    return os.fsdecode(bytes(decoded))


def _quoted_token_end(value: str) -> int:
    escaped = False
    for index in range(1, len(value)):
        character = value[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            return index + 1
    raise PatchFormatError(f"unterminated quoted path: {value}")


def _take_path_token(value: str) -> tuple[str, str]:
    value = value.lstrip(" \t")
    if not value:
        raise PatchFormatError("missing patch path")
    if value.startswith('"'):
        end = _quoted_token_end(value)
        return _decode_quoted_path(value[:end]), value[end:]
    match = re.match(r"[^ \t]+", value)
    if match is None:
        raise PatchFormatError("missing patch path")
    return match.group(0), value[match.end() :]


def _parse_diff_header(line: str) -> tuple[str, str]:
    remainder = line.removeprefix("diff --git ")
    old_raw, remainder = _take_path_token(remainder)
    new_raw, remainder = _take_path_token(remainder)
    if remainder.strip():
        raise PatchFormatError(f"invalid diff header: {line}")
    return (
        _prefixed_repo_path(old_raw, prefix="a/", label="diff old path"),
        _prefixed_repo_path(new_raw, prefix="b/", label="diff new path"),
    )


def _parse_metadata_path(value: str, *, label: str) -> str:
    value = value.strip()
    if value.startswith('"'):
        path, remainder = _take_path_token(value)
        if remainder.strip():
            raise PatchFormatError(f"invalid {label}: {value}")
    else:
        path = value
    return _repo_path(path, label=label)


def _parse_file_header_path(value: str, *, prefix: str, label: str) -> str | None:
    value = value.strip()
    if value.startswith('"'):
        path, remainder = _take_path_token(value)
        if remainder.strip():
            raise PatchFormatError(f"invalid {label}: {value}")
    else:
        # A timestamp in a traditional unified diff is tab-separated. Git-generated
        # paths containing tabs are C-quoted, so splitting here is unambiguous.
        path = value.split("\t", maxsplit=1)[0]
    if path == "/dev/null":
        return None
    return _prefixed_repo_path(path, prefix=prefix, label=label)


def _prefixed_repo_path(value: str, *, prefix: str, label: str) -> str:
    if not value.startswith(prefix):
        raise PatchFormatError(f"{label} lacks {prefix!r} prefix: {value}")
    return _repo_path(value[len(prefix) :], label=label)


def _repo_path(value: str, *, label: str) -> str:
    path = PurePosixPath(value)
    raw_parts = value.split("/")
    if (
        not value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise PatchFormatError(f"unsafe {label}: {value}")
    return value


@dataclass(slots=True)
class _DiffSection:
    old_path: str
    new_path: str
    line_number: int
    file_old: str | None = None
    file_new: str | None = None
    saw_file_old: bool = False
    saw_file_new: bool = False
    rename_from: str | None = None
    rename_to: str | None = None
    copy_from: str | None = None
    copy_to: str | None = None
    saw_hunk: bool = False
    saw_binary: bool = False
    saw_mode_change: bool = False
    binary_body: bool = False
    hunk_old_remaining: int = 0
    hunk_new_remaining: int = 0
    allow_no_newline_marker: bool = False
    paths: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.paths.update((self.old_path, self.new_path))

    @property
    def inside_hunk(self) -> bool:
        return self.hunk_old_remaining > 0 or self.hunk_new_remaining > 0

    def start_hunk(self, line: str, *, line_number: int) -> None:
        match = _HUNK_HEADER.fullmatch(line)
        if match is None:
            raise PatchFormatError(f"invalid hunk header at line {line_number}: {line}")
        if not self.saw_file_old or not self.saw_file_new:
            raise PatchFormatError(f"hunk appears before ---/+++ headers at line {line_number}")
        self.hunk_old_remaining = int(match.group(1) or "1")
        self.hunk_new_remaining = int(match.group(2) or "1")
        self.allow_no_newline_marker = False
        self.saw_hunk = True

    def consume_hunk_line(self, line: str, *, line_number: int) -> None:
        if not line:
            raise PatchFormatError(f"malformed hunk body at line {line_number}")
        prefix = line[0]
        if prefix == " ":
            self.hunk_old_remaining -= 1
            self.hunk_new_remaining -= 1
        elif prefix == "-":
            self.hunk_old_remaining -= 1
        elif prefix == "+":
            self.hunk_new_remaining -= 1
        else:
            raise PatchFormatError(f"malformed hunk body at line {line_number}")
        if self.hunk_old_remaining < 0 or self.hunk_new_remaining < 0:
            raise PatchFormatError(f"hunk line counts underflow at line {line_number}")
        self.allow_no_newline_marker = True

    def finish(self) -> set[str]:
        if self.inside_hunk:
            raise PatchFormatError(f"incomplete hunk in diff beginning at line {self.line_number}")
        if self.saw_file_old != self.saw_file_new:
            raise PatchFormatError(
                f"unpaired ---/+++ headers in diff beginning at line {self.line_number}"
            )
        if self.saw_file_old:
            if self.file_old is None and self.file_new is None:
                raise PatchFormatError(
                    f"both file headers use /dev/null at line {self.line_number}"
                )
            if self.file_old is not None and self.file_old != self.old_path:
                raise PatchFormatError(
                    f"--- path does not match diff --git path at line {self.line_number}"
                )
            if self.file_new is not None and self.file_new != self.new_path:
                raise PatchFormatError(
                    f"+++ path does not match diff --git path at line {self.line_number}"
                )

        rename_present = self.rename_from is not None or self.rename_to is not None
        copy_present = self.copy_from is not None or self.copy_to is not None
        if rename_present and (self.rename_from is None or self.rename_to is None):
            raise PatchFormatError(f"unpaired rename metadata at line {self.line_number}")
        if copy_present and (self.copy_from is None or self.copy_to is None):
            raise PatchFormatError(f"unpaired copy metadata at line {self.line_number}")
        if rename_present and copy_present:
            raise PatchFormatError(
                f"diff mixes rename and copy metadata at line {self.line_number}"
            )
        if rename_present and (
            self.rename_from != self.old_path or self.rename_to != self.new_path
        ):
            raise PatchFormatError(
                f"rename metadata does not match diff --git paths at line {self.line_number}"
            )
        if copy_present and (self.copy_from != self.old_path or self.copy_to != self.new_path):
            raise PatchFormatError(
                f"copy metadata does not match diff --git paths at line {self.line_number}"
            )
        if self.old_path != self.new_path and not rename_present and not copy_present:
            raise PatchFormatError(
                f"changed diff paths lack rename/copy metadata at line {self.line_number}"
            )
        if not (
            self.saw_hunk
            or self.saw_binary
            or self.saw_mode_change
            or rename_present
            or copy_present
        ):
            raise PatchFormatError(
                f"diff beginning at line {self.line_number} contains no applicable change"
            )
        return self.paths


def changed_paths_from_patch(content: str) -> set[str]:
    """Return every path a Git patch can affect, rejecting ambiguous input."""

    if not content.strip():
        return set()

    paths: set[str] = set()
    section: _DiffSection | None = None
    for line_number, line in enumerate(content.splitlines(), start=1):
        if section is not None and line == "\\ No newline at end of file":
            if not section.allow_no_newline_marker:
                raise PatchFormatError(f"misplaced no-newline marker at line {line_number}")
            section.allow_no_newline_marker = False
            continue
        if section is not None and section.inside_hunk:
            section.consume_hunk_line(line, line_number=line_number)
            continue
        if line.startswith("diff --git "):
            if section is not None:
                paths.update(section.finish())
            old_path, new_path = _parse_diff_header(line)
            section = _DiffSection(old_path, new_path, line_number)
            continue
        if section is None:
            if not line.strip():
                continue
            raise PatchFormatError(
                f"patch content appears outside a diff --git section at line {line_number}"
            )
        if section.binary_body:
            if line.startswith(
                ("--- ", "+++ ", "rename from ", "rename to ", "copy from ", "copy to ")
            ):
                raise PatchFormatError(
                    f"patch metadata appears inside a binary body at line {line_number}"
                )
            continue
        if line.startswith("--- "):
            if section.saw_file_old:
                raise PatchFormatError(f"duplicate --- header at line {line_number}")
            section.file_old = _parse_file_header_path(line[4:], prefix="a/", label="--- path")
            section.saw_file_old = True
            continue
        if line.startswith("+++ "):
            if not section.saw_file_old or section.saw_file_new:
                raise PatchFormatError(f"misplaced +++ header at line {line_number}")
            section.file_new = _parse_file_header_path(line[4:], prefix="b/", label="+++ path")
            section.saw_file_new = True
            continue
        if line.startswith("rename from "):
            if section.rename_from is not None:
                raise PatchFormatError(f"duplicate rename from at line {line_number}")
            section.rename_from = _parse_metadata_path(
                line.removeprefix("rename from "), label="rename from path"
            )
            section.paths.add(section.rename_from)
            continue
        if line.startswith("rename to "):
            if section.rename_to is not None:
                raise PatchFormatError(f"duplicate rename to at line {line_number}")
            section.rename_to = _parse_metadata_path(
                line.removeprefix("rename to "), label="rename to path"
            )
            section.paths.add(section.rename_to)
            continue
        if line.startswith("copy from "):
            if section.copy_from is not None:
                raise PatchFormatError(f"duplicate copy from at line {line_number}")
            section.copy_from = _parse_metadata_path(
                line.removeprefix("copy from "), label="copy from path"
            )
            section.paths.add(section.copy_from)
            continue
        if line.startswith("copy to "):
            if section.copy_to is not None:
                raise PatchFormatError(f"duplicate copy to at line {line_number}")
            section.copy_to = _parse_metadata_path(
                line.removeprefix("copy to "), label="copy to path"
            )
            section.paths.add(section.copy_to)
            continue
        if line.startswith("@@"):
            section.start_hunk(line, line_number=line_number)
            continue
        if line == "GIT binary patch" or line.startswith("Binary files "):
            section.saw_binary = True
            section.binary_body = True
            continue
        if line.startswith(("old mode ", "new mode ", "new file mode ", "deleted file mode ")):
            section.saw_mode_change = True
            continue
        if line.startswith(("index ", "similarity index ", "dissimilarity index ")):
            continue
        if not line.strip():
            continue
        raise PatchFormatError(f"unexpected patch metadata at line {line_number}: {line}")

    assert section is not None
    paths.update(section.finish())
    return paths


def validate_patch_contract(
    *,
    bundle: Bundle,
    gold_patch: str,
    test_patch: str,
    solver_view_patch: str,
) -> dict[str, list[str]]:
    """Keep evaluator material isolated and the gold patch inside candidate policy."""

    try:
        gold_paths = changed_paths_from_patch(gold_patch)
        test_paths = changed_paths_from_patch(test_patch)
        solver_view_paths = changed_paths_from_patch(solver_view_patch)
    except PatchFormatError as error:
        raise InvalidTaskError(
            "A trusted bundle patch is not a valid Git-style unified diff.",
            details={"reason": str(error)},
        ) from error

    protected_paths = bundle.manifest.tests.evaluator_owned_paths
    evaluator_paths = test_paths | solver_view_paths
    unexpected = evaluator_paths - protected_paths
    missing = protected_paths - evaluator_paths
    gold_overlap = gold_paths & protected_paths
    unauthorized_gold = {path for path in gold_paths if not bundle.manifest.candidate.allows(path)}
    if unexpected or missing or gold_overlap or unauthorized_gold:
        raise InvalidTaskError(
            "Bundle patches do not preserve the evaluator-test trust boundary.",
            hint=(
                "Limit tests/hidden.patch and tests/solver-view.patch to declared test paths, "
                "and keep gold.patch out of those paths."
            ),
            details={
                "protected_paths": sorted(protected_paths),
                "unexpected_evaluator_paths": sorted(unexpected),
                "uncovered_protected_paths": sorted(missing),
                "gold_protected_overlap": sorted(gold_overlap),
                "gold_outside_allowed_paths": sorted(unauthorized_gold),
            },
        )
    return {
        "allowed_candidate_paths": sorted(bundle.manifest.candidate.allowed_patch_paths),
        "protected_paths": sorted(protected_paths),
        "gold_paths": sorted(gold_paths),
        "test_paths": sorted(test_paths),
        "solver_view_paths": sorted(solver_view_paths),
    }
