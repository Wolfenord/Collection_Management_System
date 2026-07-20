#!/usr/bin/env python
"""Local HTTPS server for feature testing in the home network.

Browsers only expose camera capture, barcode scanning and passkeys (WebAuthn)
in a *secure context* — plain ``http://192.168.x.x:8080`` does not qualify.
This script

  1. generates a self-signed certificate (kept in ``dev-certs/``, ignored by
     git) whose SANs cover localhost, this machine's hostname, ``<hostname>
     .local`` (mDNS) and the current LAN IP — and regenerates it whenever the
     IP changes or the certificate expires,
  2. extends ALLOWED_HOSTS with those names, and
  3. starts gunicorn with TLS on https://0.0.0.0:8443 (auto-reload enabled;
     static files come from WhiteNoise, media via the DEBUG URL route).

Usage:  .venv/bin/python run_https.py [port]

Then open  https://<LAN-IP>:8443  from any device in the network and accept
the self-signed-certificate warning once per device.

Passkey note: WebAuthn refuses raw IP addresses as relying-party ID even over
HTTPS. To test passkeys from another device, use the mDNS name instead:
https://<hostname>.local:8443 (works out of the box on Android/iOS/most
desktops). Everything else (camera, PWA, …) works via the IP too.

Trusted certificates: when mkcert is installed (``sudo pacman -S mkcert`` +
``mkcert -install`` once), this script issues the certificate through mkcert
instead of self-signing — no browser warning on this machine anymore. The
mkcert root CA (``dev-certs/mkcert-root-ca.pem``, public part only) can be
imported on phones/other devices to silence the warning there as well.
"""

import datetime
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CERT_DIR = BASE_DIR / 'dev-certs'
CERT_FILE = CERT_DIR / 'dev-cert.pem'
KEY_FILE = CERT_DIR / 'dev-key.pem'
CERT_DAYS = 365


def lan_ip() -> str | None:
    """The interface IP used for outgoing traffic (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 80))  # TEST-NET, never actually reached
            return sock.getsockname()[0]
    except OSError:
        return None


def wanted_names() -> tuple[list[str], list[str]]:
    """(dns_names, ips) the certificate must cover right now."""
    hostname = socket.gethostname().split('.')[0].lower()
    dns = ['localhost', hostname, f'{hostname}.local']
    ips = ['127.0.0.1']
    if ip := lan_ip():
        ips.append(ip)
    return dns, ips


def mkcert_ready() -> str | None:
    """Path of a usable mkcert binary whose root CA exists (``mkcert -install``
    run at least once), else None."""
    binary = shutil.which('mkcert')
    if not binary:
        return None
    try:
        caroot = subprocess.run([binary, '-CAROOT'], capture_output=True,
                                text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return binary if (Path(caroot) / 'rootCA.pem').is_file() else None


def cert_is_valid(dns: list[str], ips: list[str], *, mkcert_available: bool) -> bool:
    """True when the existing certificate covers all names and is not expiring."""
    if not (CERT_FILE.is_file() and KEY_FILE.is_file()):
        return False
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
    # Prefer a trusted mkcert certificate over an older self-signed one.
    issuer_cn = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    if mkcert_available and issuer_cn and issuer_cn[0].value == 'CMS Dev':
        return False
    if cert.not_valid_after_utc < datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    have_dns = set(san.get_values_for_type(x509.DNSName))
    have_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    return set(dns) <= have_dns and set(ips) <= have_ips


def create_cert_mkcert(binary: str, dns: list[str], ips: list[str]) -> None:
    """Issue the certificate via mkcert (trusted by this machine's stores) and
    put a copy of the public root CA next to it for import on other devices."""
    CERT_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [binary, '-cert-file', str(CERT_FILE), '-key-file', str(KEY_FILE)]
        + dns + ips,
        check=True,
    )
    KEY_FILE.chmod(0o600)
    caroot = subprocess.run([binary, '-CAROOT'], capture_output=True,
                            text=True, check=True).stdout.strip()
    shutil.copyfile(Path(caroot) / 'rootCA.pem', CERT_DIR / 'mkcert-root-ca.pem')


def create_cert(dns: list[str], ips: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'CMS Dev')])
    san = x509.SubjectAlternativeName(
        [x509.DNSName(name) for name in dns]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    CERT_DIR.mkdir(exist_ok=True)
    KEY_FILE.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    KEY_FILE.chmod(0o600)
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else '8443'
    dns, ips = wanted_names()

    mkcert = mkcert_ready()
    if cert_is_valid(dns, ips, mkcert_available=bool(mkcert)):
        print(f'Zertifikat ok: {CERT_FILE}')
    elif mkcert:
        create_cert_mkcert(mkcert, dns, ips)
        print(f'Vertrauenswürdiges Zertifikat über mkcert erstellt: {CERT_FILE}')
        print(f'Root-CA für andere Geräte: {CERT_DIR / "mkcert-root-ca.pem"} '
              '(auf dem Gerät als CA-Zertifikat importieren)')
    else:
        create_cert(dns, ips)
        print(f'Neues selbstsigniertes Zertifikat erstellt: {CERT_FILE}')
        print('Tipp: mkcert installieren (sudo pacman -S mkcert && mkcert '
              '-install) und dieses Skript neu starten — dann entfällt die '
              'Browser-Warnung.')

    # Make every certificate name a valid Host header (env beats config.ini,
    # so merge instead of clobbering a user-provided list).
    sys.path.insert(0, str(BASE_DIR))
    from cms import conf
    configured = [h.strip() for h in conf.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]
    merged = list(dict.fromkeys(configured + dns + ips))
    os.environ['ALLOWED_HOSTS'] = ','.join(merged)

    print('Erreichbar unter:')
    for name in dns + ips:
        marker = '  (Passkeys: ja)' if name not in ips else ''
        print(f'  https://{name}:{port}/{marker}')
    print('Hinweis: Die Browser-Warnung zum selbstsignierten Zertifikat einmal '
          'pro Gerät bestätigen.')

    os.execv(sys.executable, [
        sys.executable, '-m', 'gunicorn', 'cms.wsgi:application',
        '--bind', f'0.0.0.0:{port}',
        '--certfile', str(CERT_FILE),
        '--keyfile', str(KEY_FILE),
        '--reload',
        '--access-logfile', '-',
    ])


if __name__ == '__main__':
    main()
