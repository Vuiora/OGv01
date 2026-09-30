"""Bounded repository snapshots and tools that operate only inside a workspace."""
from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import signal
import stat
import subprocess
import tempfile
from typing import Any


EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", ".nuxt", ".cache", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode", "coverage", ".coverage",
})
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_BYTES = 250 * 1024 * 1024
MAX_COPY_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_LIST_RESULTS = 300
MAX_SEARCH_RESULTS = 100
MAX_READ_LINES = 200
MAX_TOOL_OUTPUT_CHARS = 30_000
MAX_CHECK_OUTPUT_BYTES = 100_000
_RESERVED_WINDOWS = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)


def _is_link(path: Path) -> bool:
    """Detect symlinks and Windows junctions, also on Python 3.11."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _secret_name(name: str) -> bool:
    name = name.lower()
    if name == ".env.example":
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {".npmrc", ".pypirc", ".netrc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}:
        return True
    if name in {"credentials", "secrets", "auth.json", "token.json", "tokens.json"}:
        return True
    if name.startswith("credentials.") or name in {
        "secrets.json", "secrets.yaml", "secrets.yml", "secrets.toml", "secrets.ini",
        "service-account.json", "service_account.json",
    }:
        return True
    return Path(name).suffix in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}


def _excluded(relative: str, patterns: tuple[str, ...] = ()) -> str | None:
    parts = relative.replace("\\", "/").split("/")
    if any(part.lower() in EXCLUDED_DIRS for part in parts):
        return "excluded directory"
    if any(_secret_name(part) for part in parts):
        return "sensitive filename"
    for pattern in patterns:
        pattern = pattern.replace("\\", "/").rstrip("/")
        if fnmatch.fnmatchcase(relative, pattern) or any(fnmatch.fnmatchcase(p, pattern) for p in parts):
            return "user exclusion"
        if relative.startswith(pattern + "/"):
            return "user exclusion"
    return None


def _valid_relative(value: str, allow_root: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Path must be a relative string without NUL bytes")
    if value in {"", "."} and allow_root:
        return ""
    windows = PureWindowsPath(value)
    if windows.drive or windows.root or value.startswith(("/", "\\")):
        raise ValueError("Absolute, drive-qualified, and UNC paths are forbidden")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if any(p in {"", ".", ".."} or p.endswith((" ", ".")) for p in parts):
        raise ValueError("Empty, dot, parent, and trailing-dot/space path segments are forbidden")
    if any(":" in p or _RESERVED_WINDOWS.match(p) for p in parts):
        raise ValueError("Alternate data streams and Windows reserved filenames are forbidden")
    if any(any(ord(c) < 32 for c in p) for p in parts):
        raise ValueError("Control characters are forbidden in paths")
    reason = _excluded(normalized)
    if reason:
        raise ValueError(f"Path is blocked: {reason}")
    return normalized


def _safe_path(root: Path, relative: str, allow_root: bool = False) -> Path:
    normalized = _valid_relative(relative, allow_root=allow_root)
    candidate = root
    for part in normalized.split("/") if normalized else []:
        candidate /= part
        if _is_link(candidate):
            raise ValueError("Symlinks and junctions are forbidden")
    # resolve catches filesystem-specific aliases as well as normal traversal.
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Path escapes the workspace")
    return candidate


def _walk_files(root: Path):
    """Walk without following any kind of reparse point."""
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        kept = []
        for name in sorted(dirnames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            reason = "symlink or junction" if _is_link(path) else _excluded(relative)
            if reason:
                yield path, relative, reason
            else:
                kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            reason = "symlink or junction" if _is_link(path) else _excluded(relative)
            yield path, relative, reason


def _git_files(source: Path) -> list[str] | None:
    # Do not accidentally use an unrelated parent repository.
    try:
        top = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            capture_output=True, check=False, timeout=15,
        )
        if top.returncode != 0 or Path(os.fsdecode(top.stdout.strip())).resolve() != source:
            return None
        result = subprocess.run(
            ["git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, check=False, timeout=30,
        )
        if result.returncode == 0:
            return sorted(set(os.fsdecode(p) for p in result.stdout.split(b"\x00") if p))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def create_snapshot(source: Path, destination: Path, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    """Copy a project to an empty directory after checking all size limits.

    Git ignore rules are honored when ``source`` is a Git repository root.
    Sensitive filenames, build dependencies and links are always excluded.
    Raises ValueError on limits rather than silently creating a partial project.
    """
    source_input = Path(source).expanduser()
    destination_input = Path(destination).expanduser()
    if _is_link(source_input) or _is_link(destination_input):
        raise ValueError("Snapshot roots cannot be symlinks or junctions")
    source = source_input.resolve()
    destination = destination_input.resolve()
    if not source.is_dir():
        raise ValueError(f"Source is not a directory: {source}")
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Snapshot destination must be outside the source directory")
    if source.is_relative_to(destination):
        raise ValueError("Snapshot destination must not contain the source directory")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("Snapshot destination must be empty or absent")
    git_files = _git_files(source)
    skipped: list[dict[str, str]] = []
    candidates: list[tuple[Path, str]] = []
    if git_files is not None:
        for relative in git_files:
            reason = _excluded(relative, exclude)
            if reason:
                skipped.append({"path": relative, "reason": reason})
                continue
            try:
                path = _safe_path(source, relative)
            except (ValueError, OSError) as exc:
                skipped.append({"path": relative, "reason": str(exc)})
                continue
            if not path.is_file():
                skipped.append({"path": relative, "reason": "missing or non-regular file"})
                continue
            candidates.append((path, relative))
    else:
        for path, relative, reason in _walk_files(source):
            reason = reason or _excluded(relative, exclude)
            if reason:
                skipped.append({"path": relative, "reason": reason})
                continue
            try:
                _valid_relative(relative)
            except ValueError as exc:
                skipped.append({"path": relative, "reason": str(exc)})
                continue
            candidates.append((path, relative))
    total_bytes = 0
    selected: list[tuple[Path, str, int]] = []
    for path, relative in candidates:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            skipped.append({"path": relative, "reason": "non-regular file"})
            continue
        if info.st_size > MAX_COPY_FILE_BYTES:
            raise ValueError(f"File exceeds snapshot limit ({MAX_COPY_FILE_BYTES} bytes): {relative}")
        total_bytes += info.st_size
        selected.append((path, relative, info.st_size))
        if total_bytes > MAX_SNAPSHOT_BYTES or len(selected) > MAX_SNAPSHOT_FILES:
            raise ValueError("Project exceeds snapshot size/file-count limit; use explicit exclusions")
    destination.mkdir(parents=True, exist_ok=True)
    for path, relative, expected_size in selected:
        # Recheck immediately before copying; never intentionally dereference links.
        _safe_path(source, relative)
        if path.stat(follow_symlinks=False).st_size != expected_size:
            raise ValueError(f"Source changed during snapshot: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)
        if _is_link(target) or target.stat().st_size != expected_size:
            raise ValueError(f"Source changed during snapshot: {relative}")
    return {
        "source": str(source), "destination": str(destination),
        "files": [relative for _, relative, _ in selected],
        "skipped": skipped, "total_bytes": total_bytes,
        "git_ignore_respected": git_files is not None,
    }


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError("Path is not a regular file")
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError(f"Text file exceeds the {MAX_TEXT_BYTES}-byte tool limit")
    data = path.read_bytes()
    if b"\x00" in data:
        raise ValueError("Binary files cannot be read as text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not UTF-8 text") from exc


class RepoTools:
    def __init__(self, root: Path, writable: bool = False):
        root = Path(root).expanduser()
        if _is_link(root):
            raise ValueError("Workspace root cannot be a symlink or junction")
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError("Workspace root must be an existing directory")
        self.writable = writable

    def specs(self) -> list[dict[str, Any]]:
        definitions = [
            ("list_files", "List permitted files beneath a relative directory, with bounded results.", {"path": {"type": "string"}}),
            ("read_file", "Read UTF-8 text with line numbers, up to 200 lines per request. Lines are 1-based and inclusive.", {
                "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"},
            }),
            ("search_text", "Find a literal, case-sensitive string in permitted UTF-8 text files.", {"query": {"type": "string"}}),
        ]
        if self.writable:
            definitions.append(("write_file", "Create or replace a UTF-8 text file inside this isolated workspace.", {
                "path": {"type": "string"}, "content": {"type": "string"},
            }))
        return [{
            "type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False},
        } for name, description, properties in definitions]

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            spec = next((spec for spec in self.specs() if spec["name"] == name), None)
            if spec is None:
                raise ValueError("Unknown or unavailable tool")
            if not isinstance(args, dict) or set(args) != set(spec["parameters"]["required"]):
                raise ValueError("Tool arguments do not match the declared schema")
            for key, definition in spec["parameters"]["properties"].items():
                expected = str if definition["type"] == "string" else int
                if not isinstance(args[key], expected) or isinstance(args[key], bool):
                    raise ValueError(f"Invalid type for {key}")
            return getattr(self, "_" + name)(**args)
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}

    def _list_files(self, path: str) -> dict[str, Any]:
        folder = _safe_path(self.root, path, allow_root=True)
        if not folder.is_dir():
            raise ValueError("Path must be a directory")
        files: list[str] = []
        length = 0
        truncated = False
        for _, relative, reason in _walk_files(folder):
            if reason:
                continue
            relative = (folder.relative_to(self.root) / relative).as_posix()
            if len(files) >= MAX_LIST_RESULTS or length + len(relative) > MAX_TOOL_OUTPUT_CHARS:
                truncated = True
                break
            files.append(relative)
            length += len(relative)
        return {"files": files, "truncated": truncated, "hint": "List a subdirectory to narrow results." if truncated else ""}

    def _read_file(self, path: str, start_line: int, end_line: int) -> dict[str, Any]:
        if start_line < 1 or end_line < start_line:
            raise ValueError("Require 1 <= start_line <= end_line")
        if end_line - start_line + 1 > MAX_READ_LINES:
            raise ValueError(f"Read at most {MAX_READ_LINES} lines per call")
        lines = _read_text(_safe_path(self.root, path)).splitlines()
        numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(lines[start_line - 1:end_line], start_line))
        truncated = len(numbered) > MAX_TOOL_OUTPUT_CHARS
        return {"path": path, "content": numbered[:MAX_TOOL_OUTPUT_CHARS],
                "total_lines": len(lines), "truncated": truncated}

    def _search_text(self, query: str) -> dict[str, Any]:
        if not query or len(query) > 1000:
            raise ValueError("Search query must contain 1 to 1000 characters")
        matches: list[dict[str, Any]] = []
        skipped = 0
        output_chars = 0
        scanned = 0
        for path, relative, reason in _walk_files(self.root):
            if reason:
                continue
            scanned += 1
            if scanned > MAX_SNAPSHOT_FILES:
                return {"matches": matches, "truncated": True, "skipped_files": skipped}
            try:
                text = _read_text(_safe_path(self.root, relative))
            except (ValueError, OSError):
                skipped += 1
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if query not in line:
                    continue
                snippet = line[:500]
                cost = len(relative) + len(snippet)
                if len(matches) >= MAX_SEARCH_RESULTS or output_chars + cost > MAX_TOOL_OUTPUT_CHARS:
                    return {"matches": matches, "truncated": True, "skipped_files": skipped}
                matches.append({"path": relative, "line": line_number, "text": snippet})
                output_chars += cost
        return {"matches": matches, "truncated": False, "skipped_files": skipped}

    def _write_file(self, path: str, content: str) -> dict[str, Any]:
        if not self.writable:
            raise ValueError("Workspace is read-only")
        if "\x00" in content or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError("Content must be bounded text without NUL bytes")
        target = _safe_path(self.root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(self.root, path)
        # Replace the directory entry instead of following an existing hard link.
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent, delete=False) as handle:
                temporary_name = handle.name
                handle.write(content)
            os.replace(temporary_name, target)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}


def _diff_files(root: Path) -> dict[str, Path]:
    return {relative: path for path, relative, reason in _walk_files(root) if not reason and path.is_file()}


def diff_workspace(before: Path, after: Path) -> str:
    """Return a unified UTF-8 diff, including new/deleted files and binary notices."""
    before, after = Path(before).resolve(), Path(after).resolve()
    old_files, new_files = _diff_files(before), _diff_files(after)
    output: list[str] = []
    for relative in sorted(old_files.keys() | new_files.keys()):
        old = old_files[relative].read_bytes() if relative in old_files else b""
        new = new_files[relative].read_bytes() if relative in new_files else b""
        if old == new and (relative in old_files) == (relative in new_files):
            continue
        fromfile = f"a/{relative}" if relative in old_files else "/dev/null"
        tofile = f"b/{relative}" if relative in new_files else "/dev/null"
        try:
            if b"\x00" in old or b"\x00" in new:
                raise UnicodeDecodeError("utf-8", b"\x00", 0, 1, "binary")
            old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeDecodeError:
            output.append(f"Binary files {fromfile} and {tofile} differ\n")
            continue
        output.append(f"diff --git a/{relative} b/{relative}\n")
        if relative not in old_files:
            output.append("new file mode 100644\n")
        elif relative not in new_files:
            output.append("deleted file mode 100644\n")
        pieces = list(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=fromfile, tofile=tofile,
        ))
        for piece in pieces:
            output.append(piece)
            if not piece.endswith("\n"):
                output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def _check_environment() -> dict[str, str]:
    sensitive = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
    return {name: value for name, value in os.environ.items() if not sensitive.search(name)}


def _terminate_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # PID is generated by Popen, never supplied by the model or user.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def _bounded_output(handle) -> tuple[str, bool]:
    handle.seek(0)
    data = handle.read(MAX_CHECK_OUTPUT_BYTES + 1)
    truncated = len(data) > MAX_CHECK_OUTPUT_BYTES
    return data[:MAX_CHECK_OUTPUT_BYTES].decode("utf-8", errors="replace"), truncated


def run_checks(root: Path, commands: list[list[str]], timeout: int = 120) -> list[dict[str, Any]]:
    """Execute only caller-configured argv commands, with bounded captured output.

    A copied working directory is isolation from accidental model edits, not an
    OS sandbox. User-authorized commands can execute code and access the host.
    """
    root = Path(root).resolve()
    if not root.is_dir() or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("Checks require an existing root and positive integer timeout")
    if not isinstance(commands, list) or any(
        not isinstance(command, list) or not command
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        for command in commands
    ):
        raise ValueError("Commands must be a list of non-empty string argument lists")
    results = []
    for command in commands:
        result: dict[str, Any] = {"command": command, "exit_code": None, "stdout": "", "stderr": "", "timed_out": False}
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            options: dict[str, Any] = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            }
            try:
                process = subprocess.Popen(
                    command, cwd=root, env=_check_environment(), shell=False,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **options,
                )
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    result["timed_out"] = True
                    _terminate_tree(process)
                result["exit_code"] = process.returncode
                result["stdout"], out_truncated = _bounded_output(stdout)
                result["stderr"], err_truncated = _bounded_output(stderr)
                result["output_truncated"] = out_truncated or err_truncated
            except OSError as exc:
                result["stderr"] = str(exc)
            results.append(result)
    return results
