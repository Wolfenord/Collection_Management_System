# Deployment Guide — Collection Management System

The app runs on SQLite in development and on PostgreSQL in production, switched
entirely via configuration (no code changes). Static files are served by
WhiteNoise; the WSGI server is Gunicorn.

## Configuration layers

There are two kinds of configuration, resolved in this order:

1. **Deployment settings** (database, e-mail, security flags — fixed at process
   start): environment variables / `.env` **>** `config.ini` **>** code default.
   Use whichever suits your setup — both carry the same keys
   (`DB_ENGINE=postgres` ≙ `[database] db_engine = postgres`). Copy
   `config.example.ini` to `config.ini` for the INI variant, or
   `.env.production.example` to `.env` for the env variant. The INI path can be
   overridden with `CMS_CONFIG_FILE=/etc/cms/config.ini`.
2. **Runtime settings** (maintenance mode, JSON API on/off — see `API.md` —
   page size, loan period, registration open/closed and approval policy,
   upload limits, default currency, lookup timeouts): stored in the
   **database** and editable
   while the app is running — as a staff user via *Systemeinstellungen* in the
   user menu (or in the Django admin under *Systemeinstellungen*). Changes take
   effect immediately, no restart. Defaults can be pre-seeded in the
   `[app-defaults]` section of `config.ini`; a value saved in the UI wins.

## 1. PostgreSQL

Create the database and user (psql as a superuser):

```sql
CREATE DATABASE cms;
CREATE USER cms WITH PASSWORD 'a-strong-password';
GRANT ALL PRIVILEGES ON DATABASE cms TO cms;
ALTER DATABASE cms OWNER TO cms;
```

The `psycopg` driver is already in `requirements.txt`.

## 2. Environment

Copy the production template and fill it in:

```bash
cp .env.production.example .env
# edit .env: SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, DB_*, ...
python -c "import secrets; print(secrets.token_urlsafe(64))"   # SECRET_KEY
```

Key flags (all default to safe-for-dev values, enable them for production):
`DEBUG=False`, `DB_ENGINE=postgres`, `STATIC_MANIFEST=True`,
`SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`,
`SECURE_HSTS_SECONDS`, `USE_PROXY_SSL_HEADER` (if behind a TLS proxy).

## 3. Install, migrate, collect static

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --no-input
python manage.py createsuperuser
```

## 4. Run

```bash
gunicorn cms.wsgi:application --bind 0.0.0.0:8000 --workers 3
```

(or use the included `Procfile`). Put nginx/Caddy in front for TLS termination
and to serve `/media/` (user uploads). WhiteNoise serves `/static/` from the app.

Example nginx location for uploads:

```nginx
location /media/ { alias /path/to/app/media/; }
```

## 5. Periodic jobs (cron)

Two management commands are meant to run on a schedule; both are no-ops unless
the corresponding runtime settings are enabled/configured in the web UI:

```cron
# daily at 07:00: e-mail owners about overdue loans (loan_reminders_enabled)
0 7 * * *  cd /path/to/app && .venv/bin/python manage.py send_loan_reminders
# daily at 03:00: permanently delete trashed items past trash_retention_days
0 3 * * *  cd /path/to/app && .venv/bin/python manage.py purge_trash
```

## 6. Verify the deployment

```bash
python manage.py check --deploy   # should report no issues with the prod env
python manage.py test             # full test suite
```

## Notes

- **Media files** (`/media/`) are user uploads (images, receipts). Back them up;
  serve them via the web server, not WhiteNoise.
- To rotate to a managed Postgres or change hosts, only `.env` changes.
- Re-run `collectstatic` after any static asset change when `STATIC_MANIFEST=True`.
