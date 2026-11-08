import sqlite3
from pathlib import Path

import pytest

from app import sqlite_backup


def _create_database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO records (value) VALUES (?)", (value,))
        database.commit()


def _read_value(path: Path) -> str:
    with sqlite3.connect(path) as database:
        return database.execute("SELECT value FROM records").fetchone()[0]


def test_backup_uses_online_api_and_creates_unique_integrity_checked_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    backup_directory = tmp_path / "private-backups"
    _create_database(source, "committed value")

    first = sqlite_backup.create_backup(source, backup_directory)
    second = sqlite_backup.create_backup(source, backup_directory)

    assert first != second
    assert first.is_file()
    assert second.is_file()
    assert _read_value(first) == "committed value"
    assert sqlite_backup.validate_database(first).integrity == "ok"
    if sqlite_backup.os.name != "nt":
        assert first.stat().st_mode & 0o777 == 0o600
        assert backup_directory.stat().st_mode & 0o777 == 0o700


def test_backup_includes_committed_wal_contents_while_database_is_open(
    tmp_path: Path,
) -> None:
    source = tmp_path / "open-source.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_database(source, "before")
    writer = sqlite3.connect(source)
    try:
        writer.execute("INSERT INTO records (value) VALUES (?)", ("committed wal",))
        writer.commit()
        backup = sqlite_backup.create_backup(source, backup_directory)
        with sqlite3.connect(backup) as copy:
            values = copy.execute("SELECT value FROM records ORDER BY id").fetchall()
    finally:
        writer.close()

    assert values == [("before",), ("committed wal",)]


def test_restore_validates_stage_and_preserves_pre_restore_database(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_database(source, "restored value")
    _create_database(target, "original value")
    backup = sqlite_backup.create_backup(source, backup_directory)

    pre_restore = sqlite_backup.restore_database(
        backup,
        target,
        backup_directory,
    )

    assert pre_restore is not None
    assert pre_restore.is_file()
    assert sqlite_backup.validate_database(target).integrity == "ok"
    assert sqlite_backup.validate_database(pre_restore).integrity == "ok"
    assert _read_value(target) == "restored value"
    assert _read_value(pre_restore) == "original value"


def test_restore_can_initialize_a_missing_database_file(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "new-target.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_database(source, "restored value")
    backup = sqlite_backup.create_backup(source, backup_directory)

    pre_restore = sqlite_backup.restore_database(
        backup,
        target,
        backup_directory,
    )

    assert pre_restore is None
    assert sqlite_backup.validate_database(target).integrity == "ok"
    assert _read_value(target) == "restored value"


def test_invalid_backup_does_not_change_restore_target_or_create_prebackup(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite3"
    invalid_backup = tmp_path / "invalid.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_database(target, "must remain")
    invalid_backup.write_text("not a SQLite database", encoding="utf-8")

    with pytest.raises(sqlite_backup.BackupError):
        sqlite_backup.restore_database(
            invalid_backup,
            target,
            backup_directory,
        )

    assert _read_value(target) == "must remain"
    assert not backup_directory.exists()


def test_restore_failure_rolls_back_from_pre_restore_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    backup_directory = tmp_path / "backups"
    _create_database(source, "new value")
    _create_database(target, "original value")
    backup = sqlite_backup.create_backup(source, backup_directory)
    original_copy = sqlite_backup._copy_with_online_backup
    target_copy_attempts = 0

    def fail_first_target_copy(source_path: Path, destination_path: Path) -> None:
        nonlocal target_copy_attempts
        if destination_path.resolve() == target.resolve():
            target_copy_attempts += 1
            if target_copy_attempts == 1:
                raise sqlite_backup.BackupError("simulated restore failure")
        original_copy(source_path, destination_path)

    monkeypatch.setattr(
        sqlite_backup,
        "_copy_with_online_backup",
        fail_first_target_copy,
    )

    with pytest.raises(sqlite_backup.BackupError, match="simulated restore failure"):
        sqlite_backup.restore_database(backup, target, backup_directory)

    assert target_copy_attempts == 2
    assert _read_value(target) == "original value"
    assert len(list(backup_directory.glob("*pre-restore*.sqlite3"))) == 1


def test_backup_failure_does_not_leave_partial_backup_files(tmp_path: Path) -> None:
    backup_directory = tmp_path / "backups"

    with pytest.raises(sqlite_backup.BackupError, match="does not exist"):
        sqlite_backup.create_backup(
            tmp_path / "missing.sqlite3",
            backup_directory,
        )

    assert not backup_directory.exists()
