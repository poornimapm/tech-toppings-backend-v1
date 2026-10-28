"""Restore a backup made by `poe backup` into the database MONGO_URI names (ADR-0019).

Every file of the backup is checked before anything is written. The collections it holds must
be empty there, unless --drop is given, which replaces them. It shows where the backup came from
and where it goes, and asks for that host and database before writing (--yes skips that, for
scripts; never with --drop on a database that is not on this machine).
Usage: ``poe restore --from backups/tech-toppings-v1-20261028T093000Z [--drop] [--yes]``
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.core.backup import BackupError, Target, check, restore
from app.core.config import AppSettings, BackupSettings, MongoSettings, default_env_files


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--from", dest="folder", type=Path, required=True, help="Backup folder")
    parser.add_argument("--drop", action="store_true", help="Replace the collections it holds")
    parser.add_argument("--yes", action="store_true", help="Don't ask for the host and database")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    files = tuple(path for path in default_env_files() if path.is_file()) or None
    mongo, settings = MongoSettings(_env_file=files), BackupSettings(_env_file=files)
    uri = mongo.uri.get_secret_value()
    target = Target.of(uri)
    if args.yes and args.drop and not target.local:
        print("error: --yes can't be used with --drop on a database that is not on this machine")
        return 1
    client: MongoClient[dict[str, object]] = MongoClient(uri, appname=f"{mongo.app_name}-restore")
    try:
        db = client.get_default_database()
        manifest = check(args.folder, settings)
        created = f"{manifest['created_at']:%Y-%m-%d %H:%M} UTC"
        print(f"Backup of {manifest['database']} on {manifest.get('source', '?')}, {created}")
        where = f"{target.hosts}/{db.name}"
        print(f"Restoring into {where} (APP_ENV={AppSettings(_env_file=files).env})")
        if not args.yes and input(f"Type {where} to go ahead: ").strip() != where:
            print("Nothing was changed.")
            return 1
        done = restore(db, args.folder, settings, drop=args.drop)
    except (BackupError, PyMongoError) as exc:
        print(f"error: {exc}")
        return 1
    finally:
        client.close()
    print(f"Restored {sum(done.counts.values())} documents into {db.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
