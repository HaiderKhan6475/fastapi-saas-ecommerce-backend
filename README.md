# Advanced Multi-Tenant SaaS E-Commerce API

FastAPI scaffold using async SQLAlchemy, SQLite, and Pydantic v2.

## Local development

Supported Python versions are 3.11-3.13, on Windows, macOS, and Linux. The
backend supports SQLite 3.35 or newer (including the SQLite library bundled
with the supported Python releases). CI checks this Python matrix and the SQLite
version available in each runner.

From this directory, create and activate a virtual environment and install the
reproducibly locked development dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.lock
Copy-Item .env.example .env
```

`requirements.txt` declares production dependency ranges.
`requirements-dev.in` adds the development tools; `requirements.lock` and
`requirements-dev.lock` are generated with pip-tools and pin exact versions.
The lock files intentionally omit artifact hashes because they are generated
across supported platforms; installation still uses exact transitive versions.
When changing dependency declarations, regenerate both locks using the
documented commands below, inspect the diff, run `pip-audit`, and run the full
checks.

`DATABASE_URL` is the SQLite-only database location setting. Relative SQLite
paths resolve from this backend directory, not the process working directory;
an absolute SQLite path can be supplied through the same environment variable
for local or deployment-specific storage.

`.env` is local-only and is ignored by version control and Docker build contexts.
The example key is a placeholder for development only; never use it in
production. Keep real credentials out of source files, command output, logs, and
distributable archives.

## SQLite schema and migrations

The application uses SQLAlchemy's async SQLite driver (`aiosqlite`). The
database engine enables foreign keys and a 30-second busy timeout on every
connection, creates the parent directory for file-backed databases, and uses
WAL mode for file-backed databases. WAL improves reader/writer coexistence but
does not make SQLite a multi-writer database; keep the database on local
persistent storage and use a single application host/process topology suitable
for SQLite. Do not place a WAL database on a network filesystem without
validating its locking and shared-memory semantics.

Application startup does not create, drop, or migrate tables. Apply the
versioned migrations explicitly from this directory:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic check
```

The initial migration (`0001_initial_schema`) reproduces the schema that
existed before Alembic was introduced. The next migration adds product price
and stock checks and tenant lookup indexes. The authentication migration adds
the database-backed rate-limit buckets. Product prices retain the existing
`NUMERIC(15,4)` / `Decimal` representation, positive-price policy,
supported-currency behavior, and decimal-text API serialization. Cart prices
are read from current product records; cart changes do not alter product price
or currency behavior. Migration `0005` freezes the 177 supported currency
codes present when it was authored; that historical migration must not be
rewritten when runtime currency support changes.

### Existing SQLite database without Alembic history

Do **not** run `alembic upgrade head` directly against a database that already
has application tables but no `alembic_version` table. Do not stamp it merely
because the table names appear familiar.

1. Stop application writers and take a verified SQLite online backup. From
   this directory, the backup utility reads the configured `DATABASE_URL`,
   reserves a unique timestamped name, and checks database and foreign-key
   integrity:

   ```powershell
   New-Item -ItemType Directory -Force .\backups | Out-Null
   python -m app.sqlite_backup backup --backup-dir .\backups
   ```

   Backups are ignored by Git and must be kept in access-controlled storage.
   Never copy a live `.db` file; copying it while WAL is active may omit
   committed contents.
2. Run read-only checks on the target and backup: `PRAGMA integrity_check`,
   `PRAGMA foreign_key_check`, and inspect `sqlite_master` plus
   `PRAGMA table_info`, `PRAGMA index_list`, and `PRAGMA foreign_key_list` for
   every table. Compare the result to `migrations/versions/0001_initial_schema.py`.
   Check for duplicate tenant slugs/emails and invalid product stock/prices
   before adopting the later constraints. These checks must not print row data.
3. Perform the proposed `alembic stamp 0001_initial_schema` and
   `alembic upgrade head` against a separate backup copy first. Verify
   `PRAGMA integrity_check`, foreign-key checks, expected indexes/constraints,
   and representative application behavior on that copy.
4. Only after schema and backup verification, stamp the real database at
   `0001_initial_schema`, then run `alembic upgrade head`. Keep the backup until
   post-migration checks and application smoke tests succeed.

The database inspected when this migration was authored contained the three
expected tables and no rows; this does not establish that another environment's
database is empty or identical. The initial migration is a baseline, not a
data export or a command to recreate an existing database.

## SQLite backup and restore

The `python -m app.sqlite_backup` utility uses SQLite's online backup API.
Backups receive unique UTC timestamped filenames and are checked with
`PRAGMA integrity_check` and `PRAGMA foreign_key_check`. Backup directories are
restricted to owner access on POSIX systems; on Windows, store them in a
directory whose NTFS ACL grants access only to authorized operators. Backups
are not served by the API and must not be placed in a web/static directory.
The utility does not delete old backups.

### Windows PowerShell

Run these commands from the backend directory. Backup and validation may run
while the application is open; stop all application writers before restore.
`DATABASE_URL` determines the database file.

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
icacls .\backups /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F"
python -m app.sqlite_backup backup --backup-dir .\backups
$backup = Get-ChildItem .\backups\saas-ecommerce-backup-*.sqlite3 |
    Sort-Object Name -Descending |
    Select-Object -First 1
python -m app.sqlite_backup validate --backup $backup.FullName
```

After confirming the selected file and stopping the application, validate and
restore it with an explicit confirmation flag:

```powershell
python -m app.sqlite_backup validate --backup $backup.FullName
python -m app.sqlite_backup restore `
    --backup $backup.FullName `
    --backup-dir .\backups `
    --apply
```

Restore first copies the selected backup through SQLite's online backup API to
a separate temporary database and validates it. If the configured target
already exists, it creates and validates a unique `pre-restore` backup before
restoring. A restore failure triggers an attempt to restore that pre-restore
backup. Preserve both the source backup and pre-restore backup until the
restored application has been checked.

### Docker Compose

Compose mounts a separate named `sqlite_backups` volume at `/backups`. It is
private to the API service and is not exposed as an HTTP route. Create a backup
while the API is running, then validate it:

```powershell
$backupOutput = docker compose exec -T api python -m app.sqlite_backup backup --backup-dir /backups
$backupOutput
$containerBackup = ($backupOutput | Select-String '^Verified backup created: ').Line.Substring('Verified backup created: '.Length)
docker compose exec -T api python -m app.sqlite_backup validate --backup $containerBackup
```

To keep an additional copy outside Docker, copy the exact path printed by the
backup command to a protected host directory:

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
docker compose cp "api:$containerBackup" .\backups\
$backupName = Split-Path $containerBackup -Leaf
python -m app.sqlite_backup validate --backup (Join-Path .\backups $backupName)
```

For restore, copy the chosen host backup into the backup volume while the
service is available, then stop the API. Validate and restore using a one-off
container with the same Compose database volume and configuration, and start
the API only after both commands succeed:

```powershell
$backup = Get-ChildItem .\backups\saas-ecommerce-backup-*.sqlite3 |
    Sort-Object Name -Descending |
    Select-Object -First 1
docker compose cp $backup.FullName api:/backups/restore.sqlite3
docker compose stop api
docker compose run --rm --no-deps --entrypoint python api -m app.sqlite_backup validate --backup /backups/restore.sqlite3
docker compose run --rm --no-deps --entrypoint python api -m app.sqlite_backup restore --backup /backups/restore.sqlite3 --backup-dir /backups --apply
docker compose start api
docker compose ps
```

If validation or restore fails, leave the API stopped and retain all files for
recovery; do not remove or overwrite the source backup or the automatically
created pre-restore backup. Named volumes are not a substitute for off-host,
access-controlled backup retention.

## Production configuration

Supply settings through the deployment platform's environment variables or
secret manager. Required settings are `APP_NAME`, `ENVIRONMENT`,
`DATABASE_URL`, `SECRET_KEY`, `JWT_ALGORITHM`,
`ACCESS_TOKEN_EXPIRE_MINUTES`, and `CORS_ORIGINS`. Set `ENVIRONMENT` to
`production`, use an independently generated secret with at least 32 bytes for
HS256, 48 bytes for HS384, or 64 bytes for HS512, configure the async SQLite URL,
and provide explicit HTTPS CORS origins. Production startup rejects missing
settings, weak/default keys, unsupported JWT algorithms, invalid database URLs,
and insecure CORS origins.

`ORDER_PENDING_TTL_MINUTES` defaults to 30 and accepts values from 1 through
10080. New orders record `expires_at` using this duration. A background task
checks for expired pending orders every
`ORDER_EXPIRATION_POLL_INTERVAL_SECONDS` (default 60, allowed 1-3600) and
processes at most `ORDER_EXPIRATION_BATCH_SIZE` orders per pass (default 100,
allowed 1-1000). The worker uses SQLite `BEGIN IMMEDIATE`, cancels only orders
that are still pending and expired, and restores stock in the same transaction.
It retries after logged operational failures and stops before the database
engine is disposed during application shutdown.

The cart phase stores product references and quantities only. Each response
calculates prices from current product data and never trusts client-supplied
amounts. Cart writes use a fresh SQLite transaction beginning with
`BEGIN IMMEDIATE` and revalidate the customer, active status, role, and tenant
membership within that transaction. Checkout deducts stock and creates
immutable purchase snapshots in the same transaction. Customer checkout is
`POST /orders/` with an `Idempotency-Key` header; customer history and detail
are `GET /orders/` and `GET /orders/{order_id}`. A retry returns the original
order, while reusing its key for a different non-empty cart payload returns
409. Order amounts are calculated from current server-side product prices and
stored as fixed-point integer units. New orders start as `pending` and remain
there until cancelled, expired, or advanced after verified payment. Payment
attempts, if provisioned by trusted internal code, are stored separately and do
not change order fulfillment state.

Customers retain owner-scoped order history and detail access. Tenant admins can
list and view orders within their own tenant using the same `GET /orders/` and
`GET /orders/{order_id}` routes. Only tenant admins can advance fulfillment with
`PATCH /orders/{order_id}/fulfillment` and a
`{"fulfillment_status":"confirmed"}` request body. Paid orders advance
sequentially from `pending` to `confirmed`, `processing`, and `completed`;
repeating the current state is safe. A successful, non-reconciliation payment
recorded before order expiry is required for every transition. Payment status
remains separate from fulfillment status, and customers cannot change
fulfillment.

Customers may cancel only their own pending orders with
`POST /orders/{order_id}/cancel`. A successful cancellation returns the
cancelled order with HTTP 200. Repeating the request for an already-cancelled
order also returns HTTP 200 and never restores stock again. Orders in other
states, including orders with a successful payment, return HTTP 409. An
expiration pass does not cancel an order whose payment succeeded before its
expiry. If the order has reached its expiry time before the
worker processes it, the request performs expiration cancellation and stock
restoration, then returns HTTP 409 with an expiration-specific message rather
than treating it as a customer cancellation. Orders not owned by the customer
or tenant return HTTP 404.

Stock restoration and cancellation metadata are committed atomically. Deleted
source products are skipped because their order-item provenance references are
set to `NULL`; surviving products are updated with tenant-scoped conditional
updates that reject stock or version overflow. A failed restoration rolls
back the cancellation and all restorations in the batch.

### Payment foundation

Migration `0006_payment_foundation` adds tenant-owned merchant-account
references, payment attempts, idempotent provider-event records, and refund
records. Payment amounts use the order's fixed-point integer units and currency;
tenant-scoped composite foreign keys prevent associating an order or merchant
account with another tenant's payment. Downgrading this migration is refused
while any payment, event, refund, or merchant-account data exists.

The provider interface and credential-resolver contract are provider-agnostic
only. There is no checkout endpoint, live payment integration, webhook route,
or endpoint that can mark a payment or refund successful. Customer payment
history and detail are available at `GET /payments/` and
`GET /payments/{payment_id}`; responses omit merchant references, provider
transaction references, and internal metadata. Tenant admins and other
customers cannot read a customer's payment records.

Each tenant must use its own approved merchant account. The platform-owned
merchant account is never used as a fallback to collect tenant orders.
Merchant-account references may be provisioned by trusted operations, but
credentials must be resolved from an approved secret manager and must never be
stored in SQLite. Raw card data and credentials are not stored. Redirects do
not confirm payment; only a future provider adapter's verified server-side
event or authenticated server-to-server verification may update payment
status. Duplicate event IDs are persisted once, and late success after an
order is cancelled or expired is recorded as requiring reconciliation without
reopening the order or reserving stock again. Network calls must occur outside
SQLite write transactions. Order cancellation and expiry mark unfinished
payment attempts expired and flag any already-succeeded payment for
reconciliation; they do not call a provider or initiate a refund. Only one
initiated, pending, or succeeded attempt may be started for an order at a time.
Any separately-existing later success is marked for reconciliation along with
the earlier success. Refund request amounts remain reserved against the payment
total even after a reported failure, so a delayed success result cannot permit
total refunds to exceed the captured amount.

JazzCash API integration remains pending official technical confirmation of
the merchant contract, signatures, sandbox, callback, and refund behavior.
No JazzCash endpoints or payload formats are assumed here. A marketplace or
submerchant model must not be used without written provider approval. Sandbox
provider work can begin only after that contract, tenant merchant onboarding,
supported methods, and credentials handling have been confirmed.

Order-item purchase snapshots are protected from updates, including after the
source product is deleted. The optional source-product reference may be nulled
by SQLite's `ON DELETE SET NULL`; because SQLite triggers cannot distinguish
that foreign-key action from a direct update that nulls both provenance
columns, source-product provenance is not immutable. Snapshot name, price,
currency, quantity, and line-total fields remain protected.

### Authentication and authorization

Passwords are hashed with the Argon2 implementation selected by `pwdlib`.
Tenant registration requires 12-1024 characters; password values are never
truncated. The OAuth2 password-form login endpoint is unchanged. Access tokens
are signed with the configured HMAC algorithm and require valid `sub` and `exp`
claims. Role and tenant claims remain present for compatibility, but are never
used for authorization; current roles, tenant membership, and account status
come from the database.

Existing product permissions are explicit: TenantAdmins can create, list, and
delete products in their own tenant; Customers can list products in their own
tenant; SuperAdmins can list products only when associated with a tenant and
can deactivate users. Product IDs remain tenant-scoped. Tenant registration
remains public for onboarding. A SuperAdmin account must be provisioned by an
operator; there is no public role-promotion endpoint.

`POST /auth/users/{user_id}/deactivate` is a new SuperAdmin-only endpoint.
Deactivation invalidates all of that user's access tokens on the next request
because each request reloads the account from the database. Tokens for active
accounts remain valid until their configured expiry; this is the revocation
strategy, and there is no separate per-token logout/revocation list.

Login and registration use configurable fixed-window limits:
`LOGIN_RATE_LIMIT_PER_WINDOW` (default 10) and
`LOGIN_RATE_LIMIT_WINDOW_SECONDS` (default 900), plus
`REGISTRATION_RATE_LIMIT_PER_WINDOW` (default 5) and
`REGISTRATION_RATE_LIMIT_WINDOW_SECONDS` (default 3600). Counts are persisted
in SQLite, shared by application workers using the same database, and keyed by
a keyed hash of the direct client address. Forwarded-IP headers are not trusted;
deployments behind a proxy must configure the ASGI server/proxy trust boundary
so the direct client address is trustworthy. Configure these limits for the
expected client population and deployment topology.

The only intentional request/response contract changes are the stronger
registration password bounds and a generic duplicate-registration error detail
to avoid revealing whether an email or tenant slug already exists. The
deactivation endpoint and rate-limit `429` responses are additive. Existing
tokens with valid subject and expiry claims remain acceptable during their
remaining lifetime; role and tenant claim tampering cannot change permissions.

### Product API

Product routes are tenant-scoped using the authenticated user's database
membership; request bodies cannot set `tenant_id`. TenantAdmins can create,
partially update, and delete products. TenantAdmins, Customers, and
tenant-associated SuperAdmins can list and retrieve products. Foreign-tenant
product IDs return `404`. The routes are `POST/GET /products/`,
`GET/PATCH/DELETE /products/{product_id}`.

Tenant currency is an ISO 4217 code. New tenants default to `PKR`; registration
may specify a supported `default_currency`. Products inherit that tenant's
default when `currency` is omitted; an explicit supported product currency is
accepted. Currency values are normalized to uppercase. No currency conversion
is performed. Product prices use `Decimal` validation and `NUMERIC(15, 4)` with
positive values, at most 15 total digits and 4 fractional digits. Responses
serialize `price` as a decimal string to preserve its decimal representation;
clients that previously expected a JSON number must parse this field as a
decimal string. Names are trimmed, required, and limited to 255 characters;
descriptions are optional, may be cleared with explicit `null` on PATCH, and
are limited to 5000 characters. PATCH leaves omitted fields unchanged; explicit
`null` is accepted only for `description`. Empty PATCH requests are rejected.

List ordering is stable by product ID. `skip` is limited to 0-1,000,000,
`limit` to 1-100; optional filters are `search` (1-100 nonblank characters),
`currency`, `min_price`, and `max_price`. Invalid filters and inverted price
bounds return `422`. PATCH requires `expected_version`, returned as `version`
on the product. Updates atomically advance the version; a stale version
returns `409` rather than silently overwriting another update. Product names
are not unique; duplicate names remain allowed for compatibility.

Migration `0004_product_currency_decimal` assigns `PKR` to tenants missing a
currency, assigns each existing product its tenant's default currency only if
the product currency is missing, and changes the price column from `Float` to
`NUMERIC(15, 4)` without currency conversion. Before altering the schema it
checks existing prices and text fields; it aborts without applying the product
schema changes if a price cannot fit the new precision/scale or a name or
description is outside the API limits. Review the existing data and take a
verified backup before applying migrations. Do not edit or bulk-round legacy
prices merely to force this migration through; resolve any preflight failure
with an approved data policy first. SQLite uses dynamic numeric storage, so
keep monetary values within the documented precision and do not treat this
SQLite-backed API as a substitute for validating a production database and
deployment topology.

For example, configure these values using the environment-variable or secret
injection feature provided by your deployment platform. Do not put secret
values in Dockerfiles, source control, build arguments, or logs. For local
development, use `.env.example` as a template and replace its placeholder with
a local-only key.

If a real secret may have been exposed, rotate it in the secret manager and
redeploy affected services. Ignoring or deleting a file in the latest working
tree does **not** remove it from Git history; if it was committed, follow the
hosting provider's guidance to purge repository history as well.

The `.dockerignore` protects Docker build contexts. This project does not define
a general-purpose archive/package workflow; create any other distributable from
an explicit source allowlist rather than archiving the working directory.
Git-generated source archives also exclude sensitive/local files through
`.gitattributes`; `.env.example` remains available as a safe template.

## Container build and deployment

The production image is built from the pinned official Python
`3.13.18-slim-bookworm` multi-architecture digest in `Dockerfile` and installs
the exact package versions from `requirements.lock`. The lock pins versions but
does not contain artifact hashes; see Local development for that remaining
artifact-integrity limitation. The image copies only the application,
migrations, Alembic configuration, and production lock, and runs as UID/GID
10001. Its root filesystem is read-only compatible, Linux capabilities are
not needed, and its readiness health check uses `/health/ready`. The configured
Uvicorn process uses one worker, graceful SIGTERM shutdown, and a 25-second
graceful-shutdown window.

Build and inspect the image from this directory:

```powershell
docker build --pull --tag saas-ecommerce-api:local .
docker image inspect saas-ecommerce-api:local
docker run --rm --entrypoint python saas-ecommerce-api:local -c "import pathlib; root=pathlib.Path('/app'); forbidden=('.env', '.pem', '.key', '.p12', '.pfx'); found=[str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and (p.name in forbidden or p.name.startswith('.env.'))]; assert not found, found"
```

The final check scans files copied under `/app`; the Dockerfile's explicit
`COPY` instructions and `.dockerignore` keep local `.env` files, keys, private
SQLite files and sidecars, tests, VCS metadata, virtual environments, and test
artifacts out of the image. Never pass secrets as build arguments or copy a
runtime env file into the build context.

### Local development with Compose

Compose is for local development only. It binds the API to loopback, uses a
named persistent SQLite volume, applies resource limits, runs the process
read-only as non-root, and uses the image readiness probe. Generate a local
development key, then build, migrate explicitly, and start the API:

```powershell
$env:DEV_SECRET_KEY = (python -c "import secrets; print(secrets.token_urlsafe(48))")
docker compose build api
docker compose run --rm --no-deps --entrypoint python api -m alembic upgrade head
docker compose up -d
docker compose ps
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

The data survives `docker compose down`; do not use `docker compose down -v`
unless intentionally deleting this development database. The application
rejects non-SQLite database URLs, so this repository must not use a PostgreSQL
Compose service as if it were compatible. A future PostgreSQL service would
require an explicitly scoped database and migration architecture change. For
the same reason, readiness checks the configured SQLite file rather than
PostgreSQL.

### Production deployment and recovery

The image is portable and does not encode deployment-specific environment
values. Supply `ENVIRONMENT=production`, `DATABASE_URL` pointing to a durable
SQLite file mounted at `/data`, a separately generated `SECRET_KEY`,
`JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, and explicit HTTPS
`CORS_ORIGINS` through the deployment platform's secret/configuration facility.
Do not use the development Compose file, `.env.example`, or its development
settings in production.

Terminate HTTPS at a maintained reverse proxy or load balancer. Restrict its
accepted `Host` values to the service's real DNS names, keep the application
port private and reachable only through that trusted proxy, and forward
`X-Forwarded-*` headers only from known proxy addresses. This app does not add
Host-header validation itself. Set `CORS_ORIGINS` to the exact browser origins
that need access; it is not a substitute for HTTPS, host restrictions, or
network access control.

Use one Uvicorn worker and one application host for this SQLite deployment;
do not place the database on a network filesystem or share a WAL database
across hosts. Set container CPU/memory limits based on load testing; the local
Compose values (1 CPU, 512 MiB) are examples, not production sizing. Align
reverse-proxy request deadlines with `REQUEST_TIMEOUT_SECONDS` and Uvicorn's
graceful shutdown. Allow at least 30 seconds for container stop so the process
can finish its configured graceful-shutdown interval and dispose of the engine.

Treat schema changes as an explicit, serialized deployment operation. Stop
application writers, take and verify a SQLite backup, then run `alembic
upgrade head` using the same immutable image, runtime configuration, and
persistent database mount intended for the new app. Start the new image only
after migration succeeds and `/health/ready` returns healthy. For example, on
a deployment host with a protected runtime configuration file outside the
repository and a pre-created persistent volume:

```sh
docker run --rm --read-only --user 10001:10001 \
  --env-file "$RUNTIME_ENV_FILE" \
  --mount type=volume,source=saas-ecommerce-data,target=/data \
  --entrypoint python "$IMAGE_REF" -m alembic upgrade head
```

Start the app from the same immutable `$IMAGE_REF`, mount the same volume, and
publish it only to the private proxy network (or loopback if the proxy runs on
the host). Configure equivalent read-only, non-root, dropped-capability,
no-new-privileges, resource-limit, and graceful-stop settings in the chosen
runtime. Do not copy `$RUNTIME_ENV_FILE` into the image or commit it.

For rollback, first stop writes and retain the prior image by immutable digest.
Roll back application code only if it is compatible with the migrated schema.
Do not automatically run Alembic downgrade or overwrite the live SQLite file.
If the schema is not backward-compatible, stop the service and restore the
verified pre-migration backup using SQLite's backup procedure; validate it
before resuming writes. Preserve the failed database and logs for diagnosis.

Update the Python base digest and locked packages regularly: review upstream
Python/Debian security updates, regenerate dependency locks through the
documented pip-tools process, run the full tests and migration checks, build a
new immutable image, and scan that image with the organization's approved
container scanner before promotion. Pin the promoted image by digest and keep
the previous known-good digest and database backup for recovery. No registry,
cloud credentials, public deployment, or live infrastructure changes are
configured by this repository.

## Operations and observability

`GET /health/live` is a lightweight process liveness probe. `GET /health/ready`
executes `SELECT 1` using the configured SQLite database and returns `503` with
`{"status":"not_ready"}` when the database is unavailable. Neither endpoint
returns configuration, credentials, or exception details.

Each HTTP response includes an `X-Request-ID`. A syntactically valid UUID
provided in that request header is retained; otherwise the application creates
one. The header is exposed to browser clients through CORS. Application logs
are newline-delimited JSON written to standard error. They include the request
ID, HTTP method, route template, status, duration, and safe exception type
where applicable. Request/response bodies, query strings, authorization
headers, tokens, passwords, database URLs, and exception messages are not
logged. Raw Uvicorn access logging is disabled so query strings are not emitted
by its default request-line logger. Validation failures retain status `422` and
return a generic detail rather than echoing potentially sensitive submitted
values; unexpected errors return a generic `500` response. Existing HTTP status
codes and static business error details are retained.

`REQUEST_TIMEOUT_SECONDS` configures the application request deadline (default
`30`, allowed range greater than zero through `300`). A timed-out request
returns `504` if its response has not started, and cancellation unwinds normal
async dependencies so database sessions can close. This is an application
deadline, not a substitute for reverse-proxy, load-balancer, or server
connection timeouts. Configure infrastructure deadlines consistently and do not
use this middleware for long-lived streaming requests.

Collect the JSON application logs with the deployment platform's standard log
collector. Alert on sustained `5xx` rates, repeated `504` timeouts, readiness
failures, and elevated request durations; group incidents by request ID when
investigating an individual request. Request status and duration are available
in the structured request log events for downstream aggregation. No external
monitoring provider, metrics endpoint, dashboard, or alerting integration is
configured by this project.

## Verification

From this directory, run the complete test suite:

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest -q --cov --cov-report=term-missing
ruff check app migrations tests
ruff format --check app migrations tests
mypy
pip-audit -r requirements-dev.lock
```

Recompile lock files after changing a dependency declaration:

```powershell
python -m pip install "pip-tools>=7.4,<8.0"
python -m piptools compile --resolver=backtracking --output-file requirements.lock requirements.txt
python -m piptools compile --resolver=backtracking --output-file requirements-dev.lock requirements-dev.in
```

Run a specific test module with:

```powershell
python -m pytest tests/test_config.py tests/test_api.py -q
```

API tests use FastAPI's `TestClient`, override the production database
dependency, and create a fresh SQLite database under pytest's temporary
directory for each test. Migration tests also use disposable temporary
databases. They do not run production migrations or connect to the configured
development database. CI supplies only a CI-only signing key and validates
migrations in a temporary directory; it has no production secrets or customer
data. Lint, format, type, audit, migration, and test failures all fail the CI
job, and the stable `Required CI gate` check fails if any Python matrix job
fails. Configure `Required CI gate` as a required status check in the hosting
repository's branch protection or ruleset to prevent merging/releasing a change
with failed checks; that hosting setting cannot be enforced by workflow files
alone. The suite currently measures 79.7% branch-aware coverage of `app`; the
initial coverage floor is 70% to leave room for staged coverage improvements
without weakening tests to hit a target. Increase the floor as meaningful
behavior tests are added.


## Operations and deployment

- [Deployment guide](./DEPLOYMENT.md)
- [SQLite backup and restore](./BACKUP_RESTORE.md)
- [Release validation boundary](../RELEASE_STATUS.md)

Review the deployment guide before exposing the API publicly. The Compose file is a local development baseline; it is not proof of a production deployment.
