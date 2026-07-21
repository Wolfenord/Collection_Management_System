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

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import urlencode

from . import lookup_providers
from .price_search import PriceQuery

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Offer:
    """One concrete listing found on a platform."""

    title: str
    price: Decimal | None
    currency: str = 'EUR'
    condition: str = ''          # human text, '' if unknown
    seller: str = ''             # platform / marketplace name
    url: str = ''                # link to the offer/listing
    cover: str = ''              # optional thumbnail URL

    @property
    def price_display(self) -> str:
        if self.price is None:
            return ''
        return f'{self.price:.2f} {self.currency}'


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
            seller=f'Discogs · {num} Angebot(e)',
            url=f'https://www.discogs.com/sell/release/{release_id}?ev=rb',
            cover=str(result.get('thumb') or ''),
        ))
        if len(offers) >= limit:
            break
    return offers


OFFER_PROVIDERS: list[OfferProvider] = [
    OfferProvider(key='discogs', label='Discogs', fetch=_discogs,
                  kinds=('music',), needs_setting='discogs_token'),
]


def active_providers(query: PriceQuery) -> list[OfferProvider]:
    return [p for p in OFFER_PROVIDERS if p.available() and p.matches(query)]


def fetch_offers(query: PriceQuery, *, limit_per_provider: int = 8,
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
            return provider.fetch(query, limit_per_provider) or []
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
    return offers
