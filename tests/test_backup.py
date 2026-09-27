"""Backup/restore: a backup that has never been restored is not a backup.

Everything runs against a throwaway project root in tmp_path — the real
data/ is never touched.
"""
import io
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from agent.core import backup as bk


def _make_db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.execute("DELETE FROM t")
    conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")]
    finally:
        conn.close()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    _make_db(root / "data" / "memory.db", ["fix applied to infragpt"])
    _make_db(root / "chroma_db" / "chroma.sqlite3", ["embedding"])
    (root / "data" / "vault").mkdir(parents=True)
    (root / "data" / "vault" / "cluster.md").write_text("# notes\n", encoding="utf-8")
    (root / "data" / "runbooks.yaml").write_text("runbooks: []\n", encoding="utf-8")
    # Must never end up in an archive.
    (root / "data" / "gmail_token.json").write_text('{"token": "SECRET"}', encoding="utf-8")
    (root / "data" / "daemon.pid").write_text("123", encoding="utf-8")
    (root / "data" / "daemon.log").write_text("noise", encoding="utf-8")
    (root / "data" / "memory.db-journal").write_bytes(b"x")
    return root


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "backups"


def _members(archive):
    with tarfile.open(archive, "r:gz") as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


# --- create ---------------------------------------------------------------

def test_create_includes_state_and_manifest(project, dest):
    info = bk.create_backup(project, dest)
    assert info.path.is_file() and info.name.startswith("atlasos-")
    assert _members(info.path) == {
        "manifest.json", "data/memory.db", "data/vault/cluster.md",
        "data/runbooks.yaml", "chroma_db/chroma.sqlite3",
    }


def test_create_excludes_secrets_and_runtime_noise(project, dest):
    names = _members(bk.create_backup(project, dest).path)
    for leaked in ("data/gmail_token.json", "data/daemon.pid",
                   "data/daemon.log", "data/memory.db-journal"):
        assert leaked not in names
    with tarfile.open(bk.create_backup(project, dest).path, "r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                assert b"SECRET" not in tar.extractfile(m).read()


def test_create_snapshots_a_database_that_is_open(project, dest):
    # A connection holding the db open (as the daemon would) must not matter.
    live = sqlite3.connect(project / "data" / "memory.db")
    live.execute("INSERT INTO t VALUES ('committed while open')")
    live.commit()
    try:
        info = bk.create_backup(project, dest)
    finally:
        live.close()
    out = dest / "x"
    with tarfile.open(info.path, "r:gz") as tar:
        tar.extractall(out, filter="data")
    assert _rows(out / "data" / "memory.db") == ["fix applied to infragpt", "committed while open"]


def test_create_refuses_a_corrupt_database(project, dest):
    db = project / "data" / "memory.db"
    raw = bytearray(db.read_bytes())
    raw[100:4096] = b"\xff" * (4096 - 100)   # keep the header, trash page 1
    db.write_bytes(bytes(raw))
    with pytest.raises(bk.BackupError, match="memory.db"):
        bk.create_backup(project, dest)
    assert list(dest.iterdir()) == []   # no archive, no partial file left behind


def test_create_with_no_state_fails_clearly(tmp_path, dest):
    with pytest.raises(bk.BackupError, match="nothing to back up"):
        bk.create_backup(tmp_path / "empty", dest)


def test_same_second_backups_do_not_overwrite(project, dest):
    a = bk.create_backup(project, dest)
    b = bk.create_backup(project, dest)
    assert a.name != b.name and a.path.exists() and b.path.exists()


# --- verify ---------------------------------------------------------------

def test_verify_accepts_a_fresh_backup(project, dest):
    manifest = bk.verify_backup(bk.create_backup(project, dest).path)
    assert manifest["files"]["data/memory.db"]["sqlite"] is True


def _rewrite(archive, mutate):
    """Rebuild an archive, letting `mutate(name, data)` alter member contents."""
    with tarfile.open(archive, "r:gz") as tar:
        items = [(m.name, tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()]
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in items:
            data = mutate(name, data)
            if data is None:
                continue
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))


def test_verify_detects_a_modified_file(project, dest):
    archive = bk.create_backup(project, dest).path
    _rewrite(archive, lambda n, d: b"# tampered\n" if n == "data/vault/cluster.md" else d)
    with pytest.raises(bk.BackupError, match="checksum mismatch"):
        bk.verify_backup(archive)


def test_verify_detects_a_missing_file(project, dest):
    archive = bk.create_backup(project, dest).path
    _rewrite(archive, lambda n, d: None if n == "data/runbooks.yaml" else d)
    with pytest.raises(bk.BackupError, match="missing"):
        bk.verify_backup(archive)


def test_verify_rejects_path_traversal(project, dest):
    archive = bk.create_backup(project, dest).path
    with tarfile.open(archive, "r:gz") as tar:
        items = [(m.name, tar.extractfile(m).read()) for m in tar.getmembers() if m.isfile()]
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in items + [("../evil.txt", b"x")]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    with pytest.raises(bk.BackupError, match="unsafe entry"):
        bk.verify_backup(archive)


def test_verify_rejects_a_non_archive(dest):
    dest.mkdir()
    junk = dest / "atlasos-20260101T000000Z.tar.gz"
    junk.write_bytes(b"not a tarball")
    with pytest.raises(bk.BackupError, match="not a readable archive"):
        bk.verify_backup(junk)


# --- restore --------------------------------------------------------------

def test_restore_round_trip(project, dest):
    info = bk.create_backup(project, dest)

    _make_db(project / "data" / "memory.db", ["WRONG"])
    (project / "data" / "vault" / "cluster.md").unlink()
    (project / "data" / "runbooks.yaml").write_text("broken", encoding="utf-8")

    _, safety = bk.restore_backup(info.path, project, dest)

    assert _rows(project / "data" / "memory.db") == ["fix applied to infragpt"]
    assert (project / "data" / "vault" / "cluster.md").read_text(encoding="utf-8") == "# notes\n"
    assert (project / "data" / "runbooks.yaml").read_text(encoding="utf-8") == "runbooks: []\n"
    # The state it replaced is recoverable.
    assert safety is not None and safety.pre_restore
    with tarfile.open(safety.path, "r:gz") as tar:
        tar.extractall(dest / "undo", filter="data")
    assert _rows(dest / "undo" / "data" / "memory.db") == ["WRONG"]


def test_restore_into_an_open_database(project, dest):
    info = bk.create_backup(project, dest)
    _make_db(project / "data" / "memory.db", ["WRONG"])
    live = sqlite3.connect(project / "data" / "memory.db")
    try:
        bk.restore_backup(info.path, project, dest)
        assert [r[0] for r in live.execute("SELECT v FROM t")] == ["fix applied to infragpt"]
    finally:
        live.close()


def test_restore_leaves_secrets_and_new_files_alone(project, dest):
    info = bk.create_backup(project, dest)
    (project / "data" / "vault" / "new-note.md").write_text("later", encoding="utf-8")
    bk.restore_backup(info.path, project, dest)
    assert (project / "data" / "gmail_token.json").read_text(encoding="utf-8") == '{"token": "SECRET"}'
    assert (project / "data" / "vault" / "new-note.md").exists()


def test_restore_refuses_a_tampered_archive_and_changes_nothing(project, dest):
    archive = bk.create_backup(project, dest).path
    _rewrite(archive, lambda n, d: b"evil" if n == "data/runbooks.yaml" else d)
    _make_db(project / "data" / "memory.db", ["current"])
    with pytest.raises(bk.BackupError):
        bk.restore_backup(archive, project, dest)
    assert _rows(project / "data" / "memory.db") == ["current"]


def test_restore_refuses_files_outside_state_dirs(project, dest, tmp_path):
    # A well-formed archive whose manifest targets a path outside data/.
    payload = b"print('owned')"
    import hashlib
    manifest = {"format": 1, "created": "x", "sources": ["data"],
                "files": {"src/agent/cli.py": {"sha256": hashlib.sha256(payload).hexdigest(),
                                               "size": len(payload), "sqlite": False}}}
    archive = dest / "atlasos-20260101T000000Z.tar.gz"
    dest.mkdir(exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in (("manifest.json", json.dumps(manifest).encode()), ("src/agent/cli.py", payload)):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    with pytest.raises(bk.BackupError, match="outside state dirs"):
        bk.restore_backup(archive, project, dest)
    assert not (project / "src").exists()


# --- list / prune ---------------------------------------------------------

def _fake(dest, when, pre_restore=False):
    dest.mkdir(exist_ok=True)
    name = f"atlasos-{when.strftime('%Y%m%dT%H%M%SZ')}{'-pre-restore' if pre_restore else ''}.tar.gz"
    (dest / name).write_bytes(b"x")
    return name


def test_list_is_newest_first_and_ignores_other_files(dest):
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    older, newer = _fake(dest, t0), _fake(dest, t0 + timedelta(days=1))
    (dest / "notes.txt").write_text("x")
    (dest / f".{newer}.partial").write_bytes(b"x")
    assert [b.name for b in bk.list_backups(dest)] == [newer, older]


def test_prune_keeps_newest_per_day_then_per_week(dest):
    start = datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc)   # a Monday
    names = {}
    for day in range(60):   # two backups a day for 60 days
        for hour in (2, 14):
            t = start + timedelta(days=day, hours=hour - 2)
            names[t] = _fake(dest, t)
    safety = _fake(dest, start - timedelta(days=100), pre_restore=True)

    bk.prune(dest, keep_daily=7, keep_weekly=4)
    kept = [b for b in bk.list_backups(dest) if not b.pre_restore]

    daily = [b for b in kept if b.created.date() >= (start + timedelta(days=53)).date()]
    assert len(daily) == 7 and all(b.created.hour == 14 for b in daily)   # newest of each day
    assert len(kept) == 7 + 4
    assert (dest / safety).exists()   # pre-restore backups are never pruned


def test_prune_with_few_backups_removes_nothing(dest):
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for d in range(3):
        _fake(dest, t0 + timedelta(days=d))
    assert bk.prune(dest) == []


def test_latest_backup_ignores_pre_restore(dest):
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    regular = _fake(dest, t0)
    _fake(dest, t0 + timedelta(days=1), pre_restore=True)
    assert bk.latest_backup(dest).name == regular


def test_latest_backup_none_when_empty(dest):
    assert bk.latest_backup(dest) is None
