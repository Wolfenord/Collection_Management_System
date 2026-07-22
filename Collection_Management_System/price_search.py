"""Price comparison / multi-platform shopping search (link-out, no scraping).

Design
------
For an item (or a free query) the CMS builds **deep links** into the search of
many shopping platforms — price-comparison meta engines, second-hand
marketplaces and regular shops — pre-filled with the best available query
(ISBN/EAN where the platform supports precise code search, otherwise
title/creator text) and, where the platform's URL scheme supports it, with the
chosen filters (condition, price range, sorting).

Deliberately **no server-side price fetching**:
  * scraping violates most platforms' terms of service and breaks constantly;
  * the official price APIs (Amazon PA-API, eBay Browse, …) require partner
    accounts and per-platform contracts;
  * GDPR: this way the CMS server transmits nothing at all — a platform only
    learns about the search when the *user clicks* the link (documented on the
    privacy page). All links open with ``rel="noopener noreferrer nofollow"``.

Adding a platform = one :class:`Platform` entry in :data:`PLATFORMS`.
Every builder receives a :class:`PriceQuery` and returns a URL or ``None``
("this platform cannot answer that query", e.g. Eurobuch without an ISBN).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable
from urllib.parse import quote, urlencode

from django import forms
from django.utils.translation import gettext_lazy as _

from .lookup_providers import MEDIA_KINDS, QUERY_ATTRIBUTES, _digits
from .models import FieldType

# Condition filter values.
CONDITION_CHOICES = [
    ('', _('Neu & gebraucht')),
    ('new', _('Nur neu')),
    ('used', _('Nur gebraucht')),
]

SORT_CHOICES = [
    ('', _('Relevanz')),
    ('price', _('Preis aufsteigend (wo unterstützt)')),
    ('year_desc', _('Jahr (neueste zuerst)')),
    ('year_asc', _('Jahr (älteste zuerst)')),
]

# Groups the result page is organised by.
GROUPS = [
    ('meta', _('Preisvergleich & Metasuche')),
    ('used', _('Gebraucht kaufen')),
    ('market', _('Marktplätze')),
    ('new', _('Neu kaufen')),
]
GROUP_LABELS = dict(GROUPS)


@dataclass(frozen=True)
class PriceQuery:
    """One search request, platform-independent."""

    q: str = ''                       # free text (title, creator, keywords)
    code: str = ''                    # ISBN/EAN/UPC (any formatting)
    kind: str = ''                    # media kind, see lookup_providers.MEDIA_KINDS
    condition: str = ''               # '' | 'new' | 'used'
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    sort: str = ''                    # '' | 'price' | 'year_desc' | 'year_asc'
    year_from: int | None = None      # publication-year range (live offers filter)
    year_to: int | None = None

    @property
    def digits(self) -> str:
        return _digits(self.code)

    @property
    def isbn(self) -> str:
        """The code if it is ISBN-shaped ('' otherwise)."""
        d = self.digits
        if len(d) == 10 or (len(d) == 13 and d.startswith(('978', '979'))):
            return d
        return ''

    @property
    def best_text(self) -> str:
        """Code first (most precise), else the free text."""
        return self.digits or self.q.strip()

    def has_query(self) -> bool:
        return bool(self.best_text)


@dataclass(frozen=True)
class Platform:
    """One external shopping/search platform."""

    key: str
    label: str
    build: Callable[[PriceQuery], str | None]
    group: str = 'market'             # see GROUPS
    kinds: tuple[str, ...] = ()       # () = suitable for every media kind
    condition: str = 'both'           # 'both' | 'new' | 'used' (what it sells)
    # Which PriceQuery filters the deep link really honours (shown in the UI).
    supports: tuple[str, ...] = ()    # subset of ('code','price','condition','sort')
    note: str = ''

    def matches(self, query: PriceQuery) -> bool:
        if self.kinds and query.kind not in self.kinds:
            return False
        if query.condition == 'new' and self.condition == 'used':
            return False
        if query.condition == 'used' and self.condition == 'new':
            return False
        return True


def _ebay(query: PriceQuery) -> str | None:
    params = {'_nkw': query.best_text}
    if query.min_price is not None:
        params['_udlo'] = f'{query.min_price}'
    if query.max_price is not None:
        params['_udhi'] = f'{query.max_price}'
    if query.condition == 'new':
        params['LH_ItemCondition'] = '1000'
    elif query.condition == 'used':
        params['LH_ItemCondition'] = '3000'
    if query.sort == 'price':
        params['_sop'] = '15'  # lowest price + shipping first
    return 'https://www.ebay.de/sch/i.html?' + urlencode(params)


_AMAZON_CATEGORIES = {'books': 'stripbooks', 'movies': 'dvd', 'music': 'music',
                      'games': 'videogames', 'boardgames': 'toys'}


def _amazon(query: PriceQuery) -> str | None:
    params = {'k': query.best_text}
    category = _AMAZON_CATEGORIES.get(query.kind)
    if category:
        params['i'] = category
    if query.min_price is not None:
        params['low-price'] = f'{query.min_price}'
    if query.max_price is not None:
        params['high-price'] = f'{query.max_price}'
    return 'https://www.amazon.de/s?' + urlencode(params)


def _idealo(query: PriceQuery) -> str | None:
    return ('https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?'
            + urlencode({'q': query.best_text}))


def _geizhals(query: PriceQuery) -> str | None:
    return 'https://geizhals.de/?' + urlencode({'fs': query.best_text})


def _eurobuch(query: PriceQuery) -> str | None:
    # Dedicated ISBN price comparison across dozens of book platforms
    # (new & used) — only works with an ISBN.
    return f'https://www.eurobuch.com/buch/isbn/{query.isbn}.html' if query.isbn else None


def _vialibri(query: PriceQuery) -> str | None:
    # ViaLibri: the large antiquarian/second-hand book meta-search across
    # hundreds of dealers. ISBN gives the most precise results.
    if query.isbn:
        return 'https://www.vialibri.net/search?' + urlencode({'isbn': query.isbn})
    if query.q.strip():
        return 'https://www.vialibri.net/search?' + urlencode({'keywords': query.q.strip()})
    return None


def _booklooker(query: PriceQuery) -> str | None:
    if query.isbn:
        return f'https://www.booklooker.de/B%C3%BCcher/Angebote/isbn={quote(query.isbn)}'
    if query.q.strip():
        return f'https://www.booklooker.de/B%C3%BCcher/Angebote/titel={quote(query.q.strip())}'
    return None


def _zvab(query: PriceQuery) -> str | None:
    if query.isbn:
        return 'https://www.zvab.com/servlet/SearchResults?' + urlencode({'isbn': query.isbn})
    if query.q.strip():
        return 'https://www.zvab.com/servlet/SearchResults?' + urlencode({'tn': query.q.strip()})
    return None


def _abebooks(query: PriceQuery) -> str | None:
    if query.isbn:
        return 'https://www.abebooks.de/servlet/SearchResults?' + urlencode({'isbn': query.isbn})
    if query.q.strip():
        return 'https://www.abebooks.de/servlet/SearchResults?' + urlencode({'tn': query.q.strip()})
    return None


def _medimops(query: PriceQuery) -> str | None:
    return ('https://www.medimops.de/produkte-C0/?'
            + urlencode({'fcIsSearch': '1', 'searchparam': query.best_text}))


def _rebuy(query: PriceQuery) -> str | None:
    return 'https://www.rebuy.de/kaufen/suchen?' + urlencode({'q': query.best_text})


def _kleinanzeigen(query: PriceQuery) -> str | None:
    return ('https://www.kleinanzeigen.de/s-suchanfrage.html?'
            + urlencode({'keywords': query.best_text}))


def _discogs(query: PriceQuery) -> str | None:
    if query.digits and not query.isbn:
        return ('https://www.discogs.com/de/search/?'
                + urlencode({'barcode': query.digits, 'type': 'release'}))
    if query.q.strip():
        return ('https://www.discogs.com/de/search/?'
                + urlencode({'q': query.q.strip(), 'type': 'release'}))
    return None


def _thalia(query: PriceQuery) -> str | None:
    return 'https://www.thalia.de/suche?' + urlencode({'sq': query.best_text})


def _mediamarkt(query: PriceQuery) -> str | None:
    return 'https://www.mediamarkt.de/de/search.html?' + urlencode({'query': query.best_text})


def _otto(query: PriceQuery) -> str | None:
    return f'https://www.otto.de/suche/{quote(query.best_text)}/'


PLATFORMS: list[Platform] = [
    # --- Price comparison / meta search -----------------------------------
    Platform(key='vialibri', label='ViaLibri', build=_vialibri, group='meta',
             kinds=('books',), condition='both', supports=('code',),
             note=_('Meta-Suche über hunderte Antiquariate & Gebraucht-Händler weltweit — '
                    'am genauesten mit ISBN.')),
    Platform(key='eurobuch', label='Eurobuch', build=_eurobuch, group='meta',
             kinds=('books',), condition='both', supports=('code',),
             note=_('Vergleicht neue & gebrauchte Angebote vieler Buchplattformen — braucht eine ISBN.')),
    Platform(key='idealo', label='idealo', build=_idealo, group='meta',
             condition='both', supports=('code',),
             note=_('Preisvergleich über viele Händler; EAN-Suche liefert exakte Treffer.')),
    Platform(key='geizhals', label='Geizhals', build=_geizhals, group='meta',
             kinds=('games', 'movies', 'music', ''), condition='new', supports=('code',),
             note=_('Preisvergleich mit Preisverlauf, stark bei Technik & Spielen.')),
    # --- Second-hand ------------------------------------------------------
    Platform(key='medimops', label='Medimops', build=_medimops, group='used',
             kinds=('books', 'movies', 'music', 'games'), condition='used', supports=('code',),
             note=_('Große Auswahl geprüfter Gebrauchtmedien; ISBN/EAN-Suche möglich.')),
    Platform(key='rebuy', label='reBuy', build=_rebuy, group='used',
             kinds=('books', 'movies', 'music', 'games', ''), condition='used', supports=('code',),
             note=_('Geprüfte Gebrauchtware mit Garantie — auch zum Verkaufen geeignet.')),
    Platform(key='booklooker', label='Booklooker', build=_booklooker, group='used',
             kinds=('books',), condition='used', supports=('code',),
             note=_('Marktplatz für gebrauchte & antiquarische Bücher.')),
    Platform(key='zvab', label='ZVAB', build=_zvab, group='used',
             kinds=('books',), condition='used', supports=('code',),
             note=_('Zentrales Verzeichnis antiquarischer Bücher.')),
    Platform(key='abebooks', label='AbeBooks', build=_abebooks, group='used',
             kinds=('books',), condition='used', supports=('code',),
             note=_('Internationale antiquarische Bücher (Schwester von ZVAB).')),
    Platform(key='discogs', label='Discogs', build=_discogs, group='used',
             kinds=('music',), condition='both', supports=('code',),
             note=_('DIE Datenbank + Marktplatz für Vinyl & CDs; Barcode-Suche möglich.')),
    # --- Marketplaces -----------------------------------------------------
    Platform(key='ebay', label='eBay', build=_ebay, group='market',
             condition='both', supports=('code', 'price', 'condition', 'sort'),
             note=_('Auktionen & Sofortkauf; Preisspanne, Zustand und Sortierung werden übernommen.')),
    Platform(key='kleinanzeigen', label='Kleinanzeigen', build=_kleinanzeigen, group='market',
             condition='used', supports=(),
             note=_('Privatangebote in deiner Nähe — Abholung spart Versand.')),
    # --- New --------------------------------------------------------------
    Platform(key='amazon', label='Amazon', build=_amazon, group='new',
             condition='both', supports=('code', 'price'),
             note=_('Suche in der passenden Kategorie; Preisspanne wird übernommen.')),
    Platform(key='thalia', label='Thalia', build=_thalia, group='new',
             kinds=('books', 'boardgames'), condition='new', supports=('code',),
             note=_('Bücher & Spiele neu; ISBN-Suche möglich.')),
    Platform(key='mediamarkt', label='MediaMarkt', build=_mediamarkt, group='new',
             kinds=('movies', 'music', 'games', ''), condition='new', supports=('code',),
             note=_('Neuware: Filme, Musik, Games & Technik.')),
    Platform(key='otto', label='OTTO', build=_otto, group='new',
             kinds=('', 'boardgames', 'games'), condition='new', supports=(),
             note=_('Großes Sortiment an Neuware.')),
]


def build_links(query: PriceQuery) -> list[dict]:
    """All matching platforms with their pre-filled search URLs.

    Returns dicts ``{platform, url, precise, group_label}`` — ``precise`` is
    True when the link searches by code (ISBN/EAN), i.e. hits exactly this
    edition instead of a text search.
    """
    if not query.has_query():
        return []
    results = []
    for platform in PLATFORMS:
        if not platform.matches(query):
            continue
        url = platform.build(query)
        if not url:
            continue
        results.append({
            'platform': platform,
            'url': url,
            'precise': bool(query.digits) and 'code' in platform.supports,
            'group_label': GROUP_LABELS.get(platform.group, platform.group),
        })
    # Stable presentation: group order as defined in GROUPS.
    order = {key: index for index, (key, _label) in enumerate(GROUPS)}
    results.sort(key=lambda entry: order.get(entry['platform'].group, 99))
    return results


class PriceSearchForm(forms.Form):
    """Filter form of the price search page (GET — the URL stays shareable)."""

    q = forms.CharField(
        required=False, label=_('Suchtext'),
        widget=forms.TextInput(attrs={'class': 'form-control',
                                      'placeholder': _('Titel, Autor/Interpret, Stichwörter …')}),
    )
    code = forms.CharField(
        required=False, label=_('ISBN / EAN / Barcode'),
        help_text=_('Am genauesten: findet exakt diese Ausgabe.'),
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '978… / 40…',
                                      'data-scan': 'ean'}),
    )
    kind = forms.ChoiceField(
        required=False, label=_('Art'), choices=MEDIA_KINDS,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    condition = forms.ChoiceField(
        required=False, label=_('Zustand'), choices=CONDITION_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    min_price = forms.DecimalField(
        required=False, label=_('Preis von'), min_value=0, decimal_places=2, max_digits=10,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
    )
    max_price = forms.DecimalField(
        required=False, label=_('Preis bis'), min_value=0, decimal_places=2, max_digits=10,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'min': '0'}),
    )
    year_from = forms.IntegerField(
        required=False, label=_('Jahr von'), min_value=0, max_value=2100,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'z. B. 1900',
                                        'min': '0', 'max': '2100'}),
    )
    year_to = forms.IntegerField(
        required=False, label=_('Jahr bis'), min_value=0, max_value=2100,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'z. B. 1950',
                                        'min': '0', 'max': '2100'}),
    )
    sort = forms.ChoiceField(
        required=False, label=_('Sortierung'), choices=SORT_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def to_query(self) -> PriceQuery:
        data = self.cleaned_data if self.is_valid() else {}
        return PriceQuery(
            q=(data.get('q') or '').strip(),
            code=(data.get('code') or '').strip(),
            kind=data.get('kind') or '',
            condition=data.get('condition') or '',
            min_price=data.get('min_price'),
            max_price=data.get('max_price'),
            sort=data.get('sort') or '',
            year_from=data.get('year_from'),
            year_to=data.get('year_to'),
        )


# Field types whose stored value can serve as the code for an item's search.
_CODE_FIELD_TYPES = {FieldType.ISBN, FieldType.BARCODE}
# Attributes whose mapped values sharpen the text query (creator names).
_CREATOR_ATTRIBUTES = ('authors', 'artist', 'director')


def query_for_item(collection, item) -> PriceQuery:
    """Best-possible pre-filled query for one item.

    Code: value of a field mapped to ISBN/EAN, else of any ISBN/barcode-typed
    field. Text: the title-mapped field (falling back to the ``name``/first
    required text field), refined with the creator (author/artist/director).
    """
    values = item.values or {}
    fields = list(collection.fields.all())

    def value_of(predicate) -> str:
        for fd in fields:
            if predicate(fd):
                value = values.get(fd.key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
        return ''

    code = value_of(lambda fd: (fd.config or {}).get('lookup_attribute') in QUERY_ATTRIBUTES)
    if not code:
        code = value_of(lambda fd: fd.field_type in _CODE_FIELD_TYPES)

    title = value_of(lambda fd: (fd.config or {}).get('lookup_attribute') == 'title')
    if not title:
        title = value_of(lambda fd: fd.key == 'name' and fd.field_type == FieldType.TEXT)
    if not title:
        title = value_of(lambda fd: fd.field_type == FieldType.TEXT)
    creator = value_of(
        lambda fd: (fd.config or {}).get('lookup_attribute') in _CREATOR_ATTRIBUTES)

    text = f'{title} {creator}'.strip()
    kind = collection.lookup_provider
    from .lookup_providers import VALID_MEDIA_KINDS
    return PriceQuery(q=text, code=code, kind=kind if kind in VALID_MEDIA_KINDS else '')
