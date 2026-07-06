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

Adding a new source = registering one more :class:`LookupProvider`; no model,
view or template change required. Two providers come pre-configured (Open
Library and Google Books — official, free, no API key), so ISBN auto-fill works
out of the box.

Network access uses only the standard library (``urllib``) so there is no extra
dependency. ``_http_get_json`` is a module-level function on purpose: tests
monkeypatch it to return canned payloads without hitting the network.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from xml.etree import ElementTree

from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# Shared attribute vocabulary
# ---------------------------------------------------------------------------
# (key, human label). A FieldDefinition maps to ONE of these keys; every
# provider emits values keyed by these. This is what keeps the mapping
# provider-independent and the whole feature dynamic.
ATTRIBUTES: list[tuple[str, str]] = [
    ('title', _('Titel')),
    ('authors', _('Autor(en)')),
    ('publisher', _('Verlag')),
    ('published', _('Erscheinungsdatum')),
    ('year', _('Erscheinungsjahr')),
    ('pages', _('Seitenzahl')),
    ('description', _('Beschreibung')),
    ('categories', _('Kategorien / Genre')),
    ('language', _('Sprache')),
    ('isbn', _('ISBN')),
    ('cover_url', _('Cover-Bild (URL)')),
]
ATTRIBUTE_LABELS = dict(ATTRIBUTES)
VALID_ATTRIBUTES = {key for key, _ in ATTRIBUTES}

# Attributes whose field values make sensible free-text search input. Form
# fields mapped to one of these get a "search the databases" suggestion button.
SEARCHABLE_ATTRIBUTES = ('title', 'authors', 'publisher', 'categories')

_USER_AGENT = 'CMS-Collection-Manager/1.0 (+https://example.org)'


def _http_timeout() -> int:
    """Request timeout in seconds — runtime setting ``lookup_timeout``.

    Resolved per call (deferred import: this module must stay importable
    without Django apps being ready) so staff can tune it without a restart.
    """
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
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
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
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _http_get_bytes(url: str) -> tuple[bytes, str] | None:
    """Fetch a URL and return ``(body, content_type)``. ``None`` on failure.

    Kept tiny and dependency-free; monkeypatched in tests.
    """
    request = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_http_timeout()) as response:
            content_type = (response.headers.get_content_type() or '').lower()
            return response.read(_MAX_COVER_BYTES + 1), content_type
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


# Cover downloads are server-side fetches of client-supplied URLs, so they are
# restricted to the hosts our own providers emit (SSRF guard).
COVER_HOSTS = {
    'covers.openlibrary.org',
    'books.google.com', 'books.googleusercontent.com',
    'portal.dnb.de',
}
_MAX_COVER_BYTES = 5 * 1024 * 1024


def fetch_cover(url: str) -> tuple[bytes, str] | None:
    """Download a cover image from a whitelisted provider host.

    Returns ``(bytes, extension)`` or ``None`` (bad host/scheme, non-image
    response, too large, network error).
    """
    parsed = urllib.parse.urlparse(url or '')
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in COVER_HOSTS:
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

    ``fetch(query)`` returns a ``{attribute_key: value}`` dict using the shared
    vocabulary, or ``{}`` when nothing was found. ``query_attribute`` is the
    attribute whose field doubles as the search input (e.g. the ISBN field).
    """

    key: str
    label: str
    description: str
    fetch: Callable[[str], dict]
    query_attribute: str = 'isbn'
    # Attributes this provider can actually deliver (subset of ATTRIBUTES keys).
    provides: tuple[str, ...] = field(default_factory=tuple)
    # Optional free-text search (title/author/keywords) returning up to a
    # handful of candidate records in the same attribute vocabulary.
    search: Callable[[str], list[dict]] | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_PROVIDERS: dict[str, LookupProvider] = {}


def register(provider: LookupProvider) -> LookupProvider:
    _PROVIDERS[provider.key] = provider
    return provider


def get_provider(key: str | None) -> LookupProvider | None:
    return _PROVIDERS.get(key or '')


def all_providers() -> list[LookupProvider]:
    return list(_PROVIDERS.values())


# Key of the combined provider that queries every registered source. The UI no
# longer offers a per-collection choice — lookups always go through this one.
AUTO_PROVIDER_KEY = 'auto'


def auto_provider() -> LookupProvider:
    return _PROVIDERS[AUTO_PROVIDER_KEY]


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
    description=_('Offene, kostenlose Bücher-Datenbank des Internet Archive. Suche per ISBN.'),
    fetch=_fetch_openlibrary,
    search=_search_openlibrary,
    query_attribute='isbn',
    provides=('title', 'authors', 'publisher', 'published', 'year', 'pages', 'categories', 'isbn', 'cover_url'),
))

register(LookupProvider(
    key='googlebooks',
    label=_('Google Books (Bücher · ISBN)'),
    description=_('Google-Buchsuche. Suche per ISBN, liefert zusätzlich Beschreibung & Sprache.'),
    fetch=_fetch_google_books,
    search=_search_google_books,
    query_attribute='isbn',
    provides=('title', 'authors', 'publisher', 'published', 'year', 'pages', 'description',
              'categories', 'language', 'isbn', 'cover_url'),
))

register(LookupProvider(
    key='dnb',
    label=_('Deutsche Nationalbibliothek (Bücher · ISBN)'),
    description=_('Offizieller DNB-Katalog – beste Abdeckung für deutschsprachige Bücher. Suche per ISBN.'),
    fetch=_fetch_dnb,
    search=_search_dnb,
    query_attribute='isbn',
    provides=('title', 'authors', 'publisher', 'published', 'year', 'pages', 'categories',
              'language', 'isbn', 'cover_url'),
))

# Fallback chain: ask each source in turn and merge (first hit per attribute
# wins); stop early once every attribute the chain can deliver is filled.
_AUTO_CHAIN = ('dnb', 'googlebooks', 'openlibrary')


def _fetch_auto(query: str) -> dict:
    wanted = {attr for key in _AUTO_CHAIN for attr in _PROVIDERS[key].provides}
    merged: dict = {}
    for key in _AUTO_CHAIN:
        if merged and wanted <= merged.keys():
            break
        for attribute, value in _PROVIDERS[key].fetch(query).items():
            merged.setdefault(attribute, value)
    return merged


_MAX_SEARCH_RESULTS = 8


def _search_auto(query: str) -> list[dict]:
    """Query EVERY source of the chain and merge the candidate lists.

    Duplicates (same ISBN, or same title+authors when no ISBN is present) are
    collapsed, keeping the first occurrence — the chain is ordered by data
    quality for German-language media, so DNB hits stay on top.
    """
    merged: list[dict] = []
    seen: set = set()
    for key in _AUTO_CHAIN:
        for result in _PROVIDERS[key].search(query):
            isbn = _digits(str(result.get('isbn') or ''))
            fingerprint = isbn or (
                str(result.get('title', '')).strip().lower(),
                str(result.get('authors', '')).strip().lower(),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(result)
    return merged[:_MAX_SEARCH_RESULTS]


register(LookupProvider(
    key=AUTO_PROVIDER_KEY,
    label=_('DNB, Google Books & Open Library'),
    description=_('Fragt alle verfügbaren Datenbanken (DNB, Google Books, Open Library) ab '
                  'und kombiniert die Treffer – höchste Trefferquote.'),
    fetch=_fetch_auto,
    search=_search_auto,
    query_attribute='isbn',
    provides=tuple(dict.fromkeys(
        attr for key in _AUTO_CHAIN for attr in _PROVIDERS[key].provides
    )),
))
