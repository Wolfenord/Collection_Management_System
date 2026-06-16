# Deployment Guide — Collection Management System

The app runs on SQLite in development and on PostgreSQL in production, switched
entirely via environment variables (no code changes). Static files are served by
WhiteNoise; the WSGI server is Gunicorn.

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

## 5. Verify the deployment

```bash
python manage.py check --deploy   # should report no issues with the prod env
python manage.py test             # full test suite
```

## Notes

- **Media files** (`/media/`) are user uploads (images, receipts). Back them up;
  serve them via the web server, not WhiteNoise.
- To rotate to a managed Postgres or change hosts, only `.env` changes.
- Re-run `collectstatic` after any static asset change when `STATIC_MANIFEST=True`.
