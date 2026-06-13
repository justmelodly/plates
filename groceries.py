"""
Live UK supermarket grocery price scraper.
Returns prices + direct product URLs for an ingredient search query.
Results are cached for 24 hours to avoid hammering sites.

Each scraper always returns a result dict with at least a search URL,
so links always work even when price extraction fails.
"""

import re
import time
import logging
import threading
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FTimeout

from bs4 import BeautifulSoup
from scraper import _cf_get, _SESSION

logger = logging.getLogger(__name__)

_cache: dict = {}
_CACHE_TTL = 86400  # 24 hours
_lock = threading.Lock()


def _cached_get(key: str):
    with _lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[1] < _CACHE_TTL:
            return entry[0]
        if entry:
            del _cache[key]
    return None


def _cached_set(key: str, data):
    with _lock:
        _cache[key] = (data, time.time())


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r'£\s*([\d]+(?:\.\d+)?)', str(text))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Tesco
# ---------------------------------------------------------------------------

def _search_tesco(query: str) -> dict:
    q = quote_plus(query)
    search_url = f"https://www.tesco.com/groceries/en-GB/search?query={q}"
    result = {'store': 'Tesco', 'price': None, 'price_str': None, 'url': search_url, 'product': query}
    try:
        resp = _cf_get(
            f"https://www.tesco.com/groceries/en-GB/resources/product-search?query={q}&count=5&offset=0",
            headers={'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest',
                     'Referer': 'https://www.tesco.com/groceries/en-GB/search'}
        )
        if not resp:
            return result
        data = resp.json()
        products = (
            data.get('productsByCategory', {}).get('data', {}).get('products', [])
            or data.get('uk', {}).get('ghs', {}).get('products', {}).get('results', [])
            or data.get('products', [])
        )
        if products:
            p = products[0]
            price_val = p.get('price', p.get('currentPrice'))
            pid = str(p.get('id', ''))
            name = p.get('name', query)
            if price_val is not None:
                price = float(price_val)
                result.update({
                    'price': price,
                    'price_str': f'£{price:.2f}',
                    'product': name,
                    'url': f'https://www.tesco.com/groceries/en-GB/products/{pid}' if pid else search_url,
                })
    except Exception as e:
        logger.debug('Tesco %r: %s', query, e)
    return result


# ---------------------------------------------------------------------------
# Sainsbury's
# ---------------------------------------------------------------------------

def _search_sainsburys(query: str) -> dict:
    q = quote_plus(query)
    search_url = f"https://www.sainsburys.co.uk/shop/gb/groceries/search-results/{q}"
    result = {'store': "Sainsbury's", 'price': None, 'price_str': None, 'url': search_url, 'product': query}
    try:
        resp = _cf_get(
            f"https://www.sainsburys.co.uk/gol-ui/api/products"
            f"?filter%5Bkeyword%5D={q}&page_size=5&page_number=1",
            headers={'Accept': 'application/json'}
        )
        if not resp:
            return result
        data = resp.json()
        products = data.get('products', [])
        if products:
            p = products[0]
            rp = p.get('retail_price', {})
            price_val = rp.get('price') if isinstance(rp, dict) else None
            name = p.get('name', query)
            href = p.get('full_url', '')
            if price_val is not None:
                price = float(price_val)
                result.update({
                    'price': price,
                    'price_str': f'£{price:.2f}',
                    'product': name,
                    'url': (f"https://www.sainsburys.co.uk{href}" if href else search_url),
                })
    except Exception as e:
        logger.debug("Sainsbury's %r: %s", query, e)
    return result


# ---------------------------------------------------------------------------
# Asda
# ---------------------------------------------------------------------------

def _search_asda(query: str) -> dict:
    q = quote_plus(query)
    search_url = f"https://groceries.asda.com/search/{q}"
    result = {'store': 'Asda', 'price': None, 'price_str': None, 'url': search_url, 'product': query}
    try:
        resp = _cf_get(
            f"https://groceries.asda.com/api/rest/search/products"
            f"?keyword={q}&store=0&size=5&page=1&sortBy=relevance",
            headers={'Accept': 'application/json', 'Store-Id': '0',
                     'Referer': 'https://groceries.asda.com/'}
        )
        if not resp:
            return result
        data = resp.json()
        items = (
            data.get('items')
            or data.get('results')
            or data.get('data', {}).get('items', [])
        )
        if items:
            p = items[0]
            price_val = p.get('price', p.get('listPrice'))
            name = p.get('name', query)
            pid = str(p.get('itemId', p.get('id', '')))
            if price_val is not None:
                price = float(price_val)
                result.update({
                    'price': price,
                    'price_str': f'£{price:.2f}',
                    'product': name,
                    'url': f'https://groceries.asda.com/product/{pid}' if pid else search_url,
                })
    except Exception as e:
        logger.debug('Asda %r: %s', query, e)
    return result


# ---------------------------------------------------------------------------
# Morrisons
# ---------------------------------------------------------------------------

def _search_morrisons(query: str) -> dict:
    q = quote_plus(query)
    search_url = f"https://groceries.morrisons.com/search?entry={q}"
    result = {'store': 'Morrisons', 'price': None, 'price_str': None, 'url': search_url, 'product': query}
    try:
        resp = _cf_get(search_url)
        if not resp or not resp.text:
            return result
        soup = BeautifulSoup(resp.text, 'lxml')
        product = soup.select_one(
            '[data-test="fop-product"], .fop-item, [class*="product-item"], '
            '[class*="product-pod"], li[class*="product"]'
        )
        if product:
            price_el = product.select_one(
                '[data-test="product-price"], .fop-price, [class*="price"], '
                '[class*="Price"]'
            )
            name_el  = product.select_one('.fop-title, h4, h3, [class*="title"]')
            link_el  = product.select_one('a[href]')
            if price_el:
                price = _parse_price(price_el.get_text(strip=True))
                if price is not None:
                    href = link_el['href'] if link_el else ''
                    result.update({
                        'price': price,
                        'price_str': f'£{price:.2f}',
                        'product': name_el.get_text(strip=True) if name_el else query,
                        'url': (
                            f"https://groceries.morrisons.com{href}"
                            if href and href.startswith('/') else (href or search_url)
                        ),
                    })
    except Exception as e:
        logger.debug('Morrisons %r: %s', query, e)
    return result


# ---------------------------------------------------------------------------
# Aldi
# ---------------------------------------------------------------------------

def _search_aldi(query: str) -> dict:
    """Aldi UK doesn't have a full online grocery shop — return site search link."""
    q = quote_plus(query)
    return {
        'store': 'Aldi',
        'price': None,
        'price_str': None,
        'url': f'https://www.aldi.co.uk/search?q={q}',
        'product': query,
    }


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_SCRAPERS = [
    _search_tesco,
    _search_sainsburys,
    _search_asda,
    _search_morrisons,
    _search_aldi,
]

STORE_ORDER = ['Tesco', "Sainsbury's", 'Asda', 'Morrisons', 'Aldi']


def search_ingredient_prices(query: str) -> list[dict]:
    """
    Search all UK supermarkets for a grocery ingredient.
    Returns a list of {store, price, price_str, url, product} dicts,
    one per supermarket, always in STORE_ORDER.
    """
    key = f'grocery:{query.lower().strip()}'
    cached = _cached_get(key)
    if cached is not None:
        return cached

    results_map: dict[str, dict] = {}

    pool = ThreadPoolExecutor(max_workers=len(_SCRAPERS))
    futures = {pool.submit(fn, query): fn for fn in _SCRAPERS}
    try:
        for future in as_completed(futures, timeout=10):
            try:
                r = future.result()
                if r:
                    results_map[r['store']] = r
            except Exception as exc:
                logger.debug('Grocery scraper error: %s', exc)
    except FTimeout:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Return in consistent store order, fill in any missing stores
    ordered = []
    for store in STORE_ORDER:
        if store in results_map:
            ordered.append(results_map[store])
        else:
            ordered.append({
                'store': store,
                'price': None,
                'price_str': None,
                'url': f'https://www.google.com/search?q={quote_plus(query + " " + store)}',
                'product': query,
            })

    _cached_set(key, ordered)
    return ordered
