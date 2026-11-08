# Deployment guide

This document records the safe deployment boundary and validation required
before operating this SQLite-backed service. No deployment has been performed.

## Requirements and configuration

- Use one application host/process topology appropriate for SQLite. Keep the
  database on local persistent storage; do not share a WAL database across
  hosts or use an unverified network filesystem.
- Supply `APP_NAME`, `ENVIRONMENT=production`, `DATABASE_URL` using
  `sqlite+aiosqlite`, a separately generated `SECRET_KEY`, `JWT_ALGORITHM`,
  `ACCESS_TOKEN_EXPIRE_MINUTES`, and explicit HTTPS `CORS_ORIGINS` through a
  secret/configuration manager.
- Never bake `.env` files or runtime secrets into an image. The repository
  `.dockerignore` excludes environment files, credentials, local SQLite files,
  tests, and VCS metadata.
- Terminate HTTPS at a maintained proxy, restrict accepted hosts and network
  access, and trust forwarded headers only from configured proxy addresses.

## Build and run

The Dockerfile pins its Python base image and runs as UID/GID `10001`. Compose
uses a read-only root filesystem, a bounded `/tmp`, dropped capabilities,
`no-new-privileges`, loopback-only publishing, and named volumes for `/data`
and `/backups`.

From `saas_ecommerce_backend`, validate configuration and build before starting
any service:

```powershell
$env:DEV_SECRET_KEY = (python -c "import secrets; print(secrets.token_urlsafe(48))")
docker compose config --quiet
docker compose build api
```

The Compose file is for local development, not production. Supply equivalent
non-root, read-only, persistent-volume, network restriction, resource, health,
and shutdown controls in the production runtime. This project’s expected
production topology is one Uvicorn worker.

Apply migrations as a serialized deployment step only after stopping writers
and taking a verified backup. Review the existing-database adoption procedure
in [README.md](./README.md#existing-sqlite-database-without-alembic-history);
never apply a baseline migration blindly to an existing database.

The readiness endpoint is `/health/ready`; it checks connectivity and the
expected schema/revision. Readiness is verification only and does not migrate
or create database objects.

## Current validation boundary

`docker compose config --quiet` passed during the 2026-11-06 audit. Docker
image build, production-like container startup, mounted volume persistence,
container health-check behavior, and container shutdown were **not verified**:
the local Docker daemon was unavailable. Do not treat the configuration parse
as a deployment simulation.

For backup, restore, rollback, migration, and recovery commands, see
[BACKUP_RESTORE.md](./BACKUP_RESTORE.md). No registry publishing, deployment,
or production infrastructure changes are performed by this guide.
