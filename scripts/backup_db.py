"""Back up the whole database to a new folder under BACKUP_DIR (ADR-0019).

Reads MONGO_URI and BACKUP_* like the app (.env, then environments/<APP_ENV>.env, then the
real environment). For the live database: ``$env:APP_ENV = "production"; poe backup`` with its
URI in environments/production.env (git ignores it). A read-only database user is enough. A
database on another machine is backed up only with BACKUP_ENCRYPTION_KEY set (or --plain).
Usage: ``poe backup [--plain]``
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.core.backup import BackupError, Target, backup
from app.core.config import AppSettings, BackupSettings, MongoSettings, default_env_files


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--plain", action="store_true", help="Write it unencrypted even if it's not on this machine"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    files = tuple(path for path in default_env_files() if path.is_file()) or None
    mongo, settings = MongoSettings(_env_file=files), BackupSettings(_env_file=files)
    uri = mongo.uri.get_secret_value()
    target = Target.of(uri)
    client: MongoClient[dict[str, object]] = MongoClient(uri, appname=f"{mongo.app_name}-backup")
    try:
        db = client.get_default_database()
        print(
            f"Backing up {db.name} on {target.hosts} (APP_ENV={AppSettings(_env_file=files).env})"
        )
        done = backup(db, settings, datetime.now(UTC), source=target, plain=args.plain)
    except (BackupError, PyMongoError) as exc:
        print(f"error: {exc}")
        return 1
    finally:
        client.close()
    for name, count in done.counts.items():
        print(f"{name}: {count}")
    sealed = "encrypted" if settings.encryption_key else "NOT encrypted"
    print(f"Backed up {sum(done.counts.values())} documents to {done.folder}, {sealed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
