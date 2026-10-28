"""Copies of the whole database, made and restored from the owner's own machine (ADR-0019):
Atlas's Free cluster keeps no backups of its own.

A backup is one folder, ``<database>-<UTC time>``. Each collection is a file of its documents as
canonical Extended JSON lines, so every BSON type comes back exactly, gzipped, and encrypted
with ``BACKUP_ENCRYPTION_KEY`` (Fernet): a database on another machine is only ever backed up
encrypted. ``manifest.json`` names where it came from and each collection with its count and
indexes (no personal data). A backup is written under a ``.partial`` name and renamed once
complete, so a failed one leaves nothing behind.

Restoring checks every file (it opens, decrypts and holds as many documents as its manifest says)
before writing anything, then writes into empty collections, or ones it may drop.
"""

from __future__ import annotations

import gzip
import re
import shutil
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bson import json_util
from bson.errors import BSONError
from cryptography.fernet import Fernet, InvalidToken
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.core.config import BackupSettings

FORMAT = 1
MANIFEST = "manifest.json"
SUFFIX = ".jsonl.gz"
PARTIAL = ".partial"
EXACT = json_util.CANONICAL_JSON_OPTIONS
# What an index's description holds that is not an option for making it again.
NOT_OPTIONS = {"v", "key", "ns"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
UNREADABLE = (OSError, EOFError, zlib.error, UnicodeDecodeError, ValueError, BSONError)

Documents = Database[dict[str, Any]]


class BackupError(Exception):
    """A backup that can't be made or restored as asked; the message says why."""


@dataclass(frozen=True)
class Done:
    folder: Path
    counts: dict[str, int]


@dataclass(frozen=True)
class Target:
    """Where a connection string points, never with its credentials."""

    hosts: str
    local: bool

    @classmethod
    def of(cls, uri: str) -> Target:
        hosts = urlsplit(uri).netloc.rpartition("@")[2]
        names = {re.sub(r":\d+$", "", host).strip("[]") for host in hosts.split(",")}
        return cls(hosts, names <= LOCAL_HOSTS)


def _box(settings: BackupSettings) -> Fernet | None:
    key = settings.encryption_key
    return Fernet(key.get_secret_value().encode()) if key is not None else None


def _seal(box: Fernet | None, lines: Iterator[str]) -> tuple[bytes, int]:
    count = 0

    def numbered() -> Iterator[bytes]:
        nonlocal count
        for line in lines:
            count += 1
            yield line.encode() + b"\n"

    packed = gzip.compress(b"".join(numbered()))
    return (box.encrypt(packed) if box else packed), count


def _documents(folder: Path, name: str, box: Fernet | None) -> list[dict[str, Any]]:
    """One collection's documents, read back from its file; BackupError if it can't be."""
    try:
        data = (folder / f"{name}{SUFFIX}").read_bytes()
        if box is not None:
            data = box.decrypt(data)
        return [
            json_util.loads(line, json_options=EXACT) for line in gzip.decompress(data).splitlines()
        ]
    except InvalidToken as exc:
        raise BackupError(f"BACKUP_ENCRYPTION_KEY can't open {name}: it's another key") from exc
    except UNREADABLE as exc:
        raise BackupError(f"{name} can't be read from this backup: {type(exc).__name__}") from exc


def backup(
    db: Documents, settings: BackupSettings, now: datetime, *, source: Target, plain: bool = False
) -> Done:
    """Write every collection of ``db`` (but ``BACKUP_SKIP_COLLECTIONS``) to a new folder, then
    keep only the newest ``BACKUP_KEEP`` backups of this database. A database that isn't on this
    machine is backed up only with a key, unless ``plain`` says otherwise."""
    box = _box(settings)
    if box is None and not (source.local or plain):
        raise BackupError(f"set BACKUP_ENCRYPTION_KEY: {source.hosts} is not on this machine")
    folder = settings.path / f"{db.name}-{now:%Y%m%dT%H%M%SZ}"
    if folder.exists():
        raise FileExistsError(folder)  # never written over
    partial = folder.with_name(folder.name + PARTIAL)
    partial.mkdir(parents=True)
    try:
        collections: dict[str, Any] = {}
        for name in sorted(db.list_collection_names()):
            if name in settings.skip_collections or name.startswith("system."):
                continue
            lines = (json_util.dumps(doc, json_options=EXACT) for doc in db[name].find())
            data, count = _seal(box, lines)
            (partial / f"{name}{SUFFIX}").write_bytes(data)
            indexes = [spec for spec in db[name].list_indexes() if spec["name"] != "_id_"]
            collections[name] = {"count": count, "indexes": indexes}
        manifest = {
            "format": FORMAT,
            "database": db.name,
            "source": source.hosts,
            "created_at": now,
            "encrypted": box is not None,
            "collections": collections,
        }
        (partial / MANIFEST).write_text(json_util.dumps(manifest, json_options=EXACT, indent=2))
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    partial.rename(folder)
    prune(db.name, settings, keeping=folder)
    return Done(folder, {name: item["count"] for name, item in collections.items()})


def prune(database: str, settings: BackupSettings, *, keeping: Path | None = None) -> list[Path]:
    """Delete this database's backups past the newest ``BACKUP_KEEP``: only complete backups
    of exactly this database, and never ``keeping`` (the one just written)."""
    name = re.compile(rf"{re.escape(database)}-\d{{8}}T\d{{6}}Z")
    ours = (
        sorted(
            path
            for path in settings.path.iterdir()
            if name.fullmatch(path.name) and (path / MANIFEST).is_file()
        )
        if settings.path.is_dir()
        else []
    )
    old = [path for path in ours[: max(0, len(ours) - settings.keep)] if path != keeping]
    for path in old:
        shutil.rmtree(path)
    return old


def read_manifest(folder: Path) -> dict[str, Any]:
    try:
        manifest: dict[str, Any] = json_util.loads((folder / MANIFEST).read_text())
    except UNREADABLE as exc:
        raise BackupError(f"{folder} is not a backup (no readable {MANIFEST})") from exc
    if manifest.get("format") != FORMAT:
        raise BackupError(f"{folder} is in a format this version can't read")
    return manifest


def check(folder: Path, settings: BackupSettings) -> dict[str, Any]:
    """The backup's manifest, once every file of it opens and holds what the manifest says."""
    manifest = read_manifest(folder)
    if manifest["encrypted"] and settings.encryption_key is None:
        raise BackupError("this backup is encrypted: set BACKUP_ENCRYPTION_KEY to its key")
    box = _box(settings) if manifest["encrypted"] else None
    for name, item in manifest["collections"].items():
        found = len(_documents(folder, name, box))
        if found != item["count"]:
            raise BackupError(f"{name}: {found} documents in this backup, {item['count']} listed")
    return manifest


def restore(db: Documents, folder: Path, settings: BackupSettings, *, drop: bool) -> Done:
    """Write a backup into ``db``, once every file of it has been checked. Collections the backup
    has must be empty there, or, with ``drop``, they are replaced. Each count is checked before
    its indexes are made again (so expiring documents can't make it look short), and a failure
    part-way says which collections were already replaced."""
    manifest = check(folder, settings)
    box = _box(settings) if manifest["encrypted"] else None
    collections: dict[str, Any] = manifest["collections"]
    taken = [name for name in collections if db[name].estimated_document_count() > 0]
    if taken and not drop:
        raise BackupError(f"not empty in {db.name}: {', '.join(taken)} (drop them to restore)")
    counts: dict[str, int] = {}
    for name, item in collections.items():
        try:
            counts[name] = _replace(db, name, _documents(folder, name, box), item, settings)
        except (PyMongoError, BackupError) as exc:
            done = ", ".join(counts) or "none"
            raise BackupError(f"stopped at {name} ({exc}); already replaced: {done}") from exc
    return Done(folder, counts)


def _replace(
    db: Documents,
    name: str,
    documents: list[dict[str, Any]],
    item: dict[str, Any],
    settings: BackupSettings,
) -> int:
    db.drop_collection(name)
    size = settings.restore_batch
    for start in range(0, len(documents), size):
        db[name].insert_many(documents[start : start + size])
    count = db[name].count_documents({})
    if count != item["count"]:
        raise BackupError(f"{count} documents restored, {item['count']} saved")
    # Expiring indexes last: until then nothing removes a restored document.
    for spec in sorted(item["indexes"], key=lambda spec: "expireAfterSeconds" in spec):
        options = {key: value for key, value in spec.items() if key not in NOT_OPTIONS}
        db[name].create_index(list(spec["key"].items()), **options)
    return count
