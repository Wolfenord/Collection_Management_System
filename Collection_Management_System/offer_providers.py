"""Live inline offers for the price search (ViaLibri-style aggregated listings).

Unlike :mod:`price_search` (which only builds deep *links* into platforms and
never contacts them), this module fetches **real offers** server-side and shows
them inline: title, condition, price, seller and a link to buy.

Responsible-use guardrails (this is opt-in and off by default):
  * Gated behind the ``live_offers_enabled`` runtime setting (staff toggle).
  * Only providers backed by an **official API** with the operator's own access
    token are shipped (currently Discogs). API terms and rate limits apply — the
    operator supplies their own credentials via runtime settings.
  * Results are **cached** (see ``fetch_offers``) and the per-user call rate is
    throttled by the view, so the page can't be turned into a request cannon.
  * Every provider is fully defensive: any network/parse error yields ``[]`` and
    the link-out cards (price_search.PLATFORMS) remain as a fallback.
  * Prices are labelled "ohne Gewähr" in the UI and links open with
    ``rel="noopener noreferrer nofollow"``.

Adding a platform = one :class:`OfferProvider` in :data:`OFFER_PROVIDERS`. Each
receives a :class:`price_search.PriceQuery` and returns a list of :class:`Offer`.
A book/antiquarian provider (ViaLibri-like) plugs in the same way once an API
key or a maintained scraper is available.
"""

from __future__ import annotations

import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import quote, urlencode

from django.utils.translation import gettext_lazy as _

from . import lookup_providers
from .price_search import PriceQuery

log = logging.getLogger(__name__)


@dataclass
class Offer:
    """One concrete listing found on a platform."""

    title: str
    price: Decimal | None
    currency: str = 'EUR'
    condition: str = ''          # human text, '' if unknown
    seller: str = ''             # dealer / marketplace name
    url: str = ''                # link to the offer/listing
    cover: str = ''              # optional thumbnail URL
    platform: str = ''           # which site it was found on (Booklooker, …)
    # Rich bibliographic detail (filled where the source provides it).
    author: str = ''
    year: str = ''
    publisher: str = ''
    binding: str = ''            # Taschenbuch / Gebunden / …
    description: str = ''        # dealer's free-text note about the copy
    shipping: str = ''           # shipping cost note, e.g. "zzgl. 3,10 € Versand"
    # Other platforms the *same* offer was found on, as (platform, url) pairs
    # (filled by deduplication) — each is a link to the same offer elsewhere.
    also_on: list[tuple[str, str]] = field(default_factory=list)

    @property
    def price_display(self) -> str:
        if self.price is None:
            return ''
        return f'{self.price:.2f} {self.currency}'

    @property
    def meta_line(self) -> str:
        """Compact 'Autor · Verlag, Jahr · Einband' line for the UI."""
        parts = []
        if self.author:
            parts.append(self.author)
        pub = ', '.join(p for p in (self.publisher, self.year) if p)
        if pub:
            parts.append(pub)
        if self.binding:
            parts.append(self.binding)
        return ' · '.join(parts)


@dataclass(frozen=True)
class OfferProvider:
    """One platform we can fetch live offers from."""

    key: str
    label: str
    fetch: Callable[[PriceQuery, int], list[Offer]]
    kinds: tuple[str, ...] = ()   # () = every media kind
    needs_setting: str = ''       # runtime setting that must be truthy to run

    def available(self) -> bool:
        if not self.needs_setting:
            return True
        from .runtime_settings import get_setting
        return bool(get_setting(self.needs_setting))

    def matches(self, query: PriceQuery) -> bool:
        # No category chosen (kind == '') → search everything (ViaLibri style).
        if not query.kind:
            return True
        return not self.kinds or query.kind in self.kinds


def _to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


# --- Discogs (music): official API, operator-supplied personal token ----------

_DISCOGS_API = 'https://api.discogs.com'
_DISCOGS_RELEASES = 4  # how many top releases to price-check per search


def _discogs_token() -> str:
    from .runtime_settings import get_setting
    return (get_setting('discogs_token') or '').strip()


def _discogs(query: PriceQuery, limit: int) -> list[Offer]:
    token = _discogs_token()
    if not token:
        return []
    # Prefer an exact barcode search when we have a code, else free text.
    params = {'type': 'release', 'token': token, 'per_page': _DISCOGS_RELEASES}
    if query.digits:
        params['barcode'] = query.digits
    elif query.q.strip():
        params['q'] = query.q.strip()
    else:
        return []

    search = lookup_providers._http_get_json(f'{_DISCOGS_API}/database/search?{urlencode(params)}')
    if not isinstance(search, dict):
        return []

    offers: list[Offer] = []
    for result in (search.get('results') or [])[:_DISCOGS_RELEASES]:
        release_id = result.get('id')
        if not release_id:
            continue
        stats = lookup_providers._http_get_json(
            f'{_DISCOGS_API}/marketplace/stats/{release_id}?'
            + urlencode({'token': token, 'curr_abbr': 'EUR'}))
        if not isinstance(stats, dict) or not stats.get('num_for_sale'):
            continue
        lowest = stats.get('lowest_price') or {}
        price = _to_decimal(lowest.get('value'))
        if price is None:
            continue
        num = stats.get('num_for_sale')
        offers.append(Offer(
            title=str(result.get('title') or '').strip() or query.best_text,
            price=price,
            currency=str(lowest.get('currency') or 'EUR'),
            condition='',
            seller=f'{num} Angebot(e) ab',
            url=f'https://www.discogs.com/sell/release/{release_id}?ev=rb',
            cover=str(result.get('thumb') or ''),
            platform='Discogs',
        ))
        if len(offers) >= limit:
            break
    return offers


# --- Booklooker (books): HTML scrape of the public ISBN/title results ---------
#
# ViaLibri-style real offers for books. Booklooker renders its results
# server-side, so we can fetch and parse them without a browser. The parser is
# deliberately structural-but-forgiving: each offer on the page carries exactly
# one ``<span class='price'>…&euro;</span>``, preceded by its title, condition
# and (optionally) a thumbnail — so we segment the page on the price markers and
# read each offer's fields out of the segment that precedes it. Any layout
# change simply yields fewer/zero offers (never an error), and the link-out card
# to Booklooker remains as a fallback.

_BL_PRICE = re.compile(r"<span class='price'>\s*([\d.]*\d,\d{2})\s*&nbsp;&euro;")
_BL_TITLE = re.compile(r'<span class="articleTitleLink">(.*?)</span>', re.S)
_BL_LINK = re.compile(r"href='(/B%C3%BCcher/[^']*?/id/[^']*)'")
_BL_COND = re.compile(r'Zustand:\s*([^<\n]+)')
_BL_IMG = re.compile(r"(https://images\.booklooker\.de/[^'\"]+?\.jpg)")
_BL_HEADLINE = re.compile(r"<h3 class='seo-headline notranslate'>(.*?)</h3>", re.S)
# Booklooker prints the year right after the publisher headline: "…</h3>, 1980."
_BL_YEAR = re.compile(r'</h3>\s*,\s*(1[4-9]\d\d|20\d\d)')
_YEAR = re.compile(r'\b(1[4-9]\d\d|20\d\d)\b')


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', fragment or '')).strip()


def _parse_booklooker(body: str, limit: int) -> list[Offer]:
    if not body:
        return []
    offers: list[Offer] = []
    prev = 0
    for match in _BL_PRICE.finditer(body):
        segment = body[prev:match.start()]
        prev = match.end()
        price = _to_decimal(match.group(1).replace('.', '').replace(',', '.'))
        if price is None:
            continue
        titles = _BL_TITLE.findall(segment)
        links = _BL_LINK.findall(segment)
        conditions = _BL_COND.findall(segment)
        images = _BL_IMG.findall(segment)
        headlines = [_text(h) for h in _BL_HEADLINE.findall(segment)]
        title = _text(titles[-1]) if titles else ''
        if not title:
            continue
        # Booklooker lists author then publisher as two <h3 class="seo-headline">.
        author = headlines[0] if headlines else ''
        publisher = headlines[1] if len(headlines) > 1 else ''
        year_match = _BL_YEAR.search(segment)
        offers.append(Offer(
            title=title,
            price=price,
            currency='EUR',
            condition=_text(conditions[-1])[:60] if conditions else '',
            seller='',
            url=('https://www.booklooker.de' + links[-1]) if links else
                'https://www.booklooker.de/',
            cover=images[0] if images else '',
            platform='Booklooker',
            author=author[:120],
            publisher=publisher[:120],
            year=year_match.group(1) if year_match else '',
        ))
        if len(offers) >= limit:
            break
    return offers


def _booklooker(query: PriceQuery, limit: int) -> list[Offer]:
    if query.isbn:
        url = f'https://www.booklooker.de/B%C3%BCcher/Angebote/isbn={quote(query.isbn)}'
    elif query.q.strip():
        url = f'https://www.booklooker.de/B%C3%BCcher/Angebote/titel={quote(query.q.strip())}'
    else:
        return []
    return _parse_booklooker(lookup_providers._http_get_text(url), limit)


# --- AbeBooks & ZVAB (antiquarian, used & new books, manuscripts) -------------
#
# Both are the same platform (AbeBooks Inc.) and embed schema.org JSON-LD for
# every listing — a stable, structured source (name, price, currency, condition,
# real dealer name, url). We parse the JSON-LD instead of the obfuscated CSS, so
# the parser is robust against layout changes. Because the two sites share the
# same dealer network they often return the *same* offers — deduplication (see
# ``fetch_offers``) collapses those, keyed by (title, dealer, price).

_CONDITION_MAP = {
    'newcondition': _('neu'),
    'usedcondition': _('gebraucht'),
    'refurbishedcondition': _('generalüberholt'),
    'damagedcondition': _('beschädigt'),
}


_BINDING_MAP = {
    'paperback': _('Taschenbuch'),
    'hardcover': _('Gebunden'),
    'ebook': _('E-Book'),
    'audiobookformat': _('Hörbuch'),
}


def _schema_condition(value: str) -> str:
    key = (value or '').rstrip('/').rsplit('/', 1)[-1].lower()
    return str(_CONDITION_MAP.get(key, ''))


def _binding_label(book_format: str) -> str:
    key = (book_format or '').rstrip('/').rsplit('/', 1)[-1].lower()
    return str(_BINDING_MAP.get(key, ''))


def _publisher_year(publisher_name: str) -> tuple[str, str]:
    """Split AbeBooks' "Ort : Verlag [Jahr]." into (publisher, year)."""
    raw = _text(publisher_name)
    year = ''
    match = _YEAR.search(raw)
    if match:
        year = match.group(1)
    # Drop the bracketed year and any leading place(s) before the last ':'.
    without_year = re.sub(r'[\[\(]?\b(1[4-9]\d\d|20\d\d)\b[\]\).,]*', '', raw)
    publisher = without_year.split(':')[-1].strip(' ,.;-')
    return publisher[:120], year


# Per-listing HTML enrichment (image, detailed condition, shipping, description).
# AbeBooks/ZVAB wrap each listing in data-test-id="listing-item-<sku>", and the
# JSON-LD offer carries the same sku — so we join the structured JSON with the
# richer HTML fields by sku. All of this is best-effort: missing pieces are just
# left blank.
_AB_ITEM = re.compile(r'data-test-id="listing-item-(\d+)"')
_AB_COND = re.compile(r'listing-book-condition-\d+"[^>]*>(.*?)</', re.S)
_AB_SHIP = re.compile(r'item-shipping-price-\d+"[^>]*>(.*?)</', re.S)
_AB_DESC = re.compile(r'description-\d+"[^>]*>(.*?)</p>', re.S)
_AB_IMG = re.compile(r'<img[^>]+(?:data-src|src)="(https://pictures\.[^"]+?\.jpg)"')


def _abebooks_blocks(body: str) -> dict[str, str]:
    """Map each listing's sku -> its HTML block (from one listing-item to the next)."""
    marks = [(m.group(1), m.start()) for m in _AB_ITEM.finditer(body)]
    blocks: dict[str, str] = {}
    for index, (sku, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(body)
        blocks.setdefault(sku, body[start:end])
    return blocks


def _iter_ld_nodes(data):
    """Yield the flat product/offer nodes from a parsed JSON-LD document (in
    document order), unwrapping @graph and ItemList structures."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)
    elif isinstance(data, dict):
        if isinstance(data.get('@graph'), list):
            for item in data['@graph']:
                yield from _iter_ld_nodes(item)
        elif isinstance(data.get('itemListElement'), list):
            for element in data['itemListElement']:
                yield from _iter_ld_nodes(element.get('item', element))
        else:
            yield data


def _parse_schema_offers(body: str, platform: str, limit: int) -> list[Offer]:
    """Parse AbeBooks/ZVAB results: schema.org JSON-LD for the structured core,
    enriched per listing (by sku) with image, detailed condition, shipping and
    the dealer's free-text note from the HTML."""
    if not body:
        return []
    blocks = _abebooks_blocks(body)
    offers: list[Offer] = []
    for match in re.finditer(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body, re.S):
        try:
            data = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _iter_ld_nodes(data):
            offer_node = node.get('offers') if isinstance(node, dict) else None
            if not isinstance(offer_node, dict):
                continue
            price = _to_decimal(offer_node.get('price'))
            if price is None:
                continue
            seller = ''
            seller_node = offer_node.get('seller')
            if isinstance(seller_node, dict):
                seller = str(seller_node.get('name') or '').strip()
            author = ''
            author_node = node.get('author')
            if isinstance(author_node, dict):
                author = _text(str(author_node.get('name') or '')).rstrip(':').strip()
            publisher_node = node.get('publisher')
            publisher, year = ('', '')
            if isinstance(publisher_node, dict):
                publisher, year = _publisher_year(str(publisher_node.get('name') or ''))
            image = node.get('image')
            if isinstance(image, list):
                image = image[0] if image else ''

            # Enrich from the matching HTML listing block.
            sku = str(offer_node.get('sku') or '')
            block = blocks.get(sku, '')
            condition = _schema_condition(offer_node.get('itemCondition', ''))
            shipping = description = ''
            if block:
                cond_match = _AB_COND.search(block)
                if cond_match:
                    condition = _text(cond_match.group(1)).replace('Zustand:', '').strip() or condition
                ship_match = _AB_SHIP.search(block)
                if ship_match:
                    shipping = _text(ship_match.group(1))
                desc_match = _AB_DESC.search(block)
                if desc_match:
                    description = _text(desc_match.group(1))[:280]
                if not image:
                    img_match = _AB_IMG.search(block)
                    if img_match:
                        image = img_match.group(1)

            offers.append(Offer(
                title=_text(str(node.get('name') or ''))[:150],
                price=price,
                currency=str(offer_node.get('priceCurrency') or 'EUR'),
                condition=condition,
                seller=seller,
                url=str(offer_node.get('url') or node.get('url') or ''),
                cover=str(image or ''),
                platform=platform,
                author=author[:120],
                publisher=publisher,
                year=year,
                binding=_binding_label(str(node.get('bookFormat') or '')),
                description=description,
                shipping=shipping,
            ))
            if len(offers) >= limit:
                return offers
    return offers


def _abebooks_like(base: str, platform: str, query: PriceQuery, limit: int) -> list[Offer]:
    if query.isbn:
        url = f'{base}/servlet/SearchResults?isbn={quote(query.isbn)}&sortby=17'
    elif query.q.strip():
        url = f'{base}/servlet/SearchResults?kn={quote(query.q.strip())}&sortby=17'
    else:
        return []
    return _parse_schema_offers(lookup_providers._http_get_text(url), platform, limit)


def _abebooks(query: PriceQuery, limit: int) -> list[Offer]:
    return _abebooks_like('https://www.abebooks.de', 'AbeBooks', query, limit)


def _zvab(query: PriceQuery, limit: int) -> list[Offer]:
    return _abebooks_like('https://www.zvab.com', 'ZVAB', query, limit)


OFFER_PROVIDERS: list[OfferProvider] = [
    OfferProvider(key='discogs', label='Discogs', fetch=_discogs,
                  kinds=('music',), needs_setting='discogs_token'),
    OfferProvider(key='booklooker', label='Booklooker', fetch=_booklooker,
                  kinds=('books',), needs_setting='book_offers_enabled'),
    OfferProvider(key='abebooks', label='AbeBooks', fetch=_abebooks,
                  kinds=('books',), needs_setting='book_offers_enabled'),
    OfferProvider(key='zvab', label='ZVAB', fetch=_zvab,
                  kinds=('books',), needs_setting='book_offers_enabled'),
]


def active_providers(query: PriceQuery) -> list[OfferProvider]:
    return [p for p in OFFER_PROVIDERS if p.available() and p.matches(query)]


def fetch_offers(query: PriceQuery, *, limit_per_provider: int = 40,
                 timeout: int | None = None) -> list[Offer]:
    """All live offers for a query, from every available provider, price-sorted.

    Providers run in parallel; each is fully isolated (its errors never break the
    page). Callers should cache the result (see the view) — this makes real
    outbound requests.
    """
    if not query.has_query():
        return []
    providers = active_providers(query)
    if not providers:
        return []

    if timeout is None:
        from .runtime_settings import get_setting
        timeout = get_setting('lookup_timeout')

    def run(provider: OfferProvider) -> list[Offer]:
        # Hand the resolved timeout to the worker thread so it never touches the
        # DB/cache itself (same pattern as lookup_providers' parallel search).
        lookup_providers._thread_timeout.value = timeout
        try:
            result = provider.fetch(query, limit_per_provider) or []
            log.info('Offer provider %s returned %s offer(s)', provider.key, len(result))
            return result
        except Exception:  # noqa: BLE001 — a bad provider must not break the page
            log.warning('Offer provider %s failed', provider.key, exc_info=True)
            return []
        finally:
            lookup_providers._thread_timeout.value = None

    offers: list[Offer] = []
    with ThreadPoolExecutor(max_workers=min(4, len(providers))) as pool:
        futures = {pool.submit(run, p): p for p in providers}
        for fut in as_completed(futures):
            offers.extend(fut.result())

    offers.sort(key=lambda o: (o.price is None, o.price or Decimal(0)))
    return _deduplicate(offers)


def _norm(text: str) -> str:
    """Lowercase, keep only alphanumerics — for fuzzy title/seller matching."""
    return re.sub(r'[^a-z0-9]+', '', (text or '').lower())


def _deduplicate(offers: list[Offer]) -> list[Offer]:
    """Collapse the *same* offer found on several platforms into one row.

    ViaLibri-style: antiquarian networks (AbeBooks, ZVAB, …) syndicate the same
    dealer's listing, so an identical (dealer, price, title) tuple appearing on
    two sites is one offer. We keep the first (cheapest, already sorted) and note
    the other platforms in ``also_on``. Offers without a dealer name (e.g. a
    marketplace aggregate) are keyed by (platform, title, price) so genuinely
    distinct listings are never merged away.
    """
    kept: list[Offer] = []
    seen: dict[tuple, Offer] = {}
    for offer in offers:
        price_key = str(offer.price)
        if offer.seller:
            key = (_norm(offer.seller), price_key, _norm(offer.title)[:40])
        else:
            key = (_norm(offer.platform), _norm(offer.title)[:40], price_key)
        existing = seen.get(key)
        if existing is None:
            seen[key] = offer
            kept.append(offer)
        elif offer.platform and offer.platform != existing.platform \
                and offer.platform not in [p for p, _u in existing.also_on]:
            existing.also_on.append((offer.platform, offer.url))
    return kept
