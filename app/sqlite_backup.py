from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import settings

BACKUP_PREFIX = "saas-ecommerce"


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupValidation:
    integrity: str
    foreign_key_violations: int


def configured_database_path() -> Path:
    url = make_url(settings.DATABASE_URL)
    if url.drivername != "sqlite+aiosqlite":
        raise BackupError("Only SQLite databases can be backed up")
    database = url.database
    if (
        not database
        or database == ":memory:"
        or database.startswith("file:")
        or url.query
    ):
        raise BackupError("A file-backed SQLite DATABASE_URL is required")
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path.resolve()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError("SQLite database file does not exist")
    uri = path.resolve().as_uri() + "?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise BackupError("Could not open SQLite database read-only") from exc


def validate_database(path: Path) -> BackupValidation:
    connection = _readonly_connection(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise BackupError("SQLite integrity check failed")
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(1)
        if violations:
            raise BackupError("SQLite foreign key check failed")
        return BackupValidation(integrity="ok", foreign_key_violations=0)
    except sqlite3.Error as exc:
        raise BackupError("Could not validate SQLite database") from exc
    finally:
        connection.close()


def _secure_directory(path: Path) -> Path:
    if path.exists() and not path.is_dir():
        raise BackupError("Backup destination is not a directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = path.resolve()
    if os.name != "nt":
        try:
            os.chmod(resolved, 0o700)
        except OSError as exc:
            raise BackupError(
                "Could not restrict backup directory permissions"
            ) from exc
    return resolved


def _reserve_backup_file(directory: Path, *, label: str = "backup") -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    for _ in range(10):
        destination = directory / (
            f"{BACKUP_PREFIX}-{label}-{stamp}-{secrets.token_hex(4)}.sqlite3"
        )
        try:
            descriptor = os.open(
                destination,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            continue
        os.close(descriptor)
        if os.name != "nt":
            os.chmod(destination, 0o600)
        return destination
    raise BackupError("Could not reserve a unique backup filename")


def _backup_to_new_file(source_path: Path, destination_path: Path) -> None:
    source = _readonly_connection(source_path)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(destination_path, timeout=30)
        destination.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=256, sleep=0.1)
        destination.commit()
    except sqlite3.Error as exc:
        raise BackupError("SQLite online backup failed") from exc
    finally:
        source.close()
        if destination is not None:
            destination.close()


def create_backup(
    source_path: Path,
    backup_directory: Path,
    *,
    label: str = "backup",
) -> Path:
    source = source_path.resolve()
    if not source.is_file():
        raise BackupError("SQLite database file does not exist")
    directory = _secure_directory(backup_directory)
    destination = _reserve_backup_file(directory, label=label)
    try:
        _backup_to_new_file(source, destination)
        validate_database(destination)
        if os.name != "nt":
            os.chmod(destination, 0o600)
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _copy_with_online_backup(source_path: Path, destination_path: Path) -> None:
    source = _readonly_connection(source_path)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(destination_path, timeout=30)
        destination.execute("PRAGMA busy_timeout=30000")
        source.backup(destination, pages=256, sleep=0.1)
        destination.commit()
    except sqlite3.Error as exc:
        raise BackupError("SQLite restore operation failed") from exc
    finally:
        source.close()
        if destination is not None:
            destination.close()


def _stage_restore(backup_path: Path, target_directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".sqlite-restore-validation-",
        suffix=".sqlite3",
        dir=target_directory,
    )
    os.close(descriptor)
    staged = Path(name)
    try:
        _copy_with_online_backup(backup_path, staged)
        validate_database(staged)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def restore_database(
    backup_path: Path,
    target_path: Path,
    backup_directory: Path,
) -> Path | None:
    backup = backup_path.resolve()
    target = target_path.resolve()
    if backup == target:
        raise BackupError("Backup and target must be different files")
    validate_database(backup)

    target.parent.mkdir(parents=True, exist_ok=True)
    staged = _stage_restore(backup, target.parent)
    pre_restore_backup: Path | None = None
    try:
        if target.exists():
            pre_restore_backup = create_backup(
                target,
                backup_directory,
                label="pre-restore",
            )
        _copy_with_online_backup(staged, target)
        validate_database(target)
        return pre_restore_backup
    except Exception:
        if pre_restore_backup is not None:
            try:
                _copy_with_online_backup(pre_restore_backup, target)
                validate_database(target)
            except Exception as rollback_error:
                raise BackupError(
                    "Restore failed and automatic rollback failed; "
                    "preserve the pre-restore backup for recovery"
                ) from rollback_error
        raise
    finally:
        staged.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, validate, or restore SQLite backups safely."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser(
        "backup", help="Create and verify a timestamped backup"
    )
    backup.add_argument("--backup-dir", type=Path, required=True)

    validate = commands.add_parser("validate", help="Validate a backup read-only")
    validate.add_argument("--backup", type=Path, required=True)

    restore = commands.add_parser(
        "restore",
        help="Validate a backup, preserve the current database, then restore it",
    )
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument(
        "--apply",
        action="store_true",
        required=True,
        help="Confirm database replacement after validation and pre-restore backup",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            created = create_backup(configured_database_path(), args.backup_dir)
            print(f"Verified backup created: {created}")
        elif args.command == "validate":
            result = validate_database(args.backup)
            print(
                "Backup validation passed "
                f"(integrity={result.integrity}, "
                f"foreign_key_violations={result.foreign_key_violations})"
            )
        else:
            target = configured_database_path()
            pre_restore_backup = restore_database(
                args.backup,
                target,
                args.backup_dir,
            )
            print("Restore validation passed and database restored.")
            if pre_restore_backup is not None:
                print(f"Pre-restore backup retained: {pre_restore_backup}")
        return 0
    except (BackupError, OSError) as exc:
        print(f"SQLite backup operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
