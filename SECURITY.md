# Sicherheitskonzept — Collection Management System

Stand: Juli 2026. Dieses Dokument beschreibt die umgesetzten Schutzmaßnahmen,
die DSGVO-relevanten Eigenschaften und die Checkliste für den Produktivbetrieb.

## Grundprinzip

Das CMS verwaltet private Sammlungsdaten. Nichts davon ist öffentlich:
jede Seite außer Login/Registrierung/Passwort-Reset erfordert eine Anmeldung,
Zugriff auf Sammlungen ist zusätzlich pro Benutzer autorisiert
(Eigentümer / geteilte Freigaben mit `view`/`edit`).

## Authentifizierung

- **Registrierungs-Whitelist**: Neue Konten sind inaktiv (`is_active=False`),
  bis ein Administrator sie freigibt. Die Sperre greift im Auth-Backend selbst.
  Die Registrierung kann per Laufzeit-Einstellung komplett geschlossen werden.
- **Passkeys (WebAuthn/FIDO2)** als passwortlose Anmelde-Option
  (`accounts/passkeys.py`, `static/js/passkeys.js`):
  - Registrierung auf der Profilseite, Anmeldung per Button auf der Login-Seite.
  - Es wird nur der **öffentliche** Schlüssel gespeichert; der private bleibt
    auf dem Gerät (Handy, Sicherheitsschlüssel, Passwort-Manager).
  - Discoverable Credentials (kein Benutzername nötig, keiner wird geleakt),
    User-Verification (PIN/Biometrie) ist **erforderlich**, Challenge ist
    Einmal-Wert in der Session, Origin + RP-ID werden serverseitig geprüft,
    der Signaturzähler erkennt geklonte Authenticatoren.
  - Die Freigabe-Whitelist gilt auch für Passkey-Logins.
  - Browser erlauben WebAuthn nur in sicheren Kontexten → HTTPS nötig
    (localhost ausgenommen). Für Tests im Heimnetz: `run_https.py`
    (siehe DEPLOYMENT.md, Abschnitt 4b); als Adresse den Rechnernamen bzw.
    `<rechnername>.local` verwenden — WebAuthn akzeptiert keine IP-Adressen.
- **Brute-Force-Schutz** (`accounts/throttling.py`, Cache-basiert):
  - Login: 5 Fehlversuche pro Konto bzw. 20 pro IP → 15 Minuten Sperre;
    auch das richtige Passwort wird während der Sperre abgelehnt.
    Erfolgreiche Anmeldung setzt die Zähler zurück.
  - Registrierung: max. 10 POSTs/Stunde/IP. Passwort-Reset: max. 5
    Mails/Stunde/IP. Passkey-Login: max. 20 Versuche/15 min/IP. Antwort: 429.
  - Externe Datenbanksuche (Lookup/Suche): max. 30 Anfragen/Minute pro
    Benutzer — der Server lässt sich nicht als Anfrage-Kanone gegen
    DNB/Google/Open Library missbrauchen.
  - Schwere Download-Endpunkte: DSGVO-Datenexport (JSON) und
    Sammlungs-Sicherung (ZIP) je max. 10/Stunde pro Benutzer.
  - Sicherheitsereignisse (Lockouts, Rate-Limit-Treffer, fehlgeschlagene
    Passkey-Anmeldungen) werden im Logger ``cms.security`` protokolliert
    (stderr; in Produktion auf Datei/Syslog zeigen lassen).
  - Hinweis: Mit dem Standard-LocMemCache gelten die Limits pro Prozess.
    Für strikte globale Limits bei mehreren Gunicorn-Workern `CACHES` auf
    Redis/Memcached zeigen lassen.
- **Argon2** (memory-hard, OWASP-Empfehlung) ist der primäre Passwort-Hasher;
  bestehende PBKDF2-Hashes werden beim nächsten Login transparent migriert.
- Passwort-Validierung (Länge, Ähnlichkeit, Common-Passwords, numerisch) aktiv;
  Anmeldefehler sind generisch (kein User-Enumeration über die Fehlermeldung,
  auch nicht beim Passkey-Login oder Passwort-Reset).
- **API-Tokens werden nur als SHA-256-Hash gespeichert** — der Klartext-Schlüssel
  existiert genau einmal (Anzeige nach dem Erstellen); ein DB-Diebstahl liefert
  keine verwendbaren Tokens.

## Transport & Header

- **CSP** (Django-6-nativ, erzwungen): `default-src 'self'`; Skripte nur von
  eigener Origin + per-Request-Nonce für die wenigen Inline-Blöcke; keine
  Inline-Event-Handler (durch `data-confirm`-Muster ersetzt); Bilder von
  eigener Origin + den Cover-Hosts der Mediendatenbanken (DNB, Google Books,
  Open Library, Cover Art Archive/archive.org, TMDb, RAWG, Wikimedia);
  `frame-ancestors 'none'`, `object-src 'none'`, `base-uri`/`form-action 'self'`.
- **X-Frame-Options: DENY**, **nosniff**, **Referrer-Policy: same-origin**.
- **Permissions-Policy**: nur Kamera (Barcode-Scan/Fotoaufnahme) für die eigene
  Origin; Mikrofon, Standort, Payment, USB aus.
- Session-/CSRF-Cookies: `HttpOnly` (Session), `SameSite=Lax`; Session-Ablauf
  standardmäßig 14 Tage (`SESSION_COOKIE_AGE` konfigurierbar).
- HSTS/`Secure`-Cookies/SSL-Redirect sind env-gesteuert (siehe Checkliste) —
  lokal aus, in Produktion an.

## Crawler & Indexierung

Dreifach abgesichert, gilt auch für Nicht-HTML-Antworten (Exporte, QR-Codes):

1. `/robots.txt` → `Disallow: /` für alle User-Agents.
2. HTTP-Header `X-Robots-Tag: noindex, nofollow, noarchive` auf jeder Antwort.
3. `<meta name="robots" content="noindex, nofollow">` in jeder Seite.

Alles Inhaltliche liegt ohnehin hinter dem Login; aggressive Crawler laufen
zusätzlich in die Rate-Limits der öffentlichen Endpunkte.

## DSGVO / deutsche Anforderungen

- **Keine Drittanbieter-Requests**: Bootstrap, Icons (inkl. Fonts), Intro.js,
  html5-qrcode, SortableJS und Chart.js werden lokal aus `static/vendor/`
  ausgeliefert — keine Besucher-IP erreicht ein CDN (vgl. Google-Fonts-Urteil
  LG München I, 3 O 17493/20). Kein Analytics, kein Tracking, keine Cookies
  außer Session/CSRF/Sprache (alle technisch notwendig, kein Cookie-Banner
  erforderlich).
- Ausnahme, bewusst und nutzerinitiiert: Bei der externen Datenbanksuche
  (Bücher: DNB/Google Books/Open Library; Musik: MusicBrainz; Filme: TMDb;
  Videospiele: RAWG; Brettspiele: Wikidata; generische EAN: UPCitemdb)
  stellt der **Server** die Anfrage — übermittelt wird nur Suchbegriff/Code,
  keine Nutzer-IP. Cover-Vorschaubilder lädt der Browser direkt vom Anbieter
  (per CSP auf die Cover-Hosts begrenzt). Gespeicherte Cover lädt der Server
  (Host-Whitelist gegen SSRF, **Redirects werden gegen dieselbe Whitelist
  geprüft**, Content-Type- und Größenlimit) und liefert sie selbst aus.
- **Preissuche = reine Link-Sammlung**: Der Server kontaktiert keine
  Kaufplattform und scrapt nichts. Erst der Klick des Nutzers öffnet die
  Plattform (`rel="noopener noreferrer nofollow"`, neuer Tab); die Links
  enthalten keine Affiliate-/Tracking-Parameter.
- **Datenminimierung**: Konto = Benutzername, E-Mail, optionaler Anzeigename.
  Passwörter als Argon2-Hash, API-Tokens nur einmal sichtbar, Passkeys nur als
  öffentlicher Schlüssel.
- **Betroffenenrechte als Self-Service** (eingebaut):
  - `/datenschutz/` + `/impressum/` — öffentliche Rechtsseiten; die
    Betreiber-Angaben pflegt der Admin in den Systemeinstellungen.
  - `/accounts/export.json` — vollständiger Datenexport (Art. 15/20 DSGVO)
    als maschinenlesbares JSON.
  - `/accounts/delete/` — Kontolöschung (Art. 17) mit Re-Authentifizierung
    (Passwort bzw. Benutzername-Bestätigung bei Passkey-only-Konten); löscht
    auch alle Upload-Dateien von der Platte. Der letzte aktive Superuser kann
    sich nicht selbst aussperren; der Endpunkt ist rate-limitiert.
- **Löschbarkeit**: zusätzlich können Konten im Admin gelöscht werden
  (Sammlungen, Gegenstände, Dateien, Tokens, Passkeys hängen per `CASCADE`
  daran). Papierkorb-Inhalte werden nach Ablauffrist automatisch entfernt.
- Für den öffentlichen Betrieb zusätzlich: Impressums-Felder in den
  Systemeinstellungen ausfüllen, ggf. AVV mit dem Hoster, Backup-Konzept.

## Upload- & Eingabesicherheit

- Uploads: Größen-Limit und Erweiterungs-Whitelist (Laufzeit-Einstellungen),
  Auslieferung mit `nosniff`; Cover-Downloads nur von der Host-Whitelist,
  Größenlimit, Content-Type-Prüfung (SSRF-/Malware-Schutz).
- ORM/Templates verhindern SQLi/XSS; CSRF-Schutz auf allen Formularen und
  JSON-POSTs (Header `X-CSRFToken`); Redirects nach Login werden mit
  `url_has_allowed_host_and_scheme` geprüft (kein Open Redirect).
- Excel-Import parst nur Zellwerte (openpyxl, keine Makros/Formeln-Ausführung).
- ZIP-Wiederherstellung (`/collections/restore/`): Archiv wird nie auf die
  Platte entpackt (kein Zip-Slip); Mitglieder-Anzahl, Einzel- und
  Gesamtgröße sind begrenzt (Zip-Bomben-Schutz); jedes Feld und jeder Wert
  wird gegen das Schema validiert; alles läuft in einer Transaktion;
  Freigaben werden bewusst nicht wiederhergestellt. Max. 5 Versuche/Stunde.

## Checkliste Produktivbetrieb (`.env` / `config.ini`)

| Einstellung | Produktionswert |
|---|---|
| `DEBUG` | `False` (Pflicht) |
| `SECRET_KEY` | langer Zufallswert (Pflicht, nie der Code-Default) |
| `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` | eigene Domain |
| `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | `True` |
| `SECURE_HSTS_SECONDS` | z. B. `31536000` (+ Subdomains/Preload nach Bedarf) |
| `USE_PROXY_SSL_HEADER` | `True` hinter TLS-terminierendem Proxy |
| `DB_ENGINE` | `postgres` |
| `EMAIL_BACKEND` + `EMAIL_*` | SMTP für Reset-/Freigabe-Mails |
| `STATIC_MANIFEST` | `True` (gehashte, komprimierte Assets via WhiteNoise) |
| `SESSION_COOKIE_AGE` | nach Bedarf kürzen (Sekunden) |

Zusätzlich: Reverse-Proxy so konfigurieren, dass `REMOTE_ADDR` die echte
Client-IP ist (die Rate-Limits binden daran; `X-Forwarded-For` wird bewusst
nicht blind vertraut), regelmäßige Updates (`pip install -U -r
requirements.txt`), Backups verschlüsselt ablegen.
