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

**Arch Linux: alles automatisch** — `./scripts/setup_postgres.sh` installiert
PostgreSQL, initialisiert den Cluster, legt Rolle+Datenbank an, stellt `.env`
um, migriert und übernimmt auf Wunsch die SQLite-Daten (`--with-data` /
`--no-data` für nicht-interaktive Läufe). Idempotent, mehrfach ausführbar.

Manuell (andere Distributionen) — create the database and user (psql as a
superuser):

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

## 4b. Lokales HTTPS zum Testen im Heimnetz

Kamera-Scan, Fotoaufnahme und Passkeys (WebAuthn) funktionieren nur in einem
*Secure Context* — `http://<IP>:8080` reicht dafür nicht. Für Tests vom Handy
oder anderen Geräten im Netzwerk startet `run_https.py` die App mit TLS:

```bash
.venv/bin/python run_https.py          # https://0.0.0.0:8443, Auto-Reload
.venv/bin/python run_https.py 9443     # anderer Port
```

Das Skript erzeugt ein selbstsigniertes Zertifikat in `dev-certs/` (gitignored)
mit `localhost`, dem Rechnernamen, `<rechnername>.local` und der aktuellen
LAN-IP als SAN — und erneuert es automatisch, wenn sich die IP ändert oder es
abläuft. `ALLOWED_HOSTS` wird für diese Namen ergänzt; statische Dateien kommen
über WhiteNoise, Uploads über die DEBUG-Media-Route.

Auf jedem Gerät einmal die Browser-Warnung zum selbstsignierten Zertifikat
bestätigen. **Passkeys:** WebAuthn akzeptiert keine rohen IP-Adressen als
Domain — dafür `https://<rechnername>.local:8443` verwenden (mDNS, funktioniert
auf Android/iOS/Desktop); Kamera & Co. gehen auch über die IP.

### Ohne Zertifikatswarnung: mkcert (empfohlen)

Ist [mkcert](https://github.com/FiloSottile/mkcert) installiert, stellt
`run_https.py` das Zertifikat automatisch darüber aus — auf diesem Rechner
verschwindet die Browser-Warnung komplett:

```bash
sudo pacman -S mkcert   # Arch; sonst: brew install mkcert / choco install mkcert
mkcert -install         # lokale Root-CA in die Trust-Stores eintragen (einmalig)
.venv/bin/python run_https.py   # erkennt mkcert und stellt neu aus
```

Bei jeder Neuausstellung legt das Skript den **öffentlichen** Teil der Root-CA
als `dev-certs/mkcert-root-ca.pem` ab. Diese Datei auf andere Geräte übertragen
(KDE Connect, USB, Mail an sich selbst) und dort einmalig importieren, dann
gibt es auch dort keine Warnung:

* **Android:** Einstellungen → Sicherheit & Datenschutz → Weitere Einstellungen
  → Verschlüsselung & Anmeldedaten → Zertifikat installieren → **CA-Zertifikat**
  → Datei wählen (Pfad variiert je nach Hersteller; Warnhinweis bestätigen).
* **iOS/iPadOS:** Datei per AirDrop/Mail öffnen → Einstellungen → „Profil
  geladen“ installieren → danach unter Allgemein → Info →
  Zertifikatsvertrauenseinstellungen die CA **aktivieren**.
* **Windows/macOS/Linux-Clients:** Datei doppelklicken bzw. in den
  Zertifikatsspeicher „Vertrauenswürdige Stammzertifizierungsstellen“
  importieren.

Sicherheit: `rootCA.pem` ist unbedenklich weitergebbar. Der zugehörige
**private CA-Schlüssel** (`rootCA-key.pem` im Ordner von `mkcert -CAROOT`)
bleibt auf diesem Rechner und darf nie kopiert werden — wer ihn besitzt, kann
für jedes von den Geräten besuchte HTTPS-Ziel gültige Zertifikate fälschen.
Aus demselben Grund die Root-CA nur auf eigenen Geräten importieren.

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

## 7. Nach dem ersten Start (Web-UI)

Als Staff-Nutzer unter **Systemeinstellungen** (`/settings/`):

- **Impressum: Betreiber / Anschrift / Kontakt-E-Mail** ausfüllen — erscheint
  auf den öffentlichen Rechtsseiten `/impressum/` und `/datenschutz/`
  (DSGVO/DDG-Pflichtangaben).
- Optional **TMDb-API-Schlüssel** (themoviedb.org) und **RAWG-API-Schlüssel**
  (rawg.io) eintragen — schaltet die Film-/Serien- bzw. Videospielsuche der
  automatischen Befüllung frei. Bücher (DNB, Google Books, Open Library),
  Musik (MusicBrainz), Brettspiele (Wikidata) und generische EAN-Produkte
  (UPCitemdb) funktionieren ohne Schlüssel.

## Notes

- **Media files** (`/media/`) are user uploads (images, receipts). Back them up;
  serve them via the web server, not WhiteNoise.
- To rotate to a managed Postgres or change hosts, only `.env` changes.
- Re-run `collectstatic` after any static asset change when `STATIC_MANIFEST=True`.
