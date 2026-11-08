# SQLite backup and restore

The application database is SQLite-only. Use the existing utility
`python -m app.sqlite_backup`; it uses SQLite's online backup API, validates
integrity and foreign keys, reserves unique backup files, and preserves a
pre-restore backup before replacing an existing target.

## Create and validate a backup

Configure `DATABASE_URL` for the intended database, then from
`saas_ecommerce_backend`:

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
python -m app.sqlite_backup backup --backup-dir .\backups
$backup = Get-ChildItem .\backups\saas-ecommerce-backup-*.sqlite3 |
    Sort-Object Name -Descending |
    Select-Object -First 1
python -m app.sqlite_backup validate --backup $backup.FullName
```

Do not copy a live SQLite database file directly while WAL is active. Retain
backups in access-controlled persistent storage and keep an off-host recovery
copy according to the operator's retention policy. The utility does not delete
old backups.

On POSIX, the utility applies `0700` directory and `0600` file permissions.
When run natively on Windows, it applies and verifies a protected DACL granting
Full Control to the current user SID and built-in Administrators only. It uses
.NET `DirectorySecurity`/`FileSecurity` access-control sections; it does not
read or modify SACL/audit rules. Directories propagate access to child files
and directories; files do not inherit to children. Failure to apply or verify
the ACL stops backup/restore.

## Restore and recovery

Stop all application/database writers before restore. Validate the selected
backup and confirm the configured target path before running:

```powershell
python -m app.sqlite_backup validate --backup $backup.FullName
python -m app.sqlite_backup restore `
    --backup $backup.FullName `
    --backup-dir .\backups `
    --apply
```

Restore stages and validates a separate SQLite copy. If the target exists, the
utility creates and validates a unique `pre-restore` backup before replacing
it. On a restore failure, it attempts to restore from that pre-restore backup.
If rollback also fails, preserve the source, pre-restore backup, and failed
target for recovery; do not resume application writes until the database has
been inspected.

The restore process does not remove existing backup files. Newly created
staging files are cleaned up on ordinary success/failure paths. Preserve both
the source backup and pre-restore backup until the restored application has
been checked.

## Docker storage

Compose mounts separate named volumes at `/data` and `/backups`. A container
backup is not an off-host backup; copy and validate an additional protected
copy outside Docker. Stop the API before a restore, and use the same persistent
database and backup volumes for the one-off restore task. Secure the Docker
host/VM storage and copied host backups separately. Never use
`docker compose down -v` unless intentionally deleting development data.

See the Windows-specific commands and operational details in
[README.md](./README.md#sqlite-backup-and-restore). Production backup retention
and disaster-recovery objectives must be established and tested by the
deployment operator; they are not configured by this repository.
