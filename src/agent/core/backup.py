"""
Backup and restore of AtlasOS's local state (data/ and chroma_db/).

Everything the agent knows — the memory/audit trail, incidents, deploys,
SLOs, the vault, semantic memory — lives in a handful of SQLite files and
folders on one disk. This module snapshots them into a single verified
archive and puts them back.

Design points:

- SQLite files are copied with SQLite's online backup API, never a raw file
  copy. A plain copy taken while the agent is writing can capture half a
  transaction and produce a corrupt backup nobody notices until restore.
  Every snapshot is then integrity-checked before it is allowed into an
  archive, so a corrupt backup is never written.
- Every archive carries a manifest with a SHA-256 per file. `verify` checks
  them without restoring anything.
- Secrets (Gmail tokens, .env) are excluded: an archive is plain, readable
  data and must not become a second copy of the keys.
- Restore snapshots the current state first, so a restore can be undone.
- Archives land in a local directory (ATLASOS_BACKUP_DIR, default backups/).
  An off-site copy (e.g. S3) is a later step that uploads the finished
  archive; nothing here needs to change for it.

Callers (CLI) are responsible for confirmation prompts and the audit trail.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 1

# Directories (relative to the project root) that hold persistent state.
SOURCES = ("data", "chroma_db")

# Matched against the path relative to the project root, POSIX-style.
EXCLUDE = (
    # Secrets — never copied into an archive.
    "data/gmail_token.json",
    "data/gmail_credentials.json",
    "*.env", ".env*",
    # Runtime noise, regenerated on start.
    "data/*.pid", "data/*.log", "data/*_stdout.txt", "data/*_stderr.txt",
    "data/_*.py",
    # SQLite side files: their contents are folded into the snapshot.
    "*-journal", "*-wal", "*-shm",
)

_SQLITE_MAGIC = b"SQLite format 3\x00"
_NAME_RE = re.compile(r"^atlasos-(\d{8}T\d{6}Z)(?:-(\d+))?(-pre-restore)?\.tar\.gz$")
MANIFEST = "manifest.json"


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: Path
    created: datetime
    size: int
    pre_restore: bool


def backup_dir(root: Path | None = None) -> Path:
    root = root or Path.cwd()
    return (root / os.getenv("ATLASOS_BACKUP_DIR", "backups")).resolve()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _is_excluded(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in EXCLUDE)


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity_problem(db: Path) -> str | None:
    """None when SQLite's integrity_check passes, else the reason."""
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return str(e)
    result = "; ".join(r[0] for r in rows)
    return None if result == "ok" else result


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Consistent snapshot of a (possibly in-use) database via the backup API.

    Opened read-write on purpose: after a crash a database can have a hot
    journal, and SQLite must roll it back before the data is readable — a
    read-only connection fails with "attempt to write a readonly database".
    The backup itself only reads.
    """
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _iter_state_files(root: Path):
    for source in SOURCES:
        base = root / source
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                if not _is_excluded(rel):
                    yield rel, path


def _unique_name(dest: Path, stamp: str, suffix: str) -> str:
    name = f"atlasos-{stamp}{suffix}.tar.gz"
    n = 1
    while (dest / name).exists():
        name = f"atlasos-{stamp}-{n}{suffix}.tar.gz"
        n += 1
    return name


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Reject anything that could write outside the extraction directory."""
    members = tar.getmembers()
    for m in members:
        p = PurePosixPath(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise BackupError(f"unsafe entry in archive: {m.name!r}")
    return members


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def create_backup(root: Path | None = None, dest: Path | None = None,
                  pre_restore: bool = False) -> BackupInfo:
    """Snapshot data/ and chroma_db/ into one verified .tar.gz archive."""
    root = (root or Path.cwd()).resolve()
    dest = dest or backup_dir(root)
    dest.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    name = _unique_name(dest, now.strftime("%Y%m%dT%H%M%SZ"),
                        "-pre-restore" if pre_restore else "")

    files: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="atlasos-backup-") as tmp:
        stage = Path(tmp) / "stage"
        for rel, src in _iter_state_files(root):
            out = stage / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            is_db = _is_sqlite(src)
            if is_db:
                try:
                    _sqlite_copy(src, out)
                except sqlite3.DatabaseError as e:
                    raise BackupError(f"{rel}: cannot snapshot database: {e}") from e
                problem = _integrity_problem(out)
                if problem:
                    raise BackupError(f"{rel}: integrity check failed: {problem}")
            else:
                shutil.copy2(src, out)
            files[rel] = {"sha256": _sha256(out), "size": out.stat().st_size, "sqlite": is_db}

        if not files:
            raise BackupError(f"nothing to back up: no files under {', '.join(SOURCES)} in {root}")

        manifest = {
            "format": FORMAT_VERSION,
            "created": now.isoformat(),
            "sources": list(SOURCES),
            "files": files,
        }
        (stage / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        # Write under a temp name and rename, so an interrupted run never
        # leaves a truncated file that looks like a real backup.
        partial = dest / f".{name}.partial"
        try:
            with tarfile.open(partial, "w:gz") as tar:
                tar.add(stage / MANIFEST, arcname=MANIFEST)
                for rel in files:
                    tar.add(stage / rel, arcname=rel)
            os.replace(partial, dest / name)
        finally:
            partial.unlink(missing_ok=True)

    final = dest / name
    return BackupInfo(name, final, now, final.stat().st_size, pre_restore)


def list_backups(dest: Path | None = None) -> list[BackupInfo]:
    """All archives in the backup directory, newest first."""
    dest = dest or backup_dir()
    if not dest.is_dir():
        return []
    found = []
    for p in dest.iterdir():
        m = _NAME_RE.match(p.name)
        if m and p.is_file():
            created = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            found.append(BackupInfo(p.name, p, created, p.stat().st_size, bool(m.group(3))))
    return sorted(found, key=lambda b: (b.created, b.name), reverse=True)


def latest_backup(dest: Path | None = None) -> BackupInfo | None:
    """Newest regular (non-pre-restore) backup, or None."""
    return next((b for b in list_backups(dest) if not b.pre_restore), None)


def resolve(name_or_path: str, dest: Path | None = None) -> Path:
    p = Path(name_or_path)
    if p.is_file():
        return p
    candidate = (dest or backup_dir()) / name_or_path
    if candidate.is_file():
        return candidate
    raise BackupError(f"no such backup: {name_or_path}  (see `agent backup list`)")


def verify_backup(archive: Path) -> dict:
    """Check every file against the manifest and every database's integrity.

    Returns the manifest on success; raises BackupError describing the first
    class of problem found.
    """
    try:
        tar = tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as e:
        raise BackupError(f"not a readable archive: {e}") from e

    with tar, tempfile.TemporaryDirectory(prefix="atlasos-verify-") as tmp:
        members = _safe_members(tar)
        names = {m.name for m in members if m.isfile()}
        if MANIFEST not in names:
            raise BackupError("archive has no manifest.json")
        tar.extractall(tmp, members=members, filter="data")

        manifest = json.loads((Path(tmp) / MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("format") != FORMAT_VERSION:
            raise BackupError(f"unsupported backup format: {manifest.get('format')!r}")

        expected = set(manifest["files"])
        missing = expected - names
        extra = names - expected - {MANIFEST}
        if missing:
            raise BackupError(f"files missing from archive: {sorted(missing)}")
        if extra:
            raise BackupError(f"files not listed in manifest: {sorted(extra)}")

        bad = [rel for rel, meta in manifest["files"].items()
               if _sha256(Path(tmp) / rel) != meta["sha256"]]
        if bad:
            raise BackupError(f"checksum mismatch: {bad}")

        for rel, meta in manifest["files"].items():
            if meta.get("sqlite"):
                problem = _integrity_problem(Path(tmp) / rel)
                if problem:
                    raise BackupError(f"{rel}: integrity check failed: {problem}")
    return manifest


def restore_backup(archive: Path, root: Path | None = None,
                   dest: Path | None = None) -> tuple[dict, BackupInfo | None]:
    """Put an archive's files back. Returns (manifest, safety-backup info).

    Verifies first and takes a pre-restore backup of the current state, so
    nothing is lost if the wrong archive was chosen. Databases are written
    through the SQLite backup API, which is safe even if another process
    (the daemon, the MCP server) has them open. Files that exist now but are
    not in the archive are left alone.
    """
    root = (root or Path.cwd()).resolve()
    manifest = verify_backup(archive)

    safety = None
    if any(True for _ in _iter_state_files(root)):
        safety = create_backup(root, dest, pre_restore=True)

    with tarfile.open(archive, "r:gz") as tar, \
            tempfile.TemporaryDirectory(prefix="atlasos-restore-") as tmp:
        tar.extractall(tmp, members=_safe_members(tar), filter="data")
        for rel, meta in manifest["files"].items():
            if PurePosixPath(rel).parts[0] not in SOURCES:
                raise BackupError(f"refusing to restore outside state dirs: {rel}")
            src = Path(tmp) / rel
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if meta.get("sqlite"):
                s = sqlite3.connect(src)
                d = sqlite3.connect(target)
                try:
                    s.backup(d)
                finally:
                    d.close()
                    s.close()
            else:
                shutil.copy2(src, target)
    return manifest, safety


def prune(dest: Path | None = None, keep_daily: int = 7,
          keep_weekly: int = 4) -> list[BackupInfo]:
    """Delete old archives; returns what was removed.

    Keeps the newest archive of each of the last `keep_daily` days that have
    backups, plus the newest of each of the `keep_weekly` most recent ISO
    weeks before those. Pre-restore safety backups are never pruned.
    """
    backups = [b for b in list_backups(dest) if not b.pre_restore]
    keep: set[str] = set()

    days_seen: list = []
    for b in backups:  # newest first
        day = b.created.date()
        if day not in days_seen:
            if len(days_seen) >= keep_daily:
                break
            days_seen.append(day)
            keep.add(b.name)

    oldest_daily = min(days_seen) if days_seen else None
    weeks_seen: list = []
    for b in backups:
        if oldest_daily and b.created.date() >= oldest_daily:
            continue
        week = b.created.isocalendar()[:2]
        if week not in weeks_seen:
            if len(weeks_seen) >= keep_weekly:
                break
            weeks_seen.append(week)
            keep.add(b.name)

    removed = [b for b in backups if b.name not in keep]
    for b in removed:
        b.path.unlink(missing_ok=True)
    return removed
