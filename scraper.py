"""
Scrapers for UK personalised number plate dealer sites.

Each scraper accepts a plate/word string and returns a list of dicts:
    {plate, price, price_numeric, site, url}

price_numeric is a float (or float('inf') for POA) used for sorting.

For Cloudflare-protected sites we use curl_cffi which impersonates Chrome's
TLS fingerprint — no headless browser needed.
"""

import re
import json
import logging
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cf_requests
    _CF_SESSION = cf_requests.Session(impersonate='chrome124')
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    _CF_SESSION = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared session + helpers
# ---------------------------------------------------------------------------

_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-GB,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
}

_SESSION = requests.Session()
_SESSION.headers.update(_BROWSER_HEADERS)

# plates.co.uk needs its own session so cookies (XSRF) persist between requests
_PLATES_SESSION = requests.Session()
_PLATES_SESSION.headers.update(_BROWSER_HEADERS)

TIMEOUT = (2, 4)  # (connect, read) seconds


def _get(url: str, **kwargs) -> requests.Response | None:
    try:
        resp = _SESSION.get(url, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp
    except Exception as exc:
        logger.debug('GET %s → %s', url, exc)
        return None


def _cf_get(url: str, **kwargs) -> object | None:
    """GET using curl_cffi Chrome impersonation (bypasses Cloudflare). Falls
    back to plain requests if curl_cffi is unavailable."""
    if _CURL_CFFI_AVAILABLE:
        try:
            resp = _CF_SESSION.get(url, timeout=TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            logger.debug('CF GET %s → %s', url, exc)
            return None
    return _get(url, **kwargs)


def _parse_price_str(text: str | None) -> tuple[str, float | None]:
    """Return (display_string, numeric_pence_value_as_pounds)."""
    if not text:
        return 'POA', None
    text = str(text).strip()
    m = re.search(r'£\s*([\d,]+(?:\.\d+)?)', text)
    if m:
        val = float(m.group(1).replace(',', ''))
        display = f'£{int(val):,}' if val == int(val) else f'£{val:,.2f}'
        return display, val
    if re.search(r'poa|call|enquire|price on', text, re.I):
        return 'POA', float('inf')
    return text or 'POA', None


def _pence_to_display(pence: int) -> tuple[str, float]:
    """Convert pence integer to (display_str, float_pounds)."""
    pounds = pence / 100
    display = f'£{int(pounds):,}' if pounds == int(pounds) else f'£{pounds:,.2f}'
    return display, pounds


def _abs_url(href: str, base: str) -> str:
    if not href:
        return base
    if href.startswith('http'):
        return href
    root = re.match(r'(https?://[^/]+)', base)
    return (root.group(1) if root else '') + '/' + href.lstrip('/')


def _clean_plate(raw: str) -> str:
    return re.sub(r'\s+', ' ', raw.strip().upper())


# ---------------------------------------------------------------------------
# newreg.co.uk
# — URL: /number-plates/{plate_lower}/
# — Returns HTML; results are in .nr2-results as a list of <li> elements.
# — Each plate <li> contains: <a href="/number-plates/reg/…"> (plate text)
#   + a <span> with the green price + <a href="/buy-click?reg=…"> for buy URL.
# ---------------------------------------------------------------------------

def _search_newreg(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').lower()
    url = f'https://www.newreg.co.uk/number-plates/{key}/'
    resp = _get(url)
    if not resp:
        return results

    soup = BeautifulSoup(resp.text, 'lxml')
    nr2 = soup.select_one('.nr2-results')
    if not nr2:
        return results

    for li in nr2.find_all('li'):
        # Section headers are styled with a dark background and contain no
        # plate link — skip them.
        plate_links = li.select('a[href*="/number-plates/reg/"]')
        if not plate_links:
            continue

        plate_text = _clean_plate(plate_links[0].get_text())
        # The price is in a <span> that contains '£'
        price_text = ''
        for span in li.find_all('span'):
            txt = span.get_text(strip=True)
            if '£' in txt:
                price_text = txt
                break

        # Buy link
        buy_links = li.select('a[href*="/buy-click"]')
        if buy_links:
            buy_url = _abs_url(buy_links[0]['href'], 'https://www.newreg.co.uk')
        else:
            buy_url = _abs_url(plate_links[0]['href'], 'https://www.newreg.co.uk')

        if not plate_text:
            continue

        price_str, price_num = _parse_price_str(price_text)
        results.append({
            'plate': plate_text,
            'price': price_str,
            'price_numeric': price_num,
            'site': 'newreg.co.uk',
            'url': buy_url,
        })

    return results


# ---------------------------------------------------------------------------
# plates.co.uk
# — Uses Inertia.js; sending X-Inertia header returns JSON.
# — URL: /search/{keyword}
# — Response: { props: { results: [ { title, registrations: [{label,formatted,
#     date,price (pence),…}] } ] } }
# ---------------------------------------------------------------------------

_PLATES_INERTIA_VERSION = ''  # populated on first successful request


def _get_plates_version() -> str:
    global _PLATES_INERTIA_VERSION
    if _PLATES_INERTIA_VERSION:
        return _PLATES_INERTIA_VERSION
    try:
        resp = _PLATES_SESSION.get('https://plates.co.uk/', timeout=TIMEOUT)
        m = re.search(r'"version"\s*:\s*"([^"]+)"', resp.text)
        if m:
            _PLATES_INERTIA_VERSION = m.group(1)
    except Exception as exc:
        logger.debug('plates version fetch: %s', exc)
    return _PLATES_INERTIA_VERSION


def _search_plates_co_uk(plate: str) -> list[dict]:
    results: list[dict] = []
    version = _get_plates_version()
    key = plate.replace(' ', '').lower()
    url = f'https://plates.co.uk/search/{key}'
    try:
        resp = _PLATES_SESSION.get(url, timeout=TIMEOUT, headers={
            'Accept': 'application/json',
            'X-Inertia': 'true',
            'X-Inertia-Version': version,
        })
        resp.raise_for_status()
    except Exception as exc:
        logger.debug('plates search %s: %s', url, exc)
        return results
    if not resp:
        return results

    try:
        data = resp.json()
    except Exception as exc:
        logger.debug('plates json decode: %s', exc)
        return results

    props = data.get('props', {})

    # "exact" is a single exact-match plate object (may be null)
    exact = props.get('exact')
    if exact and isinstance(exact, dict) and exact.get('price'):
        price_str, price_num = _pence_to_display(exact['price'])
        label = exact.get('label', '')
        formatted = exact.get('formatted', label)
        results.append({
            'plate': _clean_plate(formatted or label),
            'price': price_str,
            'price_numeric': price_num,
            'site': 'plates.co.uk',
            'url': f'https://plates.co.uk/registration/{label.lower()}',
        })

    # "results" is a list of groups, each containing a list of registrations
    seen_labels: set[str] = set()
    for group in (props.get('results') or []):
        if not isinstance(group, dict):
            continue
        for reg in (group.get('registrations') or []):
            if not isinstance(reg, dict):
                continue
            label = reg.get('label', '')
            formatted = reg.get('formatted', label)
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            # Use `total` (incl. VAT + DVLA transfer fee) as the price to pay
            price_pence = reg.get('total') or reg.get('subtotal') or reg.get('price', 0)
            if not price_pence:
                continue
            price_str, price_num = _pence_to_display(price_pence)
            results.append({
                'plate': _clean_plate(formatted or label),
                'price': price_str,
                'price_numeric': price_num,
                'site': 'plates.co.uk',
                'url': f'https://plates.co.uk/registration/{label.lower()}',
            })

    return results


# ---------------------------------------------------------------------------
# choosemyreg.co.uk
# — Accessible with verify=False (SSL hostname mismatch on their cert).
# — Returns JSON via their API.
# ---------------------------------------------------------------------------

def _search_choosemyreg(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '')
    # Try their AJAX search API
    api_url = f'https://www.choosemyreg.co.uk/api/search?term={quote(key)}&type=exact'
    try:
        resp = _SESSION.get(api_url, timeout=TIMEOUT, verify=False,
                            headers={'Accept': 'application/json',
                                     'X-Requested-With': 'XMLHttpRequest'})
        if resp.status_code == 200 and 'json' in resp.headers.get('content-type', ''):
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or item.get('link') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'choosemyreg.co.uk',
                    'url': _abs_url(href, 'https://www.choosemyreg.co.uk'),
                })
            if results:
                return results
    except Exception as exc:
        logger.debug('choosemyreg api: %s', exc)

    # Fallback: HTML search page
    search_url = f'https://www.choosemyreg.co.uk/registration-plates?search={quote(key)}'
    try:
        resp2 = _SESSION.get(search_url, timeout=TIMEOUT, verify=False)
        if resp2.status_code == 200 and resp2.text:
            soup = BeautifulSoup(resp2.text, 'lxml')
            for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article'):
                plate_el = card.select_one('[class*="reg"], [class*="plate"]')
                price_el = card.select_one('[class*="price"]')
                link_el = card.select_one('a[href]')
                if not plate_el:
                    continue
                reg = _clean_plate(plate_el.get_text())
                if not reg or len(re.sub(r'\s', '', reg)) > 10:
                    continue
                price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
                href = link_el['href'] if link_el else ''
                results.append({
                    'plate': reg,
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'choosemyreg.co.uk',
                    'url': _abs_url(href, search_url),
                })
    except Exception as exc:
        logger.debug('choosemyreg html: %s', exc)

    return results


# ---------------------------------------------------------------------------
# privatereg.co.uk
# — POST form search
# ---------------------------------------------------------------------------

def _search_privatereg(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '')
    base = 'https://www.privatereg.co.uk'

    # Try their POST search form
    try:
        resp = _SESSION.post(
            f'{base}/index.cfm',
            data={'view': 'search', 'qsRegSearch': key, 'submit': 'Search'},
            headers={'Content-Type': 'application/x-www-form-urlencoded',
                     'Referer': f'{base}/'},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200 and len(resp.text) > 500:
            soup = BeautifulSoup(resp.text, 'lxml')
            for row in soup.select('tr, .result-row, [class*="result"]'):
                tds = row.find_all('td')
                if len(tds) >= 2:
                    reg_td = tds[0]
                    price_td = tds[-1]
                    reg = _clean_plate(reg_td.get_text())
                    if not reg or len(re.sub(r'\s', '', reg)) > 10:
                        continue
                    price_str, price_num = _parse_price_str(price_td.get_text())
                    link = row.find('a', href=True)
                    href = _abs_url(link['href'] if link else '', base)
                    results.append({
                        'plate': reg,
                        'price': price_str,
                        'price_numeric': price_num,
                        'site': 'privatereg.co.uk',
                        'url': href,
                    })
    except Exception as exc:
        logger.debug('privatereg: %s', exc)

    return results


# ---------------------------------------------------------------------------
# regtransfers.co.uk
# — Protected by Cloudflare; use curl_cffi impersonation.
# ---------------------------------------------------------------------------

def _search_regtransfers(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '')
    base = 'https://www.regtransfers.co.uk'

    # Try JSON API endpoints first
    for api_url in [
        f'{base}/api/search?q={quote(key)}&limit=20',
        f'{base}/number-plates/search/results?q={quote(key)}',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price') or item.get('sale_price'))
                href = item.get('url') or item.get('link') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'regtransfers.co.uk',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    # Fallback: HTML search page via Cloudflare bypass
    resp = _cf_get(f'{base}/number-plates/{quote(key.lower())}')
    if resp and resp.text:
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate-item"], [class*="reg-item"], .plate, article'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"], [class*="registration"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'regtransfers.co.uk',
                'url': _abs_url(href, base),
            })

    return results


# ---------------------------------------------------------------------------
# platehunter.com
# — Search via /search?term={plate}, returns JSON from their AJAX endpoint.
# ---------------------------------------------------------------------------

def _search_platehunter(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.platehunter.com'

    for api_url in [
        f'{base}/api/search?term={quote(key)}',
        f'{base}/ajax/search?q={quote(key)}',
        f'{base}/search/ajax?term={quote(key)}',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', data.get('data', [])))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or item.get('number') or ''
                price_str, price_num = _parse_price_str(item.get('price') or item.get('sale_price'))
                href = item.get('url') or item.get('link') or f'{base}/number-plates/{reg.lower().replace(" ", "")}'
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'platehunter.com',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    # HTML fallback
    resp = _cf_get(f'{base}/search?term={quote(key)}')
    if resp and resp.text:
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], article, .reg-plate'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"], [class*="number"]')
            price_el = card.select_one('[class*="price"], [class*="cost"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'platehunter.com',
                'url': _abs_url(href, base),
            })

    return results


# ---------------------------------------------------------------------------
# jamplates.co.uk
# — HTML search at /search/?q={plate}
# ---------------------------------------------------------------------------

def _search_jamplates(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.jamplates.co.uk'

    # Try JSON API
    for api_url in [
        f'{base}/api/search?q={quote(key)}',
        f'{base}/search/json?term={quote(key)}',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or item.get('link') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'jamplates.co.uk',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    # HTML fallback
    for search_url in [
        f'{base}/search/?q={quote(key)}',
        f'{base}/number-plates/{quote(key.lower())}',
    ]:
        resp = _cf_get(search_url)
        if not resp or not resp.text:
            continue
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article, li.plate'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"], [class*="number"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'jamplates.co.uk',
                'url': _abs_url(href, base),
            })
        if results:
            break

    return results


# ---------------------------------------------------------------------------
# carreg.co.uk
# — Has a documented REST-style search endpoint.
# ---------------------------------------------------------------------------

def _search_carreg(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.carreg.co.uk'

    for api_url in [
        f'{base}/api/search?registration={quote(key)}',
        f'{base}/search?registration={quote(key)}&format=json',
        f'{base}/number-plates/search?q={quote(key)}',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('registrations', data.get('plates', [])))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_raw = item.get('price') or item.get('sale_price') or item.get('cost') or ''
                price_str, price_num = _parse_price_str(str(price_raw) if price_raw else '')
                href = item.get('url') or item.get('link') or f'{base}/number-plates/{reg.lower()}'
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'carreg.co.uk',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    # HTML fallback
    resp = _cf_get(f'{base}/number-plates/{quote(key.lower())}')
    if not resp or not resp.text:
        resp = _cf_get(f'{base}/search-results?search={quote(key)}')
    if resp and resp.text:
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"], [class*="number"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'carreg.co.uk',
                'url': _abs_url(href, base),
            })

    return results


# ---------------------------------------------------------------------------
# myregplates.com
# — JSON API via /api endpoint, HTML fallback.
# ---------------------------------------------------------------------------

def _search_myregplates(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.myregplates.com'

    for api_url in [
        f'{base}/api/search?q={quote(key)}',
        f'{base}/search?q={quote(key)}&ajax=1',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or item.get('link') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'myregplates.com',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    # HTML fallback
    resp = _cf_get(f'{base}/search?q={quote(key)}')
    if resp and resp.text:
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'myregplates.com',
                'url': _abs_url(href, base),
            })

    return results


# ---------------------------------------------------------------------------
# onlyregistrations.co.uk
# — HTML search page.
# ---------------------------------------------------------------------------

def _search_onlyregistrations(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.onlyregistrations.co.uk'

    for api_url in [
        f'{base}/api/plates/search?q={quote(key)}',
        f'{base}/search?registration={quote(key)}&format=json',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'onlyregistrations.co.uk',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    for search_url in [
        f'{base}/number-plates/{quote(key.lower())}',
        f'{base}/search?q={quote(key)}',
        f'{base}/registrations/search?q={quote(key)}',
    ]:
        resp = _cf_get(search_url)
        if not resp or not resp.text:
            continue
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article, .registration'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"], [class*="number"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'onlyregistrations.co.uk',
                'url': _abs_url(href, base),
            })
        if results:
            break

    return results


# ---------------------------------------------------------------------------
# nationalplates.co.uk
# — HTML / AJAX search.
# ---------------------------------------------------------------------------

def _search_nationalplates(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.nationalplates.co.uk'

    for api_url in [
        f'{base}/api/search?q={quote(key)}',
        f'{base}/search?q={quote(key)}&ajax=true',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'nationalplates.co.uk',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    resp = _cf_get(f'{base}/number-plates/{quote(key.lower())}')
    if not resp or not resp.text:
        resp = _cf_get(f'{base}/search?term={quote(key)}')
    if resp and resp.text:
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'nationalplates.co.uk',
                'url': _abs_url(href, base),
            })

    return results


# ---------------------------------------------------------------------------
# regdirect.com
# — AJAX / HTML search.
# ---------------------------------------------------------------------------

def _search_regdirect(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.regdirect.com'

    for api_url in [
        f'{base}/api/search?q={quote(key)}',
        f'{base}/search?q={quote(key)}&format=json',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'regdirect.com',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    for search_url in [
        f'{base}/number-plates/{quote(key.lower())}',
        f'{base}/search?q={quote(key)}',
    ]:
        resp = _cf_get(search_url)
        if not resp or not resp.text:
            continue
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'regdirect.com',
                'url': _abs_url(href, base),
            })
        if results:
            break

    return results


# ---------------------------------------------------------------------------
# premiumplates.co.uk
# — AJAX / HTML search.
# ---------------------------------------------------------------------------

def _search_premiumplates(plate: str) -> list[dict]:
    results: list[dict] = []
    key = plate.replace(' ', '').upper()
    base = 'https://www.premiumplates.co.uk'

    for api_url in [
        f'{base}/api/search?q={quote(key)}',
        f'{base}/search?q={quote(key)}&ajax=1',
    ]:
        resp = _cf_get(api_url, headers={'Accept': 'application/json',
                                         'X-Requested-With': 'XMLHttpRequest'})
        if not resp:
            continue
        if 'json' not in resp.headers.get('content-type', ''):
            continue
        try:
            data = resp.json()
            items = data if isinstance(data, list) else data.get('results', data.get('plates', []))
            for item in items:
                if not isinstance(item, dict):
                    continue
                reg = item.get('registration') or item.get('reg') or item.get('plate') or ''
                price_str, price_num = _parse_price_str(item.get('price'))
                href = item.get('url') or ''
                results.append({
                    'plate': _clean_plate(reg),
                    'price': price_str,
                    'price_numeric': price_num,
                    'site': 'premiumplates.co.uk',
                    'url': _abs_url(href, base),
                })
            if results:
                return results
        except Exception:
            pass

    for search_url in [
        f'{base}/number-plates/{quote(key.lower())}',
        f'{base}/search?q={quote(key)}',
    ]:
        resp = _cf_get(search_url)
        if not resp or not resp.text:
            continue
        soup = BeautifulSoup(resp.text, 'lxml')
        for card in soup.select('[class*="result"], [class*="plate"], [class*="reg"], article'):
            plate_el = card.select_one('[class*="reg"], [class*="plate"]')
            price_el = card.select_one('[class*="price"]')
            link_el = card.select_one('a[href]')
            if not plate_el:
                continue
            reg = _clean_plate(plate_el.get_text())
            if not reg or len(re.sub(r'\s', '', reg)) > 10:
                continue
            price_str, price_num = _parse_price_str(price_el.get_text() if price_el else '')
            href = link_el['href'] if link_el else ''
            results.append({
                'plate': reg,
                'price': price_str,
                'price_numeric': price_num,
                'site': 'premiumplates.co.uk',
                'url': _abs_url(href, base),
            })
        if results:
            break

    return results


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_SCRAPERS: list[tuple[str, callable]] = [
    ('newreg.co.uk',              _search_newreg),
    ('plates.co.uk',              _search_plates_co_uk),
    ('choosemyreg.co.uk',         _search_choosemyreg),
    ('privatereg.co.uk',          _search_privatereg),
    ('regtransfers.co.uk',        _search_regtransfers),
    ('platehunter.com',           _search_platehunter),
    ('jamplates.co.uk',           _search_jamplates),
    ('carreg.co.uk',              _search_carreg),
    ('myregplates.com',           _search_myregplates),
    ('onlyregistrations.co.uk',   _search_onlyregistrations),
    ('nationalplates.co.uk',      _search_nationalplates),
    ('regdirect.com',             _search_regdirect),
    ('premiumplates.co.uk',       _search_premiumplates),
]


def search_all_sites(plate: str, scrapers=None) -> list[dict]:
    """Search every configured site for *plate*. Returns combined results."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

    scraper_list = scrapers if scrapers is not None else _SCRAPERS
    all_results: list[dict] = []

    # Don't use context manager — it waits for ALL threads on exit.
    # Instead shut down immediately after collecting results.
    pool = ThreadPoolExecutor(max_workers=len(scraper_list))
    futures = {pool.submit(fn, plate): site_name for site_name, fn in scraper_list}
    try:
        for future in as_completed(futures, timeout=5):
            site_name = futures[future]
            try:
                res = future.result()
                if res:
                    logger.info('%s → %d result(s) for %s', site_name, len(res), plate)
                    all_results.extend(res)
            except Exception as exc:
                logger.warning('%s failed for %s: %s', site_name, plate, exc)
    except FuturesTimeout:
        pass  # Accept whatever scrapers responded — slow ones are abandoned
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return all_results


# Fast scrapers used for background pool building — only the two quickest
_FAST_SCRAPERS = [
    ('newreg.co.uk',  _search_newreg),
    ('plates.co.uk',  _search_plates_co_uk),
]
