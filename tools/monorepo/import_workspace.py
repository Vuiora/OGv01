"""Import local projects with original Git ancestry; verify the published snapshot.

Only the destination is mutated. Existing source repositories and runtime data
remain in place. Source commit IDs are preserved through two-parent merge commits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime, timezone

HISTORIES = {
    "sdl": "F-20260919-StatisticalDiscoveryLearning",
    "mentor": "F-20260914-Utils/Mentor",
}
PROJECT_ROOTS = [
    "F-260831", "F-260903-PRT", "F-260905-PRT", "F-260907-VM",
    "F-260908-GPTSelf", "F-260908-GPTSelf-renew", "F-260909-anki",
    "F-20260914-Utils", "F-20260915-MathematicsModel", "Wiki",
]
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".vs",
    ".vs.cache-backup", "out", "build", "dist", "tmp", "data", "logs",
    "scratch", "models", ".n8n-test", ".build", ".build;C", ".cache",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".renew-runs", "runs",
    "demo-output", "credentials", "__MACOSX",
}
SKIP_FILES = {".env", "botpw.txt", "a.out", "res", "TESTORFILE", ".DS_Store", "Thumbs.db"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".bkp", ".zip", ".apkg", ".out", ".token", ".key", ".pem", ".mutation-backup"}
TOKEN_PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]
# Explicit dummy key used by SDL CredentialDisciplineTests to test redaction.
# The value was reviewed in context; actual local credentials are always checked.
TEST_TOKEN_HASHES = {"02ef3257a74ff498c2168f590d6f12c20fe1bd76ae4b337808f1ec3424d009f6"}


def run(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True)
    if result.returncode:
        # Git output can include remote configuration; never print file contents.
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def git(repo: Path, *args: str) -> str:
    return run(repo, *args).decode("utf-8", errors="replace").strip()


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_secret_values(source: Path) -> set[bytes]:
    secrets: set[bytes] = set()
    for rel in ["F-20260914-Utils", *["F-20260914-Utils/" + x for x in
            ["ConceptLayerConstructor", "ConceptRelationDraw", "ThePicWorkingFlow", "Mentor"]]]:
        env = source / rel / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8-sig").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    name, value = line.split("=", 1)
                    value = value.strip().strip("\"'")
                    if re.search("(?i)(key|secret|password|token)", name) and len(value) >= 12:
                        secrets.add(value.encode("utf-8"))
    wiki = source / "Wiki/wiki-deploy"
    bot = wiki / "botpw.txt"
    if bot.exists():
        secrets.add(bot.read_bytes().strip())
    php = wiki / "LocalSettings.php"
    if php.exists():
        for match in re.finditer(r'\$wg(?:DBpassword|SecretKey|UpgradeKey)\s*=\s*["\']([^"\']+)', php.read_text(encoding="utf-8")):
            secrets.add(match.group(1).encode("utf-8"))
    compose = wiki / "docker-compose.yml"
    if compose.exists():
        for match in re.finditer(r"MARIADB_(?:ROOT_)?PASSWORD:\s*([^\s]+)", compose.read_text(encoding="utf-8")):
            secrets.add(match.group(1).encode("utf-8"))
    readme = wiki / "README.md"
    if readme.exists():
        for line in readme.read_text(encoding="utf-8").splitlines():
            if "网页登录" in line:
                values = re.findall(r"`([^`]+)`", line)
                if len(values) >= 2:
                    secrets.add(values[-1].encode("utf-8"))
    return {x for x in secrets if x}


def scan(data: bytes, secrets: set[bytes]) -> bool:
    return any(digest(m.group()) not in TEST_TOKEN_HASHES
               for p in TOKEN_PATTERNS for m in p.finditer(data)) or any(s in data for s in secrets)


def scan_history(repo: Path, secrets: set[bytes]) -> int:
    objects = git(repo, "rev-list", "--objects", "--all").splitlines()
    ids = "\n".join(row.split(" ", 1)[0] for row in objects) + "\n"
    metadata = run(repo, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                   data=ids.encode("ascii")).decode("ascii").splitlines()
    blobs = []
    for row in metadata:
        oid, kind, size = row.split()
        if kind == "blob":
            if int(size) >= 100 * 1024 * 1024:
                raise RuntimeError(f"Historical blob exceeds GitHub size limit: {oid}")
            blobs.append((oid, int(size)))
    # Batch object reads with a bounded payload instead of launching Git per blob.
    start = 0
    while start < len(blobs):
        end, size = start, 0
        while end < len(blobs) and (end == start or size + blobs[end][1] <= 24 * 1024 * 1024):
            size += blobs[end][1]
            end += 1
        batch = blobs[start:end]
        payload = run(repo, "cat-file", "--batch", data=("\n".join(oid for oid, _ in batch) + "\n").encode("ascii"))
        cursor = 0
        for oid, size in batch:
            header_end = payload.index(b"\n", cursor)
            if payload[cursor:header_end] != f"{oid} blob {size}".encode("ascii"):
                raise RuntimeError("Unexpected Git object batch response")
            cursor = header_end + 1
            if scan(payload[cursor:cursor + size], secrets):
                raise RuntimeError(f"Historical credential match; import stopped: {oid}")
            cursor += size + 1
        start = end
    return len(blobs)


def excluded(path: Path, relative: Path, allow_tracked_data: bool = False) -> bool:
    return (path.is_symlink() or path.name in SKIP_FILES
            or (path.name.startswith(".env.") and path.name not in {".env.example", ".env.template"})
            or path.suffix.lower() in SKIP_SUFFIXES
            or ".sqlite" in path.name or path.name.endswith((".db", ".db-wal", ".db-shm"))
            or any((x in SKIP_DIRS and not (allow_tracked_data and x == "data"))
                   or x.endswith(".egg-info") for x in relative.parts))


def sanitize_source(relative: str, data: bytes, secrets: set[bytes]) -> tuple[bytes, bool]:
    if relative == "F-260908-GPTSelf-renew/renew.validation.config.json":
        config = json.loads(data.decode("utf-8-sig"))
        if config.get("api_key"):
            config["api_key"] = None
            config.setdefault("api_key_env", "OPENAI_API_KEY")
            return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), True
        return data, False
    if relative in {"F-260903-PRT/setup-codexzh-mac.sh", "F-260903-PRT/setup-codexzh.ps1"}:
        text = data.decode("utf-8-sig")
        for pattern in TOKEN_PATTERNS[:2]:
            text = pattern.sub(b"__API_KEY__", text.encode("utf-8")).decode("utf-8")
        if relative.endswith(".sh"):
            text = text.replace('API_KEY="__API_KEY__"', 'API_KEY="${CODEXZH_API_KEY:-__API_KEY__}"')
        return text.encode("utf-8"), True
    if not relative.startswith("Wiki/wiki-deploy/"):
        return data, False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data, False
    original = text
    if relative == "Wiki/wiki-deploy/docker-compose.yml":
        text = text.replace("\r\n", "\n")
        text = re.sub(r"(MARIADB_ROOT_PASSWORD:)\s*[^\n]+", r"\1 ${OGWIKI_DB_ROOT_PASSWORD:?Set OGWIKI_DB_ROOT_PASSWORD in .env}", text)
        text = re.sub(r"(MARIADB_PASSWORD:)\s*[^\n]+", r"\1 ${OGWIKI_DB_PASSWORD:?Set OGWIKI_DB_PASSWORD in .env}", text)
        text = text.replace("    depends_on:\n", "    environment:\n      OGWIKI_DB_PASSWORD: ${OGWIKI_DB_PASSWORD:?Set OGWIKI_DB_PASSWORD in .env}\n      OGWIKI_SECRET_KEY: ${OGWIKI_SECRET_KEY:?Set OGWIKI_SECRET_KEY in .env}\n      OGWIKI_UPGRADE_KEY: ${OGWIKI_UPGRADE_KEY:?Set OGWIKI_UPGRADE_KEY in .env}\n    depends_on:\n")
    if relative == "Wiki/wiki-deploy/LocalSettings.php":
        for variable, env in [("wgDBpassword", "OGWIKI_DB_PASSWORD"), ("wgSecretKey", "OGWIKI_SECRET_KEY"), ("wgUpgradeKey", "OGWIKI_UPGRADE_KEY")]:
            text = re.sub(r'\$' + variable + r'\s*=\s*["\'][^"\']*["\']\s*;', "$" + variable + " = getenv('" + env + "');", text)
    for secret in secrets:
        try:
            value = secret.decode("utf-8")
        except UnicodeDecodeError:
            continue
        text = text.replace(value, "CONFIGURE_LOCALLY")
    if text != original:
        return text.encode("utf-8"), True
    return data, False


def import_all(source: Path, target: Path, resume: bool = False) -> None:
    existing = (target / ".git").exists()
    if source == target or (existing and not resume):
        raise RuntimeError("Destination must be separate; use --resume for an interrupted import")
    secrets = local_secret_values(source)
    records = []
    for name, relative in HISTORIES.items():
        repo = source / relative
        if git(repo, "rev-parse", "--is-shallow-repository") != "false":
            raise RuntimeError("A source history is shallow")
        commits = git(repo, "rev-list", "--all").splitlines()
        records.append({"id": name, "path": relative, "head": git(repo, "rev-parse", "HEAD"),
                        "commits": commits, "refs": git(repo, "show-ref").splitlines(),
                        "status_before": git(repo, "-c", "core.quotepath=false", "status", "--porcelain"),
                        "scanned_blobs": scan_history(repo, secrets)})
    if not existing:
        git(target, "init", "--initial-branch=main")
        git(target, "config", "core.autocrlf", "false")
        git(target, "add", ".")
        git(target, "commit", "-m", "chore: initialize automated reasoning monorepo")
    for record in records:
        relative, name = record["path"], record["id"]
        if existing:
            if git(target, "rev-parse", "history/" + name + "/imported-head") != record["head"]:
                raise RuntimeError("Resume source HEAD differs from imported history")
            message = f"merge: preserve {name} original history under {relative}"
            rows = git(target, "log", "--first-parent", "--format=%H%x09%s").splitlines()
            record["merge_commit"] = next(row.split("\t")[0] for row in rows if row.endswith("\t" + message))
            continue
        git(target, "fetch", "--no-tags", str(source / relative),
            f"+refs/heads/*:refs/heads/history/{name}/*",
            f"+refs/tags/*:refs/tags/history/{name}/*")
        previous = git(target, "rev-parse", "HEAD")
        git(target, "read-tree", "--prefix=" + relative + "/", "-u", record["head"])
        tree = git(target, "write-tree")
        merge = git(target, "commit-tree", tree, "-p", previous, "-p", record["head"],
                    "-m", f"merge: preserve {name} original history under {relative}")
        git(target, "update-ref", "refs/heads/main", merge, previous)
        record["merge_commit"] = merge
        git(target, "tag", "history/" + name + "/imported-head", record["head"])
        print(f"Imported {name}: {len(record['commits'])} original commits", flush=True)
    files, skipped = [], []
    roots = [*PROJECT_ROOTS, HISTORIES["sdl"]]
    for relative_root in roots:
        root = source / relative_root
        for directory, dirs, names in os.walk(root, followlinks=False):
            directory = Path(directory)
            retained = []
            for name in sorted(dirs):
                path = directory / name
                relative = path.relative_to(source)
                if name in SKIP_DIRS or name.endswith(".egg-info") or path.is_symlink() or path.is_junction():
                    skipped.append({"path": relative.as_posix() + "/", "reason": "metadata/runtime/dependency/cache/link"})
                else:
                    retained.append(name)
            dirs[:] = retained
            for name in sorted(names):
                src = directory / name
                relative = src.relative_to(source)
                if excluded(src, relative):
                    skipped.append({"path": relative.as_posix(), "reason": "local secret/runtime/build/backup"})
                    continue
                data = src.read_bytes()
                if len(data) >= 100 * 1024 * 1024:
                    raise RuntimeError(f"File exceeds GitHub size limit: {relative.as_posix()}")
                output, sanitized = sanitize_source(relative.as_posix(), data, secrets)
                if scan(output, secrets):
                    raise RuntimeError(f"Credential match; source path only: {relative.as_posix()}")
                dst = target / relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(output)
                files.append({"path": relative.as_posix(), "bytes": len(output), "source_sha256": digest(data),
                              "imported_sha256": digest(output), "sanitized": sanitized})
    skipped += [{"path": x, "reason": "external reference or local service state; retained in original workspace"}
                for x in ["d2l/", "work/", ".renew-runs/", "Linux Kernel Development (3rd Edition) (Robert Love) (z-library.sk, 1lib.sk, z-lib.sk).pdf"]]
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "source": "original OGv01 workspace",
                "histories": records, "files": files, "excluded": skipped,
                "policy": "Preserve original commit IDs; preserve directory layout; import current safe source snapshot; keep runtime data local."}
    dump(target / "docs/migration-manifest.json", manifest)
    git(target, "add", ".")
    git(target, "commit", "-m", "import: consolidate current project sources and provenance")
    verify(target, source)
    print(json.dumps({"imported_files": len(files), "bytes": sum(x["bytes"] for x in files),
                      "sanitized_files": [x["path"] for x in files if x["sanitized"]],
                      "excluded_entries": len(skipped)}, ensure_ascii=False), flush=True)


def add_repository(source: Path, repo: Path, prefix: str, name: str) -> None:
    """Add a complete, clean source repository to an existing monorepo."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", prefix):
        raise ValueError("Prefix must be a single project directory name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError("History ID must use lowercase letters, digits and hyphens")
    if source == repo or (repo / prefix).exists():
        raise RuntimeError("Source must be separate and project prefix must not exist")
    if git(repo, "branch", "--show-current") != "main" or git(repo, "status", "--porcelain"):
        raise RuntimeError("Destination main must have a clean working tree")
    if git(source, "status", "--porcelain") or git(source, "rev-parse", "--is-shallow-repository") != "false":
        raise RuntimeError("Source must be clean and have a complete history")
    if git(source, "rev-list", "--all", "--not", "HEAD"):
        raise RuntimeError("Source has history outside HEAD; additional ancestry must be merged explicitly")
    manifest = json.loads((repo / "docs/migration-manifest.json").read_text(encoding="utf-8"))
    if any(item["id"] == name for item in manifest["histories"]):
        raise RuntimeError("History ID already exists")
    secrets = local_secret_values(source.parent)
    commits = git(source, "rev-list", "--all").splitlines()
    record = {"id": name, "path": prefix, "head": git(source, "rev-parse", "HEAD"),
              "commits": commits, "refs": git(source, "show-ref").splitlines(),
              "status_before": git(source, "-c", "core.quotepath=false", "status", "--porcelain"),
              "source_url": git(source, "remote", "get-url", "origin"),
              "scanned_blobs": scan_history(source, secrets)}
    files = []
    for relative in run(source, "ls-files", "-z").decode("utf-8").rstrip("\0").split("\0"):
        path = source / relative
        # Explicitly versioned sample data is part of the original project.
        if excluded(path, Path(relative), allow_tracked_data=True):
            raise RuntimeError("Source tracks an excluded local file: " + relative)
        data = path.read_bytes()
        canonical = run(source, "show", "HEAD:" + relative)
        if data != canonical:
            raise RuntimeError("Source checkout bytes differ from Git; normalize checkout first: " + relative)
        if len(data) >= 100 * 1024 * 1024 or scan(data, secrets):
            raise RuntimeError("Source file fails publication audit: " + relative)
        files.append({"path": prefix + "/" + relative, "bytes": len(data), "source_sha256": digest(data),
                      "imported_sha256": digest(data), "sanitized": False, "source_tracked": True})
    specs = [f"+refs/heads/*:refs/heads/history/{name}/*", f"+refs/tags/*:refs/tags/history/{name}/*"]
    local_branches = set(git(source, "for-each-ref", "--format=%(refname:strip=2)", "refs/heads").splitlines())
    for branch in git(source, "for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin").splitlines():
        if branch != "HEAD" and branch not in local_branches:
            specs.append(f"+refs/remotes/origin/{branch}:refs/heads/history/{name}/{branch}")
    git(repo, "fetch", "--no-tags", str(source), *specs)
    previous = git(repo, "rev-parse", "HEAD")
    git(repo, "read-tree", "--prefix=" + prefix + "/", "-u", record["head"])
    tree = git(repo, "write-tree")
    merge = git(repo, "commit-tree", tree, "-p", previous, "-p", record["head"],
                "-m", f"merge: preserve {name} original history under {prefix}")
    git(repo, "update-ref", "refs/heads/main", merge, previous)
    git(repo, "tag", "history/" + name + "/imported-head", record["head"])
    record["merge_commit"] = merge
    manifest["histories"].append(record)
    manifest["files"].extend(files)
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    dump(repo / "docs/migration-manifest.json", manifest)
    git(repo, "add", "docs/migration-manifest.json")
    git(repo, "commit", "-m", f"import: record {name} source history and file provenance")
    verify(repo, source.parent)
    print(json.dumps({"project": prefix, "original_commits": len(commits), "files": len(files),
                      "bytes": sum(item["bytes"] for item in files)}, ensure_ascii=False), flush=True)


def verify(repo: Path, source: Path | None = None) -> None:
    manifest = json.loads((repo / "docs/migration-manifest.json").read_text(encoding="utf-8"))
    errors = []
    tracked = set(run(repo, "ls-files", "-z").decode("utf-8").rstrip("\0").split("\0"))
    tracked_source_data = {item["path"] for item in manifest["files"]
                           if item.get("source_tracked") and "data" in Path(item["path"]).parts}
    for entry in manifest["files"]:
        path = repo / entry["path"]
        if not path.is_file() or digest(path.read_bytes()) != entry["imported_sha256"]:
            errors.append("file mismatch: " + entry["path"])
        if entry["path"] not in tracked:
            errors.append("file not tracked: " + entry["path"])
    total = 0
    for history in manifest["histories"]:
        for commit in history["commits"]:
            check = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "main"], capture_output=True)
            if check.returncode:
                errors.append("missing ancestry: " + commit)
            total += 1
        if source:
            original = source / history["path"]
            if git(original, "rev-parse", "HEAD") != history["head"] or git(original, "-c", "core.quotepath=false", "status", "--porcelain") != history["status_before"]:
                errors.append("source repository state changed: " + history["id"])
    for relative in tracked:
        if excluded(repo / relative, Path(relative), allow_tracked_data=relative in tracked_source_data):
            # Previously committed research notes are retained, runtime files are not.
            errors.append("excluded file tracked: " + relative)
        if scan((repo / relative).read_bytes(), set()):
            errors.append("credential pattern: " + relative)
    git(repo, "fsck", "--full", "--no-dangling")
    if errors:
        raise RuntimeError("\n".join(errors))
    print(json.dumps({"verified_files": len(manifest["files"]), "original_commits_in_main": total,
                      "tracked_files": len(tracked), "git_fsck": "passed", "source_state_check": bool(source)}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--source", type=Path, required=True)
    imp.add_argument("--repo", type=Path, required=True)
    imp.add_argument("--resume", action="store_true")
    add = sub.add_parser("add", help="Preserve another repository under a new project directory")
    add.add_argument("--source", type=Path, required=True)
    add.add_argument("--repo", type=Path, required=True)
    add.add_argument("--prefix", required=True)
    add.add_argument("--id", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--repo", type=Path, default=Path.cwd())
    check.add_argument("--source", type=Path)
    args = parser.parse_args()
    if args.action == "import":
        import_all(args.source.resolve(), args.repo.resolve(), args.resume)
    elif args.action == "add":
        add_repository(args.source.resolve(), args.repo.resolve(), args.prefix, args.id)
    else:
        verify(args.repo.resolve(), args.source.resolve() if args.source else None)


if __name__ == "__main__":
    main()
