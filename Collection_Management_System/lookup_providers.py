"""Pluggable lookup providers for auto-filling item fields from official
external databases.

Why this exists
---------------
The collection schema is fully dynamic (every column is a ``FieldDefinition``),
so the auto-fill must be dynamic too: a provider never knows about a specific
collection's columns. Instead every provider speaks a *shared attribute
vocabulary* (see :data:`ATTRIBUTES`) — e.g. ``title``, ``authors``, ``year`` …
Each ``FieldDefinition`` can be mapped to one of these attributes via
``config['lookup_attribute']``. The lookup view then translates the provider's
result into ``{field_key: value}`` for exactly the fields the user mapped.

The sources deliberately cover more than books: music (MusicBrainz), films &
series (TMDb), video games (RAWG), board games (BoardGameGeek) and generic
EAN/UPC products (UPCitemdb) sit next to the book databases (DNB, Google
Books, Open Library). ``Collection.lookup_provider`` stores the collection's
*media kind* (see :data:`MEDIA_KINDS`) and selects which sources are queried.

Adding a new source = registering one more :class:`LookupProvider`; no model,
view or template change required.

Network access uses only the standard library (``urllib``) so there is no extra
dependency. ``_http_get_json`` is a module-level function on purpose: tests
monkeypatch it to return canned payloads without hitting the network.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from xml.etree import ElementTree

from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared attribute vocabulary
# ---------------------------------------------------------------------------
# (key, human label). A FieldDefinition maps to ONE of these keys; every
# provider emits values keyed by these. This is what keeps the mapping
# provider-independent and the whole feature dynamic.
ATTRIBUTES: list[tuple[str, str]] = [
    ('title', _('Titel')),
    ('authors', _('Autor(en)')),
    ('director', _('Regie')),
    ('artist', _('Interpret / Künstler')),
    ('publisher', _('Verlag / Label')),
    ('published', _('Erscheinungsdatum')),
    ('year', _('Erscheinungsjahr')),
    ('pages', _('Seitenzahl')),
    ('runtime', _('Laufzeit / Spieldauer (Minuten)')),
    ('players', _('Spieleranzahl')),
    ('platform', _('Plattform / System')),
    ('format', _('Medium / Format')),
    ('brand', _('Marke / Hersteller')),
    ('description', _('Beschreibung')),
    ('categories', _('Kategorien / Genre')),
    ('language', _('Sprache')),
    ('isbn', _('ISBN')),
    ('ean', _('Barcode / EAN')),
    ('cover_url', _('Cover-Bild (URL)')),
]
ATTRIBUTE_LABELS = dict(ATTRIBUTES)
VALID_ATTRIBUTES = {key for key, _ in ATTRIBUTES}

# Attributes a scanned/typed code can arrive in: the field mapped to one of
# these doubles as the scan/lookup input of the item form.
QUERY_ATTRIBUTES = ('isbn', 'ean')

# Attributes whose field values make sensible free-text search input. Form
# fields mapped to one of these get a "search the databases" suggestion button.
SEARCHABLE_ATTRIBUTES = ('title', 'authors', 'artist', 'publisher', 'categories')

# ---------------------------------------------------------------------------
# Media kinds
# ---------------------------------------------------------------------------
# A collection can declare what it mainly holds; the lookup then only queries
# sources that make sense for that kind (stored in ``Collection.lookup_provider``,
# set automatically by the presets and changeable when editing the collection).
MEDIA_KINDS: list[tuple[str, str]] = [
    ('', _('Gemischt / Alles')),
    ('books', _('Bücher')),
    ('movies', _('Filme & Serien')),
    ('music', _('Musik / Tonträger')),
    ('games', _('Videospiele')),
    ('boardgames', _('Brett- & Gesellschaftsspiele')),
]
VALID_MEDIA_KINDS = {key for key, _ in MEDIA_KINDS}

_USER_AGENT = 'CMS-Collection-Manager/1.0 (self-hosted collection manager)'

# Per-thread override for the request timeout: the parallel search resolves
# the runtime setting ONCE in the request thread and hands it to its worker
# threads, so the workers never touch the database/cache themselves.
_thread_timeout = threading.local()


def _http_timeout() -> int:
    """Request timeout in seconds — runtime setting ``lookup_timeout``.

    Resolved per call (deferred import: this module must stay importable
    without Django apps being ready) so staff can tune it without a restart.
    """
    override = getattr(_thread_timeout, 'value', None)
    if override is not None:
        return override
    from .runtime_settings import get_setting
    return get_setting('lookup_timeout')


def _http_get_json(url: str) -> dict | list | None:
    """Fetch a URL and parse the JSON body. Returns ``None`` on any failure.

    Kept tiny and dependency-free; monkeypatched in tests.
    """
    request = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_http_timeout()) as response:
            charset = response.headers.get_content_charset() or 'utf-8'
            return json.loads(response.read().decode(charset))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning('External JSON fetch failed (%s): %s', url.split('?', 1)[0], exc)
        return None


def _http_get_text(url: str) -> str | None:
    """Fetch a URL and return the body as text (e.g. XML). ``None`` on failure.

    Kept tiny and dependency-free; monkeypatched in tests.
    """
    request = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_http_timeout()) as response:
            charset = response.headers.get_content_charset() or 'utf-8'
            return response.read().decode(charset, errors='replace')
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning('External text fetch failed (%s): %s', url.split('?', 1)[0], exc)
        return None


# Cover downloads are server-side fetches of client-supplied URLs, so they are
# restricted to the hosts our own providers emit (SSRF guard). Keep in sync
# with the CSP ``img-src`` list in cms/settings.py (browser-side previews).
COVER_HOSTS = {
    'covers.openlibrary.org',
    'books.google.com', 'books.googleusercontent.com',
    'portal.dnb.de',
    'coverartarchive.org',      # MusicBrainz cover art …
    'archive.org',              # … which redirects to archive.org mirrors
    'image.tmdb.org',           # TMDb posters
    'media.rawg.io',            # RAWG game covers
    'commons.wikimedia.org',    # Wikidata images (Special:FilePath) …
    'upload.wikimedia.org',     # … which redirect to the upload servers
}
# Mirror hosts that only ever appear as redirect *targets* (e.g.
# ia800505.us.archive.org for Cover Art Archive images).
_COVER_HOST_SUFFIXES = ('.archive.org',)
_MAX_COVER_BYTES = 5 * 1024 * 1024


def cover_url_allowed(url: str) -> bool:
    """True when ``url`` points at one of the trusted cover hosts (https/http)."""
    parsed = urllib.parse.urlparse(url or '')
    host = parsed.hostname or ''
    return parsed.scheme in ('http', 'https') and (
        host in COVER_HOSTS or host.endswith(_COVER_HOST_SUFFIXES)
    )


class _CoverRedirectGuard(urllib.request.HTTPRedirectHandler):
    """Refuse redirects that leave the cover-host allowlist.

    ``urlopen`` follows redirects transparently — without this, a whitelisted
    host could bounce the server to an arbitrary (e.g. internal) address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not cover_url_allowed(newurl):
            raise urllib.error.URLError(f'redirect outside cover allowlist: {newurl}')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get_bytes(url: str) -> tuple[bytes, str] | None:
    """Fetch a cover URL and return ``(body, content_type)``. ``None`` on failure.

    Redirects are re-validated against the cover-host allowlist.
    Kept tiny and dependency-free; monkeypatched in tests.
    """
    request = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    opener = urllib.request.build_opener(_CoverRedirectGuard)
    try:
        with opener.open(request, timeout=_http_timeout()) as response:
            content_type = (response.headers.get_content_type() or '').lower()
            return response.read(_MAX_COVER_BYTES + 1), content_type
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def fetch_cover(url: str) -> tuple[bytes, str] | None:
    """Download a cover image from a whitelisted provider host.

    Returns ``(bytes, extension)`` or ``None`` (bad host/scheme, non-image
    response, too large, network error).
    """
    if not cover_url_allowed(url or ''):
        return None
    result = _http_get_bytes(url)
    if not result:
        return None
    body, content_type = result
    if not content_type.startswith('image/') or len(body) > _MAX_COVER_BYTES:
        return None
    extension = {'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp'}.get(content_type, 'jpg')
    return body, extension


def _digits(value: str | None) -> str:
    """Strip ISBNs to bare digits/X so 978-3-… and 9783… look the same."""
    return re.sub(r'[^0-9Xx]', '', value or '')


def _year_from(value: str | None) -> str | None:
    """Pull a 4-digit year out of a free-form date like '2008' or '2008-05'."""
    match = re.search(r'(\d{4})', value or '')
    return match.group(1) if match else None


def _clean(data: dict) -> dict:
    """Drop empty/None values so they never overwrite user input on the form."""
    return {k: v for k, v in data.items() if v not in (None, '', [], {})}


@dataclass(frozen=True)
class LookupProvider:
    """One external data source.

    ``fetch(query)`` looks a scanned/typed code (ISBN/EAN) up and returns a
    ``{attribute_key: value}`` dict using the shared vocabulary, or ``{}`` when
    nothing was found — ``None`` when the source has no code lookup at all.
    ``query_attribute`` is the attribute whose field doubles as the search
    input (e.g. the ISBN field).
    """

    key: str
    label: str
    description: str
    fetch: Callable[[str], dict] | None = None
    query_attribute: str = 'isbn'
    # Short display name for combined listings ("DNB, Google Books, …").
    short: str = ''
    # Media kinds this source makes sense for (see MEDIA_KINDS keys).
    kinds: tuple[str, ...] = ('books',)
    # Attributes this provider can actually deliver (subset of ATTRIBUTES keys).
    provides: tuple[str, ...] = field(default_factory=tuple)
    # Optional free-text search (title/author/keywords) returning up to a
    # handful of candidate records in the same attribute vocabulary.
    search: Callable[[str], list[dict]] | None = None
    # Optional runtime availability check (e.g. "API key configured?").
    available: Callable[[], bool] | None = None

    @property
    def short_label(self) -> str:
        return self.short or str(self.label)

    def is_available(self) -> bool:
        return self.available() if self.available else True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_PROVIDERS: dict[str, LookupProvider] = {}


def register(provider: LookupProvider) -> LookupProvider:
    _PROVIDERS[provider.key] = provider
    return provider


def get_provider(key: str | None) -> LookupProvider | None:
    if key == 'auto':  # combined provider over all available sources
        return auto_provider()
    return _PROVIDERS.get(key or '')


def all_providers() -> list[LookupProvider]:
    return list(_PROVIDERS.values())


# ---------------------------------------------------------------------------
# Pre-configured providers (books, ISBN, no API key required)
# ---------------------------------------------------------------------------

def _fetch_openlibrary(query: str) -> dict:
    isbn = _digits(query)
    if not isbn:
        return {}
    url = (
        'https://openlibrary.org/api/books?'
        + urllib.parse.urlencode({
            'bibkeys': f'ISBN:{isbn}',
            'format': 'json',
            'jscmd': 'data',
        })
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return {}
    book = payload.get(f'ISBN:{isbn}')
    if not isinstance(book, dict):
        return {}

    cover = book.get('cover') or {}
    identifiers = book.get('identifiers') or {}
    isbn13 = (identifiers.get('isbn_13') or [None])[0]
    return _clean({
        'title': book.get('title'),
        'authors': ', '.join(a.get('name', '') for a in book.get('authors', []) if a.get('name')),
        'publisher': ', '.join(p.get('name', '') for p in book.get('publishers', []) if p.get('name')),
        'published': book.get('publish_date'),
        'year': _year_from(book.get('publish_date')),
        'pages': book.get('number_of_pages'),
        'categories': ', '.join(s.get('name', '') for s in (book.get('subjects') or [])[:5] if s.get('name')),
        'isbn': isbn13 or isbn,
        'cover_url': cover.get('large') or cover.get('medium') or cover.get('small'),
    })


def _parse_google_volume(info: dict, fallback_isbn: str = '') -> dict:
    isbn_out = fallback_isbn
    for ident in info.get('industryIdentifiers', []):
        if ident.get('type') == 'ISBN_13':
            isbn_out = ident.get('identifier') or isbn_out
            break

    images = info.get('imageLinks') or {}
    return _clean({
        'title': info.get('title'),
        'authors': ', '.join(info.get('authors', [])),
        'publisher': info.get('publisher'),
        'published': info.get('publishedDate'),
        'year': _year_from(info.get('publishedDate')),
        'pages': info.get('pageCount'),
        'description': info.get('description'),
        'categories': ', '.join(info.get('categories', [])),
        'language': info.get('language'),
        'isbn': isbn_out,
        'cover_url': images.get('thumbnail') or images.get('smallThumbnail'),
    })


def _fetch_google_books(query: str) -> dict:
    isbn = _digits(query)
    if not isbn:
        return {}
    url = (
        'https://www.googleapis.com/books/v1/volumes?'
        + urllib.parse.urlencode({'q': f'isbn:{isbn}', 'maxResults': 1})
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict) or not payload.get('items'):
        return {}
    return _parse_google_volume((payload['items'][0] or {}).get('volumeInfo') or {}, isbn)


def _search_google_books(query: str) -> list[dict]:
    url = (
        'https://www.googleapis.com/books/v1/volumes?'
        + urllib.parse.urlencode({'q': query, 'maxResults': 5})
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return []
    results = [_parse_google_volume((item or {}).get('volumeInfo') or {})
               for item in payload.get('items') or []]
    return [r for r in results if r.get('title')]


_DC = '{http://purl.org/dc/elements/1.1/}'
_XSI_TYPE = '{http://www.w3.org/2001/XMLSchema-instance}type'
_LANGUAGE_NAMES = {'ger': 'Deutsch', 'eng': 'Englisch', 'fre': 'Französisch',
                   'spa': 'Spanisch', 'ita': 'Italienisch'}


_ROLE_WORDS = r'(Verfasser|Übersetzer|Erzähler|Herausgeber|Illustrator|Mitwirkende[rn]?|Sprecher)(In|in)?'


def _person_name(raw: str) -> str:
    """'Hawking, Stephen W. [Verfasser]' -> 'Stephen W. Hawking'.

    Also survives dirty DNB records with unbalanced brackets like
    'Hawking, Stephen Verfasser]' or '[Kober, Hainer [Übersetzer]'.
    """
    name = re.sub(r'\[[^\]]*\]', '', raw)  # complete [role] groups
    name = re.sub(r'[\[\]]', '', name)  # stray brackets
    name = re.sub(r'\s+' + _ROLE_WORDS + r'\s*$', '', name)
    name = name.strip(' ,;')
    match = re.match(r'^([^,]+),\s*(.+)$', name)
    return f'{match.group(2)} {match.group(1)}'.strip() if match else name


_OAI_DC = '{http://www.openarchives.org/OAI/2.0/oai_dc/}dc'


def _dnb_query(cql: str, maximum: int) -> list:
    """Run one SRU query against the DNB and return the <dc> record elements."""
    url = (
        'https://services.dnb.de/sru/dnb?'
        + urllib.parse.urlencode({
            'version': '1.1',
            'operation': 'searchRetrieve',
            'query': cql,
            'recordSchema': 'oai_dc',
            'maximumRecords': str(maximum),
        })
    )
    body = _http_get_text(url)
    if not body:
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    return list(root.iter(_OAI_DC))


def _parse_dnb_record(record, fallback_isbn: str = '') -> dict:
    def texts(tag: str) -> list[str]:
        return [el.text.strip() for el in record.iter(_DC + tag) if el.text and el.text.strip()]

    titles = texts('title')
    if not titles:
        return {}
    # 'Original ; Deutscher Titel / Verfasserangabe' -> keep the German main title.
    title = titles[0].split(' / ')[0]
    title = re.sub(r'^\[[^\]]*\]\s*;\s*', '', title).strip()

    creators = texts('creator')
    authors = ([_person_name(c) for c in creators if 'Verfasser' in c]
               or [_person_name(c) for c in creators])
    authors = list(dict.fromkeys(a for a in authors if a))

    publisher = (texts('publisher') or [''])[0]
    publisher = publisher.split(' : ')[-1].strip()

    pages = None
    for fmt in texts('format'):
        match = re.search(r'(\d+)\s*Seiten', fmt)
        if match:
            pages = int(match.group(1))
            break

    # ISBN identifiers carry price suffixes ('978-… Festeinband : EUR 16.00');
    # extract the bare number and prefer the 13-digit form.
    candidates = []
    for el in record.iter(_DC + 'identifier'):
        if (el.get(_XSI_TYPE) or '').endswith('ISBN') and el.text:
            match = re.match(r'[0-9][0-9Xx\-]*', el.text.strip())
            if match:
                candidates.append(_digits(match.group(0)))
    isbn_out = next((c for c in candidates if len(c) == 13),
                    candidates[0] if candidates else fallback_isbn)

    date = (texts('date') or [None])[0]
    language = (texts('language') or [None])[0]
    subjects = [re.sub(r'^\d{3}\s+', '', s) for s in texts('subject')]
    return _clean({
        'title': title,
        'authors': ', '.join(authors),
        'publisher': publisher,
        'published': date,
        'year': _year_from(date),
        'pages': pages,
        'categories': ', '.join(dict.fromkeys(subjects[:5])),
        'language': _LANGUAGE_NAMES.get(language or '', language),
        'isbn': isbn_out,
        'cover_url': f'https://portal.dnb.de/opac/mvb/cover?isbn={isbn_out}' if isbn_out else None,
    })


def _fetch_dnb(query: str) -> dict:
    """Deutsche Nationalbibliothek via the open SRU interface (oai_dc records).

    Best coverage for German-language books; metadata is CC0, no API key.
    Covers come from the DNB/MVB cover service (may 404 for old titles).
    """
    isbn = _digits(query)
    if not isbn:
        return {}
    records = _dnb_query(f'NUM={isbn}', 1)
    return _parse_dnb_record(records[0], isbn) if records else {}


def _search_dnb(query: str) -> list[dict]:
    """Free-text search: every word must match (DNB 'WOE' word index)."""
    words = re.findall(r'\w+', query, re.UNICODE)[:8]
    if not words:
        return []
    cql = ' and '.join(f'WOE={word}' for word in words)
    results = [_parse_dnb_record(record) for record in _dnb_query(cql, 5)]
    return [r for r in results if r.get('title')]


def _search_openlibrary(query: str) -> list[dict]:
    url = (
        'https://openlibrary.org/search.json?'
        + urllib.parse.urlencode({
            'q': query, 'limit': 5,
            'fields': 'title,author_name,first_publish_year,publisher,isbn,cover_i,number_of_pages_median',
        })
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return []
    results = []
    for doc in payload.get('docs') or []:
        isbns = [_digits(i) for i in doc.get('isbn') or []]
        isbn = next((i for i in isbns if len(i) == 13), isbns[0] if isbns else None)
        cover = doc.get('cover_i')
        results.append(_clean({
            'title': doc.get('title'),
            'authors': ', '.join(doc.get('author_name') or []),
            'publisher': (doc.get('publisher') or [None])[0],
            'year': str(doc['first_publish_year']) if doc.get('first_publish_year') else None,
            'pages': doc.get('number_of_pages_median'),
            'isbn': isbn,
            'cover_url': f'https://covers.openlibrary.org/b/id/{cover}-L.jpg' if cover else None,
        }))
    return [r for r in results if r.get('title')]


register(LookupProvider(
    key='openlibrary',
    label=_('Open Library (Bücher · ISBN)'),
    short='Open Library',
    description=_('Offene, kostenlose Bücher-Datenbank des Internet Archive. Suche per ISBN.'),
    fetch=_fetch_openlibrary,
    search=_search_openlibrary,
    query_attribute='isbn',
    kinds=('books',),
    provides=('title', 'authors', 'publisher', 'published', 'year', 'pages', 'categories', 'isbn', 'cover_url'),
))

register(LookupProvider(
    key='googlebooks',
    label=_('Google Books (Bücher · ISBN)'),
    short='Google Books',
    description=_('Google-Buchsuche. Suche per ISBN, liefert zusätzlich Beschreibung & Sprache.'),
    fetch=_fetch_google_books,
    search=_search_google_books,
    query_attribute='isbn',
    kinds=('books',),
    provides=('title', 'authors', 'publisher', 'published', 'year', 'pages', 'description',
              'categories', 'language', 'isbn', 'cover_url'),
))

register(LookupProvider(
    key='dnb',
    label=_('Deutsche Nationalbibliothek (Bücher · ISBN)'),
    short='DNB',
    description=_('Offizieller DNB-Katalog – beste Abdeckung für deutschsprachige Bücher. Suche per ISBN.'),
    fetch=_fetch_dnb,
    search=_search_dnb,
    query_attribute='isbn',
    kinds=('books',),
    provides=('title', 'authors', 'publisher', 'published', 'year', 'pages', 'categories',
              'language', 'isbn', 'cover_url'),
))


# ---------------------------------------------------------------------------
# Music: MusicBrainz (open database, no API key; barcode lookup + free search)
# ---------------------------------------------------------------------------

_LUCENE_SPECIALS = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


def _parse_musicbrainz_release(release: dict) -> dict:
    artists = ''.join(
        (credit.get('name') or '') + (credit.get('joinphrase') or '')
        for credit in release.get('artist-credit') or []
    )
    labels = [info.get('label', {}).get('name')
              for info in release.get('label-info') or []
              if isinstance(info.get('label'), dict)]
    media = release.get('media') or []
    fmt = (media[0] or {}).get('format') if media else None
    release_id = release.get('id')
    return _clean({
        'title': release.get('title'),
        'artist': artists,
        'authors': artists,  # so music data also fills generic creator fields
        'publisher': labels[0] if labels else None,
        'published': release.get('date'),
        'year': _year_from(release.get('date')),
        'format': fmt,
        'ean': release.get('barcode'),
        'cover_url': f'https://coverartarchive.org/release/{release_id}/front-250' if release_id else None,
    })


def _musicbrainz_query(query: str, limit: int) -> list[dict]:
    url = (
        'https://musicbrainz.org/ws/2/release/?'
        + urllib.parse.urlencode({'query': query, 'fmt': 'json', 'limit': limit})
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return []
    return [r for r in (payload.get('releases') or []) if isinstance(r, dict)]


def _fetch_musicbrainz(query: str) -> dict:
    barcode = _digits(query)
    if not barcode:
        return {}
    releases = _musicbrainz_query(f'barcode:{barcode}', 1)
    return _parse_musicbrainz_release(releases[0]) if releases else {}


def _search_musicbrainz(query: str) -> list[dict]:
    cleaned = _LUCENE_SPECIALS.sub(' ', query).strip()
    if not cleaned:
        return []
    results = [_parse_musicbrainz_release(r) for r in _musicbrainz_query(cleaned, 5)]
    return [r for r in results if r.get('title')]


register(LookupProvider(
    key='musicbrainz',
    label=_('MusicBrainz (Musik · Barcode)'),
    short='MusicBrainz',
    description=_('Offene Musik-Datenbank: CDs, Vinyl & Co. per Barcode-Scan oder Titel-/Interpretensuche.'),
    fetch=_fetch_musicbrainz,
    search=_search_musicbrainz,
    query_attribute='ean',
    kinds=('music',),
    provides=('title', 'artist', 'authors', 'publisher', 'published', 'year',
              'format', 'ean', 'cover_url'),
))


# ---------------------------------------------------------------------------
# Films & series: TMDb (free API key required — runtime setting tmdb_api_key)
# ---------------------------------------------------------------------------

_TMDB_GENRES = {
    28: 'Action', 12: 'Abenteuer', 16: 'Animation', 35: 'Komödie', 80: 'Krimi',
    99: 'Dokumentarfilm', 18: 'Drama', 10751: 'Familie', 14: 'Fantasy',
    36: 'Historie', 27: 'Horror', 10402: 'Musik', 9648: 'Mystery',
    10749: 'Liebesfilm', 878: 'Science Fiction', 10770: 'TV-Film', 53: 'Thriller',
    10752: 'Kriegsfilm', 37: 'Western', 10759: 'Action & Abenteuer',
    10762: 'Kinder', 10763: 'News', 10764: 'Reality', 10765: 'Sci-Fi & Fantasy',
    10766: 'Soap', 10767: 'Talk', 10768: 'Krieg & Politik',
}
_ISO_LANGUAGES = {
    'de': 'Deutsch', 'en': 'Englisch', 'fr': 'Französisch', 'es': 'Spanisch',
    'it': 'Italienisch', 'ja': 'Japanisch', 'ko': 'Koreanisch',
    'zh': 'Chinesisch', 'ru': 'Russisch',
}


def _tmdb_key() -> str:
    from .runtime_settings import get_setting
    return get_setting('tmdb_api_key')


def _parse_tmdb_result(entry: dict) -> dict:
    date = entry.get('release_date') or entry.get('first_air_date')
    poster = entry.get('poster_path')
    language = entry.get('original_language')
    genres = [_TMDB_GENRES[g] for g in entry.get('genre_ids') or [] if g in _TMDB_GENRES]
    return _clean({
        'title': entry.get('title') or entry.get('name'),
        'published': date,
        'year': _year_from(date),
        'description': entry.get('overview'),
        'categories': ', '.join(genres),
        'language': _ISO_LANGUAGES.get(language or '', language),
        'cover_url': f'https://image.tmdb.org/t/p/w342{poster}' if poster else None,
    })


def _search_tmdb(query: str) -> list[dict]:
    key = _tmdb_key()
    if not key:
        return []
    url = (
        'https://api.themoviedb.org/3/search/multi?'
        + urllib.parse.urlencode({
            'api_key': key, 'query': query, 'language': 'de-DE', 'include_adult': 'false',
        })
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return []
    results = [_parse_tmdb_result(entry) for entry in (payload.get('results') or [])[:8]
               if isinstance(entry, dict) and entry.get('media_type') in ('movie', 'tv')]
    return [r for r in results if r.get('title')][:5]


register(LookupProvider(
    key='tmdb',
    label=_('TMDb (Filme & Serien · Titelsuche)'),
    short='TMDb',
    description=_('The Movie Database: Filme & Serien per Titelsuche. Benötigt einen kostenlosen '
                  'API-Schlüssel (Systemeinstellungen → TMDb-API-Schlüssel).'),
    fetch=None,  # TMDb has no barcode/EAN lookup
    search=_search_tmdb,
    query_attribute='ean',
    kinds=('movies',),
    provides=('title', 'published', 'year', 'description', 'categories', 'language', 'cover_url'),
    available=lambda: bool(_tmdb_key()),
))


# ---------------------------------------------------------------------------
# Video games: RAWG (free API key required — runtime setting rawg_api_key)
# ---------------------------------------------------------------------------

def _rawg_key() -> str:
    from .runtime_settings import get_setting
    return get_setting('rawg_api_key')


def _search_rawg(query: str) -> list[dict]:
    key = _rawg_key()
    if not key:
        return []
    url = (
        'https://api.rawg.io/api/games?'
        + urllib.parse.urlencode({'key': key, 'search': query, 'page_size': 5})
    )
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return []
    results = []
    for entry in payload.get('results') or []:
        if not isinstance(entry, dict):
            continue
        platforms = [p.get('platform', {}).get('name')
                     for p in entry.get('platforms') or [] if isinstance(p, dict)]
        genres = [g.get('name') for g in entry.get('genres') or [] if isinstance(g, dict)]
        results.append(_clean({
            'title': entry.get('name'),
            'published': entry.get('released'),
            'year': _year_from(entry.get('released')),
            'platform': ', '.join(p for p in platforms if p)[:150],
            'categories': ', '.join(g for g in genres if g),
            'cover_url': entry.get('background_image'),
        }))
    return [r for r in results if r.get('title')]


register(LookupProvider(
    key='rawg',
    label=_('RAWG (Videospiele · Titelsuche)'),
    short='RAWG',
    description=_('Größte offene Videospiel-Datenbank: Titelsuche mit Plattformen & Genres. '
                  'Benötigt einen kostenlosen API-Schlüssel (Systemeinstellungen → RAWG-API-Schlüssel).'),
    fetch=None,  # RAWG has no barcode/EAN lookup
    search=_search_rawg,
    query_attribute='ean',
    kinds=('games',),
    provides=('title', 'published', 'year', 'platform', 'categories', 'cover_url'),
    available=lambda: bool(_rawg_key()),
))


# ---------------------------------------------------------------------------
# Board games: Wikidata (open, no API key)
# ---------------------------------------------------------------------------
# BoardGameGeek's XML API now rejects unauthenticated requests (HTTP 401), so
# board games are resolved through Wikidata instead: entity search, then one
# batched claims request, filtered to game-like items via P31 (instance of).

_WIKIDATA_API = 'https://www.wikidata.org/w/api.php'
# P31 values that make a search hit a (board/card/tabletop) game.
_WIKIDATA_GAME_CLASSES = {
    'Q131436',   # board game
    'Q142714',   # card game
    'Q3244175',  # tabletop game
    'Q1368898',  # dice game
    'Q11410',    # game
}


def _wikidata_quantity(claims: dict, prop: str) -> int | None:
    for claim in claims.get(prop) or []:
        value = (claim.get('mainsnak') or {}).get('datavalue', {}).get('value', {})
        amount = str(value.get('amount') or '')
        if amount.lstrip('+-').isdigit():
            return int(amount)
    return None


def _search_wikidata(query: str) -> list[dict]:
    search_url = _WIKIDATA_API + '?' + urllib.parse.urlencode({
        'action': 'wbsearchentities', 'search': query, 'language': 'de',
        'uselang': 'de', 'type': 'item', 'limit': 8, 'format': 'json',
    })
    payload = _http_get_json(search_url)
    if not isinstance(payload, dict):
        return []
    hits = {h['id']: h for h in payload.get('search') or []
            if isinstance(h, dict) and h.get('id')}
    if not hits:
        return []

    detail_url = _WIKIDATA_API + '?' + urllib.parse.urlencode({
        'action': 'wbgetentities', 'ids': '|'.join(hits), 'format': 'json',
        'props': 'claims|labels|descriptions', 'languages': 'de|en',
    })
    details = _http_get_json(detail_url)
    if not isinstance(details, dict):
        return []

    results = []
    for qid, hit in hits.items():
        entity = (details.get('entities') or {}).get(qid) or {}
        claims = entity.get('claims') or {}
        classes = {(c.get('mainsnak') or {}).get('datavalue', {}).get('value', {}).get('id')
                   for c in claims.get('P31') or []}
        descriptions = entity.get('descriptions') or {}
        description = ((descriptions.get('de') or {}).get('value')
                       or (descriptions.get('en') or {}).get('value') or '')
        # Keep only game-like hits: known P31 class, or (for the many game
        # subclasses Wikidata has) a description that says it is a game.
        if not (classes & _WIKIDATA_GAME_CLASSES
                or re.search(r'spiel|game', description, re.IGNORECASE)):
            continue  # a city, football club, … — not a game
        labels = entity.get('labels') or {}
        title = (labels.get('de') or labels.get('en') or {}).get('value') or hit.get('label')
        if not title:
            continue
        published = None
        for claim in claims.get('P577') or []:  # publication date '+1995-00-00…'
            time = (claim.get('mainsnak') or {}).get('datavalue', {}).get('value', {}).get('time')
            if time:
                published = time.lstrip('+')[:10].rstrip('-0') or None
                break
        minimum = _wikidata_quantity(claims, 'P1872')  # min players
        maximum = _wikidata_quantity(claims, 'P1873')  # max players
        players = None
        if minimum and maximum:
            players = str(minimum) if minimum == maximum else f'{minimum}–{maximum}'
        elif minimum or maximum:
            players = str(minimum or maximum)
        image = None
        for claim in claims.get('P18') or []:  # image (Commons file name)
            name = (claim.get('mainsnak') or {}).get('datavalue', {}).get('value')
            if isinstance(name, str) and name:
                image = ('https://commons.wikimedia.org/wiki/Special:FilePath/'
                         + urllib.parse.quote(name) + '?width=400')
                break
        results.append(_clean({
            'title': title,
            'year': _year_from(published),
            'published': published,
            'players': players,
            'description': description or None,
            'cover_url': image,
        }))
    return results[:5]


register(LookupProvider(
    key='wikidata',
    label=_('Wikidata (Brett- & Gesellschaftsspiele · Titelsuche)'),
    short='Wikidata',
    description=_('Freie Wissensdatenbank: Titelsuche für Brett-, Karten- und '
                  'Gesellschaftsspiele mit Jahr, Spielerzahl und Bild.'),
    fetch=None,  # Wikidata has no reliable EAN lookup
    search=_search_wikidata,
    query_attribute='ean',
    kinds=('boardgames',),
    provides=('title', 'year', 'published', 'players', 'description', 'cover_url'),
))


# ---------------------------------------------------------------------------
# Generic products: UPCitemdb (free trial endpoint, no key; any EAN/UPC)
# ---------------------------------------------------------------------------

def _fetch_upcitemdb(query: str) -> dict:
    code = _digits(query)
    if not code or len(code) < 8:
        return {}
    url = ('https://api.upcitemdb.com/prod/trial/lookup?'
           + urllib.parse.urlencode({'upc': code}))
    payload = _http_get_json(url)
    if not isinstance(payload, dict):
        return {}
    items = payload.get('items') or []
    if not items or not isinstance(items[0], dict):
        return {}
    entry = items[0]
    # Product image URLs point at arbitrary merchant servers — deliberately NOT
    # emitted (server-side cover download is limited to trusted hosts).
    return _clean({
        'title': entry.get('title'),
        'brand': entry.get('brand'),
        'description': (entry.get('description') or '')[:600] or None,
        'categories': entry.get('category'),
        'ean': entry.get('ean') or code,
    })


register(LookupProvider(
    key='upcitemdb',
    label=_('UPCitemdb (Alle Produkte · Barcode)'),
    short='UPCitemdb',
    description=_('Generische Produktdatenbank: liefert zu fast jedem EAN/UPC-Barcode Titel, '
                  'Marke und Kategorie (kostenloses Kontingent, ohne Schlüssel).'),
    fetch=_fetch_upcitemdb,
    search=None,
    query_attribute='ean',
    kinds=('', 'movies', 'music', 'games', 'boardgames'),
    provides=('title', 'brand', 'description', 'categories', 'ean'),
))


# ---------------------------------------------------------------------------
# Combined per-kind provider
# ---------------------------------------------------------------------------
# Which sources are queried, in which order (= data quality for German media).
_KIND_CHAINS: dict[str, tuple[str, ...]] = {
    'books': ('dnb', 'googlebooks', 'openlibrary'),
    'movies': ('tmdb', 'upcitemdb'),
    'music': ('musicbrainz', 'upcitemdb'),
    'games': ('rawg', 'upcitemdb'),
    'boardgames': ('wikidata', 'upcitemdb'),
    # Mixed collections ask everything that is available.
    '': ('dnb', 'googlebooks', 'openlibrary', 'musicbrainz', 'tmdb', 'rawg',
         'wikidata', 'upcitemdb'),
}

_MAX_SEARCH_RESULTS = 10


def chain_for(kind: str) -> list[LookupProvider]:
    """The available providers for a media kind, in priority order."""
    keys = _KIND_CHAINS.get(kind if kind in _KIND_CHAINS else '')
    return [_PROVIDERS[k] for k in keys if k in _PROVIDERS and _PROVIDERS[k].is_available()]


def _looks_like_isbn(code: str) -> bool:
    digits = _digits(code)
    return len(digits) == 10 or (len(digits) == 13 and digits.startswith(('978', '979')))


def _fetch_chain(chain: list[LookupProvider], query: str) -> dict:
    """Ask each code-capable source in turn and merge (first hit per attribute
    wins); stop early once every attribute the chain can deliver is filled.
    ISBN-shaped codes prefer the book sources, other EANs the product sources.
    """
    fetchers = [p for p in chain if p.fetch]
    is_isbn = _looks_like_isbn(query)
    fetchers.sort(key=lambda p: ('books' in p.kinds) != is_isbn)
    wanted = {attr for p in fetchers for attr in p.provides}
    merged: dict = {}
    for provider in fetchers:
        if merged and wanted <= merged.keys():
            break
        for attribute, value in provider.fetch(query).items():
            merged.setdefault(attribute, value)
    return merged


def _search_chain(chain: list[LookupProvider], query: str) -> list[dict]:
    """Free-text search across every source of the chain — in parallel.

    Candidate lists are merged in chain order; duplicates (same ISBN/EAN, or
    same title+creator when no code is present) are collapsed.
    """
    searchers = [p for p in chain if p.search]
    if not searchers:
        return []
    timeout = _http_timeout()  # resolve the runtime setting in THIS thread

    def run(provider: LookupProvider) -> list[dict]:
        _thread_timeout.value = timeout
        try:
            return provider.search(query)
        except Exception:  # one broken source must not kill the whole search
            return []
        finally:
            # WSGI worker threads are reused — never leave a stale override.
            _thread_timeout.value = None

    if len(searchers) == 1:
        per_provider = [run(searchers[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(searchers)) as pool:
            per_provider = list(pool.map(run, searchers))

    merged: list[dict] = []
    seen: set = set()
    for results in per_provider:
        for result in results:
            code = _digits(str(result.get('isbn') or result.get('ean') or ''))
            fingerprint = code or (
                str(result.get('title', '')).strip().lower(),
                str(result.get('authors') or result.get('artist') or '').strip().lower(),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(result)
    return merged[:_MAX_SEARCH_RESULTS]


def provider_for(collection) -> LookupProvider:
    """The combined provider for a collection's media kind.

    Queries every available source of the kind's chain and merges the results;
    ``label`` lists the active sources so the UI can say what is being asked.
    """
    kind = collection.lookup_provider if collection.lookup_provider in _KIND_CHAINS else ''
    chain = chain_for(kind)
    query_attribute = 'isbn' if kind == 'books' else 'ean'
    return LookupProvider(
        key=f'auto:{kind or "all"}',
        label=', '.join(p.short_label for p in chain) or _('keine Datenbank verfügbar'),
        description='',
        fetch=(lambda q: _fetch_chain(chain, q)) if any(p.fetch for p in chain) else None,
        search=(lambda q: _search_chain(chain, q)) if any(p.search for p in chain) else None,
        query_attribute=query_attribute,
        kinds=(kind,),
        provides=tuple(dict.fromkeys(attr for p in chain for attr in p.provides)),
    )


def auto_provider() -> LookupProvider:
    """The combined provider over ALL available sources (mixed collections)."""
    class _All:
        lookup_provider = ''
    return provider_for(_All())
