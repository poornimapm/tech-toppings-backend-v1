"""Backups made and restored from the owner's machine (ADR-0019): every BSON type comes back
exactly, with its indexes; a database elsewhere is backed up only encrypted; a failed backup
leaves nothing; a restore checks every file before writing, never writes over data it wasn't
told to drop, and says how far it got; only the oldest complete backups are let go."""

from __future__ import annotations

import base64
import gzip
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from bson import Binary, Decimal128, ObjectId
from pymongo import ASCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import OperationFailure

from app.core import backup as backups
from app.core.backup import (
    MANIFEST,
    BackupError,
    Done,
    Target,
    backup,
    prune,
    read_manifest,
    restore,
)
from app.core.config import BackupSettings
from tests.helpers import TEST_MONGO_BASE_URI

NOW = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()
HERE = Target("localhost:27017", local=True)
ATLAS = Target("cluster0.example.mongodb.net", local=False)
ENTRY: dict[str, Any] = {
    "_id": ObjectId(),
    "title": "சாப்பாடு",  # Tamil comes back as written
    "amount_minor": 25050,
    "rate": Decimal128(Decimal("83.1234")),
    "at": datetime(2026, 10, 7, 6, 0, tzinfo=UTC).replace(tzinfo=None),
    "receipt": Binary(b"\x00\x01binary"),
    "tags": ["food", {"nested": True}],
}

Fresh = Callable[[], Database[dict[str, Any]]]


@pytest.fixture
def fresh() -> Iterator[Fresh]:
    """New, empty databases for one test, dropped afterwards."""
    client: MongoClient[dict[str, Any]] = MongoClient(TEST_MONGO_BASE_URI)
    made: list[str] = []

    def new() -> Database[dict[str, Any]]:
        made.append(f"tt-backup-{uuid.uuid4().hex[:10]}")
        return client[made[-1]]

    yield new
    for name in made:
        client.drop_database(name)
    client.close()


def settings(tmp_path: Path, **values: Any) -> BackupSettings:
    return BackupSettings(_env_file=None, dir=tmp_path, **values)


def make(db: Database[dict[str, Any]], chosen: BackupSettings, when: datetime = NOW) -> Done:
    return backup(db, chosen, when, source=HERE)


def filled(fresh: Fresh) -> Database[dict[str, Any]]:
    db = fresh()
    db.entries.insert_one(ENTRY)
    db.entries.create_index([("title", ASCENDING)], name="title", unique=True)
    db.cache.insert_one({"key": "k"})
    db.cache.create_index("at", expireAfterSeconds=60)
    return db


@pytest.mark.parametrize("key", [None, KEY])
def test_a_backup_comes_back_exactly_with_its_indexes(
    fresh: Fresh, tmp_path: Path, key: str | None
) -> None:
    source, target = filled(fresh), fresh()
    chosen = settings(tmp_path, encryption_key=key)

    made = make(source, chosen)
    restored = restore(target, made.folder, chosen, drop=False)

    assert made.folder.name == f"{source.name}-20261028T093000Z"
    assert made.counts == restored.counts == {"cache": 1, "entries": 1}
    assert target.entries.find_one() == source.entries.find_one()
    assert target.entries.index_information()["title"]["unique"] is True
    assert [spec.get("expireAfterSeconds") for spec in target.cache.list_indexes()] == [None, 60]
    manifest = read_manifest(made.folder)
    assert (manifest["encrypted"], manifest["source"]) == (key is not None, HERE.hosts)


def test_a_database_elsewhere_is_backed_up_only_encrypted(fresh: Fresh, tmp_path: Path) -> None:
    db = filled(fresh)

    with pytest.raises(BackupError, match="set BACKUP_ENCRYPTION_KEY"):
        backup(db, settings(tmp_path), NOW, source=ATLAS)
    assert list(tmp_path.iterdir()) == []  # nothing written

    assert backup(db, settings(tmp_path, encryption_key=KEY), NOW, source=ATLAS).counts
    assert backup(db, settings(tmp_path), NOW + timedelta(days=1), source=ATLAS, plain=True).counts


@pytest.mark.parametrize(
    ("uri", "hosts", "local"),
    [
        (
            "mongodb+srv://app:secret@cluster0.ab1.mongodb.net/db?w=majority",
            "cluster0.ab1.mongodb.net",
            False,
        ),
        ("mongodb://localhost:27017,127.0.0.1:27018/db", "localhost:27017,127.0.0.1:27018", True),
        ("mongodb://[::1]:27017/db", "[::1]:27017", True),
        ("mongodb://u:p@localhost.evil.test/db", "localhost.evil.test", False),
    ],
)
def test_a_target_never_shows_its_password(uri: str, hosts: str, local: bool) -> None:
    assert Target.of(uri) == Target(hosts, local)


def test_a_failed_backup_leaves_nothing_behind(
    fresh: Fresh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = filled(fresh)
    sealed: list[int] = []

    def seal_once(*args: Any) -> tuple[bytes, int]:
        if sealed:
            raise OSError("disk full")
        sealed.append(1)
        return real(*args)

    real = backups._seal
    monkeypatch.setattr(backups, "_seal", seal_once)

    with pytest.raises(OSError, match="disk full"):
        make(db, settings(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_an_encrypted_backup_reads_as_nothing_without_its_key(fresh: Fresh, tmp_path: Path) -> None:
    made = make(filled(fresh), settings(tmp_path, encryption_key=KEY))
    other = base64.urlsafe_b64encode(os.urandom(32)).decode()

    assert "சாப்பாடு".encode() not in (made.folder / "entries.jsonl.gz").read_bytes()
    with pytest.raises(BackupError, match="set BACKUP_ENCRYPTION_KEY"):
        restore(fresh(), made.folder, settings(tmp_path), drop=False)
    with pytest.raises(BackupError, match="it's another key"):
        restore(fresh(), made.folder, settings(tmp_path, encryption_key=other), drop=False)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda file: file.unlink(), "entries can't be read"),
        (lambda file: file.write_bytes(file.read_bytes()[:20]), "entries can't be read"),
        (lambda file: file.write_bytes(gzip.compress(b"")), "entries: 0 documents in this backup"),
    ],
)
def test_a_damaged_backup_changes_nothing(
    fresh: Fresh, tmp_path: Path, damage: Callable[[Path], object], message: str
) -> None:
    db = filled(fresh)
    made = make(db, settings(tmp_path))
    damage(made.folder / "entries.jsonl.gz")
    db.cache.insert_one({"key": "newer"})

    with pytest.raises(BackupError, match=message):
        restore(db, made.folder, settings(tmp_path), drop=True)
    assert db.cache.count_documents({}) == 2  # checked before anything was dropped


def test_a_restore_stopped_part_way_says_what_it_replaced(
    fresh: Fresh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = filled(fresh)
    made = make(db, settings(tmp_path))
    replace = backups._replace

    def failing(db: Any, name: str, *args: Any) -> int:
        if name == "entries":
            raise OperationFailure("you are over your space quota")
        return replace(db, name, *args)

    monkeypatch.setattr(backups, "_replace", failing)

    with pytest.raises(BackupError, match=r"stopped at entries .*quota.*; already replaced: cache"):
        restore(db, made.folder, settings(tmp_path), drop=True)


def test_a_restore_writes_over_nothing_it_was_not_told_to_drop(
    fresh: Fresh, tmp_path: Path
) -> None:
    source = filled(fresh)
    made = make(source, settings(tmp_path))
    source.entries.insert_one({"title": "added after the backup"})

    with pytest.raises(BackupError, match=r"not empty in .*: cache, entries"):
        restore(source, made.folder, settings(tmp_path), drop=False)
    assert source.entries.count_documents({}) == 2  # untouched

    restore(source, made.folder, settings(tmp_path), drop=True)
    assert [doc["title"] for doc in source.entries.find()] == [ENTRY["title"]]


def test_collections_can_be_left_out(fresh: Fresh, tmp_path: Path) -> None:
    made = make(filled(fresh), settings(tmp_path, skip_collections="cache"))

    assert made.counts == {"entries": 1}
    assert not (made.folder / "cache.jsonl.gz").exists()


def test_only_this_databases_oldest_backups_are_let_go(fresh: Fresh, tmp_path: Path) -> None:
    db = filled(fresh)
    kept = settings(tmp_path, keep=2)
    stranger = tmp_path / f"{db.name}-notes"  # no manifest, not a backup's name
    stranger.mkdir()
    # A backup of another database whose name starts with this one's (a scratch copy).
    scratch = tmp_path / f"{db.name}-scratch-20261020T000000Z"
    scratch.mkdir()
    (scratch / MANIFEST).write_text('{"format": 1}')

    folders = [make(db, kept, NOW + timedelta(days=day)).folder for day in range(3)]

    assert [path.exists() for path in folders] == [False, True, True]
    assert stranger.exists()
    assert scratch.exists()
    assert prune(db.name, kept) == []


def test_the_backup_just_written_is_never_let_go(fresh: Fresh, tmp_path: Path) -> None:
    db = filled(fresh)
    kept = settings(tmp_path, keep=1)
    newer = make(db, kept, NOW + timedelta(days=1)).folder

    older = make(db, kept, NOW).folder  # written last, though older by its name

    assert (older.exists(), newer.exists()) == (True, True)


def test_a_folder_is_never_written_over(fresh: Fresh, tmp_path: Path) -> None:
    db = filled(fresh)
    make(db, settings(tmp_path))

    with pytest.raises(FileExistsError):
        make(db, settings(tmp_path))


def test_what_is_not_a_backup_is_said_so(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match=f"no readable {MANIFEST}"):
        read_manifest(tmp_path)
    (tmp_path / MANIFEST).write_text('{"format": 99}')
    with pytest.raises(BackupError, match="format this version can't read"):
        read_manifest(tmp_path)
