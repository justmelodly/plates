"""
Facebook Marketplace scraper — Playwright-based.
"""

import re
import os
import glob
import shutil
import sqlite3
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _to_epoch_seconds(expiry) -> int:
    """Convert Firefox cookie expiry (which is in ms since epoch) to seconds for Playwright."""
    try:
        v = int(expiry)
    except (TypeError, ValueError):
        return -1
    if v <= 0:
        return -1
    # Firefox stores expiry in milliseconds; Playwright wants seconds
    if v > 2_000_000_000:
        v = v // 1000
    return v if v > 0 else -1


# Listings whose TITLE contains these are commercial/spam — filtered by default
SPAM_TITLE_PHRASES = [
    'home delivery',
    'free delivery',
    'next day delivery',
    'uk delivery',
    'delivered to your door',
    'fully assembled',
    'free shipping',
    'nationwide delivery',
    'delivered uk wide',
    'uk wide delivery',
]

# Car listing exclusions — checked against title + description
CAR_EXCLUSION_PHRASES = [
    'spares and repairs', 'spares or repairs', 'spares & repairs',
    'for spares', 'parts only', 'spares only',
    'breaking for parts', 'breaking only', 'breaking car',
    'non runner', 'non-runner', 'nonrunner',
    'does not run', "doesn't run", 'not running', 'no longer runs',
    'engine seized', 'engine blown', 'gearbox seized',
    'no mot', 'no m.o.t', 'cat a', 'cat b', 'cat c ', 'cat d ', 'cat n ', 'cat s ',
    'written off', 'write-off', 'salvage', 'accident damage',
    'fire damage', 'flood damage',
]

_SUPERCAR_MAKES = {
    'ferrari', 'lamborghini', 'mclaren', 'bugatti', 'pagani',
    'koenigsegg', 'rimac',
}
_PREMIUM_MAKES = {
    'porsche', 'aston martin', 'bentley', 'rolls-royce', 'rolls royce',
    'maserati', 'lotus', 'tvr', 'noble', 'radical', 'caterham', 'alpine',
}
_SPORTS_RE = re.compile(
    r'\b(bmw\s*m[2-8]|bmw\s*z[34]|bmw\s*i8'
    r'|mercedes[\s\-]?amg|mercedes[\s\-]?sls|c63|e63|gt63|slk\b|sl\b'
    r'|audi\s*r8|audi\s*rs\d|audi\s*tt'
    r'|jaguar\s*f.?type|jaguar\s*xkr?'
    r'|nissan\s*gt.?r|nissan\s*370z|nissan\s*350z'
    r'|toyota\s*supra|toyota\s*gr86|gt86|gr\s*yaris'
    r'|honda\s*nsx|honda\s*s2000'
    r'|mazda\s*mx.?5|mazda\s*rx.?[78]'
    r'|ford\s*mustang|ford\s*gt\b'
    r'|chevrolet\s*corvette'
    r'|subaru\s*sti|subaru\s*wrx|impreza\s*sti'
    r'|mitsubishi\s*evo|lancer\s*evo'
    r'|renault\s*clio\s*rs|megane\s*rs|megane\s*2[256]\d'
    r'|golf\s*r\b|golf\s*gti|polo\s*gti'
    r'|alpine\s*a110'
    r'|vauxhall\s*vxr8|monaro)\b',
    re.I,
)
_ALL_CAR_MAKES = [
    'Ferrari', 'Lamborghini', 'McLaren', 'Bugatti', 'Pagani', 'Koenigsegg', 'Rimac',
    'Porsche', 'Aston Martin', 'Bentley', 'Rolls-Royce', 'Rolls Royce', 'Maserati',
    'Lotus', 'TVR', 'Noble', 'Radical', 'Caterham', 'Alpine', 'Morgan', 'Ginetta',
    'BMW', 'Mercedes-Benz', 'Mercedes', 'Audi', 'Volkswagen', 'VW',
    'Ford', 'Vauxhall', 'Renault', 'Peugeot', 'Citroen', 'Toyota', 'Honda',
    'Nissan', 'Mazda', 'Subaru', 'Mitsubishi', 'Jaguar', 'Land Rover',
    'Range Rover', 'Mini', 'Alfa Romeo', 'Fiat', 'Skoda', 'Seat',
    'Volvo', 'Saab', 'Kia', 'Hyundai', 'Lexus', 'Tesla',
    'Dodge', 'Chevrolet', 'Cadillac', 'Jeep', 'Chrysler',
]


def classify_car(title: str, desc: str = '') -> str:
    text = (title + ' ' + (desc or '')).lower()
    for make in _SUPERCAR_MAKES:
        if make in text:
            return 'supercar'
    for make in _PREMIUM_MAKES:
        if make in text:
            return 'premium'
    if _SPORTS_RE.search(text):
        return 'sports'
    return 'standard'


def extract_car_make(title: str) -> Optional[str]:
    tl = title.lower()
    for make in sorted(_ALL_CAR_MAKES, key=len, reverse=True):
        if make.lower() in tl:
            return make
    return None


def is_excluded_car(title: str, desc: str) -> bool:
    text = (title + ' ' + (desc or '')).lower()
    return any(p in text for p in CAR_EXCLUSION_PHRASES)


DEFAULT_NO_OFFER_PHRASES = [
    "no offers",
    "no offer",
    "not accepting offers",
    "fixed price",
    "firm price",
    "price is firm",
    "no time wasters",
    "no low offers",
    "no silly offers",
    "asking price is final",
    "price not negotiable",
    "not negotiable",
    "no haggling",
    "price firm",
    "offers not accepted",
    "strict price",
    "won't accept offers",
    "will not accept offers",
    "no negotiations",
]

_PRICE_POUND  = re.compile(r'£\s*(\d[\d,]*(?:\.\d{1,2})?)', re.I)
_PRICE_SUFFIX = re.compile(r'\b(\d[\d,]+)\s*(?:ono|o\.n\.o\.?|pounds?|gbp|quid)\b', re.I)
_PRICE_ASKING = re.compile(r'\basking\s+(?:price\s+)?(?:of\s+)?£?\s*(\d[\d,]*)', re.I)


def extract_prices(text: str) -> list:
    prices = []
    for pat in (_PRICE_POUND, _PRICE_SUFFIX, _PRICE_ASKING):
        for m in pat.finditer(text):
            raw = m.group(1).replace(',', '')
            try:
                v = float(raw)
                if 1 <= v <= 1_000_000:
                    prices.append(v)
            except ValueError:
                pass
    return sorted(set(prices))


_PLATFORM_REDIRECT_PHRASES = [
    'whatsapp', 'whats app', 'what\'s app',
    'telegram', 'signal app',
    'snapchat', 'instagram dm', 'facebook messenger',
    'message me on', 'text me on', 'contact me on', 'reach me on',
    'dm me on', 'chat on', 'message on',
    'text only', 'no calls please', 'no phone calls',
    'call me on', 'ring me on', 'phone me on',
    'my number is', 'my no is', 'my num is',
]
_UK_MOBILE_RE = re.compile(r'\b(07\d{3}[\s\-]?\d{6}|\+44\s?7\d{3}[\s\-]?\d{6})\b')


def platform_redirect_flag(text: str) -> bool:
    """True if the listing description tries to move the buyer off Facebook."""
    lower = text.lower()
    if any(p in lower for p in _PLATFORM_REDIRECT_PHRASES):
        return True
    if _UK_MOBILE_RE.search(text):
        return True
    return False


def offer_status(text: str, phrases: list) -> str:
    lower = text.lower()
    if any(p.lower() in lower for p in phrases if p.strip()):
        return 'firm'
    positive = [
        'ono', 'o.n.o', 'open to offers', 'offers considered',
        'offers welcome', 'make an offer', 'offers accepted',
        'negotiable', 'willing to negotiate', 'best offer',
    ]
    if any(s in lower for s in positive):
        return 'negotiable'
    return 'unknown'


def _stealth_context(p, cookies=None, geolocation=None):
    browser = p.chromium.launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
        ],
    )
    geo = geolocation or {'latitude': 51.8997, 'longitude': -2.0771}  # Cheltenham default
    ctx = browser.new_context(
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/122.0.0.0 Safari/537.36'
        ),
        viewport={'width': 1280, 'height': 800},
        locale='en-GB',
        timezone_id='Europe/London',
        geolocation=geo,
        permissions=['geolocation'],
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
        "Object.defineProperty(navigator,'languages',{get:()=>['en-GB','en']});"
    )
    if cookies:
        ctx.add_cookies(cookies)
    return browser, ctx


def _remove_overlays(page) -> None:
    """
    Remove Facebook's cookie/login overlays from the DOM so the page is interactive.
    Removes: cookie consent dialogs, login-prompt modals, fixed overlay backdrops.
    """
    page.evaluate("""
    () => {
        // Remove all dialogs (cookie consent, login prompts)
        document.querySelectorAll('[role="dialog"]').forEach(el => el.remove());

        // Remove fixed/absolute overlays with high z-index that block clicks
        document.querySelectorAll('*').forEach(el => {
            const s = window.getComputedStyle(el);
            if ((s.position === 'fixed' || s.position === 'sticky') &&
                parseInt(s.zIndex || '0') > 100 &&
                el.offsetWidth > 200 && el.offsetHeight > 200) {
                // Skip the main nav and sidebar — only remove full-screen overlays
                if (el.offsetWidth > window.innerWidth * 0.5 &&
                    el.offsetHeight > window.innerHeight * 0.5) {
                    el.remove();
                }
            }
        });

        // Remove the scroll-lock Facebook adds when a modal is open
        document.body.style.overflow = '';
        document.documentElement.style.overflow = '';
    }
    """)
    page.wait_for_timeout(400)


def _firefox_cookie_paths() -> list:
    home = os.path.expanduser('~')
    patterns = [
        # Standard Linux Firefox
        os.path.join(home, '.mozilla', 'firefox', '*.default-release', 'cookies.sqlite'),
        os.path.join(home, '.mozilla', 'firefox', '*.default',         'cookies.sqlite'),
        os.path.join(home, '.mozilla', 'firefox', '*',                  'cookies.sqlite'),
        # Flatpak Firefox (Fedora default) — uses config/mozilla not .mozilla
        os.path.join(home, '.var', 'app', 'org.mozilla.firefox', 'config', 'mozilla', 'firefox', '*.default-release', 'cookies.sqlite'),
        os.path.join(home, '.var', 'app', 'org.mozilla.firefox', 'config', 'mozilla', 'firefox', '*', 'cookies.sqlite'),
        # Snap Firefox (Ubuntu)
        os.path.join(home, 'snap', 'firefox', 'common', '.mozilla', 'firefox', '*', 'cookies.sqlite'),
    ]
    found = []
    for pat in patterns:
        found.extend(glob.glob(pat))
    return list(dict.fromkeys(found))  # deduplicate, preserve order


def fb_grab_browser_cookies() -> dict:
    """
    Read Facebook session cookies from the user's Firefox profile on disk.
    The user must already be logged into Facebook in Firefox.
    """
    paths = _firefox_cookie_paths()
    if not paths:
        return {
            'success': False,
            'error': 'Firefox profile not found. Log into Facebook in Firefox, then try again.',
        }

    all_cookies: list = []
    for db_path in paths:
        tmp = db_path + '._fbscan_tmp'
        try:
            shutil.copy2(db_path, tmp)
            # Copy WAL + SHM so recent uncommitted cookies are included
            for ext in ('-wal', '-shm'):
                src = db_path + ext
                if os.path.exists(src):
                    shutil.copy2(src, tmp + ext)

            conn = sqlite3.connect(f'file:{tmp}?mode=ro&immutable=1', uri=True)
            rows = conn.execute(
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies WHERE host LIKE '%facebook.com'"
            ).fetchall()
            conn.close()
            for name, value, host, path_, expiry, secure, httponly, samesite in rows:
                # Keep the leading dot (e.g. '.facebook.com') so the cookie applies
                # to all subdomains (www.facebook.com, m.facebook.com, etc.)
                all_cookies.append({
                    'name':     name,
                    'value':    value,
                    'domain':   host,
                    'path':     path_ or '/',
                    'expires':  _to_epoch_seconds(expiry),
                    'secure':   bool(secure),
                    'httpOnly': bool(httponly),
                    'sameSite': 'Strict' if samesite == 2 else 'Lax' if samesite == 1 else 'None',
                })
        except Exception as exc:
            logger.debug('Cookie read error for %s: %s', db_path, exc)
        finally:
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.unlink(tmp + suffix)
                except OSError:
                    pass
        if all_cookies:
            break

    if not all_cookies:
        return {
            'success': False,
            'error': 'No Facebook cookies found. Open Firefox, log into Facebook, then click Connect again.',
        }

    # c_user cookie confirms the user is actually logged in
    if not any(c['name'] == 'c_user' for c in all_cookies):
        return {
            'success': False,
            'error': 'Firefox has Facebook cookies but you are not logged in. Log into Facebook in Firefox first.',
        }

    logger.info('Grabbed %d Facebook cookies from Firefox', len(all_cookies))
    return {'success': True, 'cookies': all_cookies}


def fb_open_login_browser(pending: dict, login_id: str) -> None:
    """
    Launch a visible (non-headless) Chromium window for manual login.
    Runs in a background thread. Updates pending[login_id] when done.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        pending[login_id] = {'status': 'error', 'error': 'Playwright not installed', 'cookies': None}
        return

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=['--no-sandbox', '--disable-setuid-sandbox'],
            )
            ctx = browser.new_context(viewport={'width': 1100, 'height': 750})
            page = ctx.new_page()
            page.goto('https://www.facebook.com/login', wait_until='networkidle', timeout=40_000)
            pending[login_id]['status'] = 'waiting'
            # Wait up to 5 minutes for user to log in manually
            page.wait_for_url(
                lambda url: 'facebook.com' in url and '/login' not in url and 'login.php' not in url,
                timeout=300_000,
            )
            page.wait_for_timeout(1500)
            cookies = ctx.cookies()
            pending[login_id] = {'status': 'success', 'cookies': cookies, 'error': None}
            browser.close()
    except Exception as exc:
        pending[login_id] = {'status': 'error', 'cookies': None, 'error': str(exc)}


_DEFAULT_LOCATION = 'Cheltenham, UK'
_DEFAULT_RADIUS_KM = 40

# Quick cache so repeated scans don't re-geocode
_geocode_cache: dict = {}


def _geocode(location: str) -> Optional[tuple]:
    """Return (lat, lon) for a location string via Nominatim. Returns None on failure."""
    key = location.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]
    try:
        import requests as _req
        resp = _req.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': location, 'format': 'json', 'limit': 1},
            headers={'User-Agent': 'PlatesApp/1.0 (personal use)'},
            timeout=6,
        )
        data = resp.json()
        if data:
            result = (float(data[0]['lat']), float(data[0]['lon']))
            _geocode_cache[key] = result
            return result
    except Exception as exc:
        logger.warning('Geocode failed for %r: %s', location, exc)
    return None


def fb_scan(
    cookies: list,
    query: str,
    location: str,
    radius_km: int,
    excluded_cities: list,
    min_price: Optional[float],
    max_price: Optional[float],
    condition: str,
    no_offer_phrases: list,
    car_mode: bool = False,
) -> dict:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return {'success': False, 'results': [], 'error': 'Playwright not installed'}

    if not query.strip():
        return {'success': False, 'results': [], 'error': 'Search query is required'}

    loc = (location or _DEFAULT_LOCATION).strip()
    logger.info('Scanning for %r near %s, radius %dkm', query, loc, radius_km)

    all_raw: list = []
    active_location: str = ''

    coords = _geocode(loc)
    geo = {'latitude': coords[0], 'longitude': coords[1]} if coords else None

    debug_info: dict = {
        'cookie_count': len(cookies),
        'has_c_user': any(c.get('name') == 'c_user' for c in cookies),
        'geolocation': geo,
        'url_requested': None,
        'url_resolved': None,
        'location_before': None,
        'location_typed': None,
        'suggestions': [],
        'suggestion_selected': None,
        'location_after': None,
    }

    # Four sort orders: same query, same location, different ranking each time.
    # Each gives a fresh 24-item DOM page; deduplication keeps only new items.
    _SORT_ORDERS = [
        'creation_time_descend',  # newest first  (default)
        'price_ascend',           # cheapest first
        'price_descend',          # most expensive first
        'distance_ascend',        # closest first
    ]
    _MAX_RESULTS = 100

    with sync_playwright() as p:
        browser, ctx = _stealth_context(p, cookies, geolocation=geo)
        page = ctx.new_page()

        try:
            # ── Pass 1: navigate, set location, scroll ──
            url = _build_url(query, loc, min_price, max_price, condition,
                             sort_by=_SORT_ORDERS[0])
            debug_info['url_requested'] = url
            logger.info('FB scan URL: %s', url)
            page.goto(url, wait_until='load', timeout=40_000)
            page.wait_for_timeout(3000)
            _remove_overlays(page)
            page.wait_for_timeout(500)

            debug_info['url_resolved'] = page.url
            logger.info('Resolved URL: %s', page.url)

            active_location, loc_debug = _set_location_ui(page, loc, radius_km)
            debug_info.update(loc_debug)
            page.wait_for_timeout(1500)

            def _scroll_and_extract():
                prev = 0
                stale = 0
                for _ in range(12):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(900)
                    cur = len(_extract_dom(page))
                    if cur > prev:
                        prev = cur
                        stale = 0
                    else:
                        stale += 1
                        if stale >= 3:
                            break
                return _extract_dom(page)

            batch = _scroll_and_extract()
            seen_ids: set = set()
            for item in batch:
                if item['id'] not in seen_ids:
                    seen_ids.add(item['id'])
                    all_raw.append(item)
            logger.info('Sort %s: %d unique items', _SORT_ORDERS[0], len(all_raw))

            # ── Passes 2-4: navigate to same query with different sort ──
            for sort_by in _SORT_ORDERS[1:]:
                if len(all_raw) >= _MAX_RESULTS:
                    break
                sort_url = _build_url(query, loc, min_price, max_price, condition,
                                      sort_by=sort_by)
                logger.info('Fetching sort order: %s', sort_by)
                page.goto(sort_url, wait_until='load', timeout=35_000)
                page.wait_for_timeout(2000)
                _remove_overlays(page)
                page.wait_for_timeout(500)

                batch = _scroll_and_extract()
                new_count = 0
                for item in batch:
                    if item['id'] not in seen_ids:
                        seen_ids.add(item['id'])
                        all_raw.append(item)
                        new_count += 1
                logger.info('Sort %s: +%d new (total %d)', sort_by, new_count, len(all_raw))

            logger.info('Final total: %d unique items | active_location: %s',
                        len(all_raw), active_location)

        except PWTimeout:
            logger.warning('Scan timeout')
        except Exception as exc:
            logger.warning('Scan error: %s', exc)
        finally:
            browser.close()

    result = _post_process(all_raw, excluded_cities, no_offer_phrases, car_mode=car_mode)
    result['active_location'] = active_location
    result['debug_info'] = debug_info
    return result


def _build_url(query, location, min_price, max_price, condition,
               sort_by='creation_time_descend'):
    import urllib.parse
    city_slug = location.split(',')[0].strip().lower().replace(' ', '-')
    params = {
        'query':  query,
        'sortBy': sort_by,
    }
    if min_price is not None:
        params['minPrice'] = str(int(min_price))
    if max_price is not None:
        params['maxPrice'] = str(int(max_price))
    if condition == 'new':
        params['itemCondition'] = 'new'
    elif condition == 'used':
        params['itemCondition'] = 'used_like_new,used_good,used_fair'
    return f'https://www.facebook.com/marketplace/{city_slug}/search/?{urllib.parse.urlencode(params)}'


def _set_location_ui(page, location_text: str, radius_km: int):
    """
    Open the Marketplace location dialog.
    Changes location if it doesn't match location_text, always updates radius.
    Returns (detected_location_str, debug_dict).
    """
    detected = ''
    dbg = {
        'location_before': None,
        'location_typed': None,
        'suggestions': [],
        'suggestion_selected': None,
        'location_after': None,
    }
    try:
        loc_btn = page.query_selector('[role="button"][aria-label*="Location:" i]')
        if not loc_btn:
            logger.warning('Location button not found on page')
            return detected, dbg

        current_label = (loc_btn.get_attribute('aria-label') or '').lower()
        detected = current_label
        dbg['location_before'] = current_label
        logger.info('Location before change: %s', current_label)

        city_want = location_text.split(',')[0].strip().lower()
        # If the target is a UK city, also require a UK keyword in the current label.
        # Without this, "cheltenham, nevada" would satisfy city_want="cheltenham" and
        # the code would skip the location change, leaving results stuck on NV.
        _uk_hint = any(kw in location_text.lower() for kw in ('uk', 'england', 'britain', 'gloucestershire', 'wales', 'scotland'))
        _uk_in_label = any(kw in current_label for kw in ('england', 'uk', 'united kingdom', 'wales', 'scotland', 'gloucestershire', 'yorkshire', 'midlands', 'lancashire', 'kent', 'surrey', 'essex', 'hampshire', 'devon', 'somerset', 'dorset', 'norfolk', 'suffolk', 'oxford', 'cambridge'))
        if _uk_hint:
            location_correct = city_want in current_label and _uk_in_label
        else:
            location_correct = city_want in current_label

        # Open the dialog
        loc_btn.click(force=True)
        page.wait_for_timeout(2000)

        # Verify dialog opened by checking for the location input
        loc_input = None
        for sel in ['input[aria-label="Location" i]', 'input[aria-label*="Location" i]', 'input[placeholder*="city" i]']:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el and el.is_visible():
                    loc_input = el
                    break
            except Exception:
                continue

        if not loc_input:
            logger.warning('Location dialog did not open — cannot change location/radius')
            return detected, dbg

        if not location_correct:
            logger.info('Changing location from %r to %r', current_label, location_text)

            city = location_text.split(',')[0].strip()
            country_hint = location_text.split(',')[-1].strip().upper() if ',' in location_text else ''
            # For UK cities, search just the city name — the UK geolocation bias
            # (set on the Playwright context) should surface the UK result first.
            # Typing "Cheltenham, Gloucestershire" sometimes returns zero suggestions
            # because Facebook's autocomplete doesn't recognise the county suffix.
            if country_hint in ('UK', 'GB', 'ENGLAND', 'UNITED KINGDOM'):
                search_text = city  # just "Cheltenham", let geolocation do the rest
            else:
                search_text = location_text
            dbg['location_typed'] = search_text
            logger.info('Typing in location dialog: %r', search_text)

            loc_input.click(click_count=3)
            page.wait_for_timeout(150)
            loc_input.press('Control+a')
            loc_input.press('Delete')
            page.wait_for_timeout(150)
            loc_input.type(search_text, delay=55)
            page.wait_for_timeout(3000)

            uk_keywords = ['gloucestershire', 'england', 'united kingdom', 'uk',
                           'wales', 'scotland', 'yorkshire', 'midlands', 'lancashire']
            us_keywords = ['nv', 'ca', 'tx', 'fl', 'ny', 'wa', 'or', 'az', 'co', 'ut',
                           'nevada', 'california', 'texas', 'florida', 'ohio', 'georgia']
            clicked = False
            try:
                options = page.query_selector_all('[role="option"]')
                all_suggestions = [(opt.text_content() or '').strip() for opt in options]
                dbg['suggestions'] = all_suggestions
                logger.info('Autocomplete options (%d): %s', len(all_suggestions), all_suggestions)

                # Pass 1 — prefer explicit UK keyword match
                for opt, txt_raw in zip(options, all_suggestions):
                    txt = txt_raw.lower()
                    if any(kw in txt for kw in uk_keywords):
                        opt.click()
                        detected = txt
                        dbg['suggestion_selected'] = txt_raw
                        logger.info('Selected UK suggestion (pass 1): %s', txt_raw)
                        clicked = True
                        break

                # Pass 2 — accept any suggestion that is NOT a known US state/location
                if not clicked:
                    for opt, txt_raw in zip(options, all_suggestions):
                        txt = txt_raw.lower()
                        # Split by comma to check the last part (state/country)
                        parts = [p.strip() for p in txt.split(',')]
                        last = parts[-1] if parts else ''
                        if not any(us_kw == last or us_kw in last for us_kw in us_keywords):
                            opt.click()
                            detected = txt
                            dbg['suggestion_selected'] = f'NON-US: {txt_raw}'
                            logger.info('Selected non-US suggestion (pass 2): %s', txt_raw)
                            clicked = True
                            break

                # Pass 3 — fall back to first result, log a warning
                if not clicked and options:
                    txt_raw = all_suggestions[0] if all_suggestions else ''
                    options[0].click()
                    detected = txt_raw.lower()
                    dbg['suggestion_selected'] = f'FALLBACK: {txt_raw}'
                    logger.warning('No UK suggestion found — selected first: %s', txt_raw)
                    clicked = True
            except Exception as exc:
                logger.warning('Suggestion selection error: %s', exc)

            if not clicked:
                page.keyboard.press('ArrowDown')
                page.wait_for_timeout(300)
                page.keyboard.press('Enter')
                dbg['suggestion_selected'] = 'keyboard-fallback'
                logger.info('Selected via keyboard fallback')

            page.wait_for_timeout(1000)

        _set_radius_ui(page, radius_km)

        for apply_sel in ['button:has-text("Apply")', 'button:has-text("Update")', 'button:has-text("Done")']:
            try:
                btn = page.query_selector(apply_sel)
                if btn and btn.is_visible():
                    btn.click()
                    logger.info('Clicked Apply')
                    break
            except Exception:
                continue

        page.wait_for_timeout(3000)

        try:
            new_btn = page.query_selector('[role="button"][aria-label*="Location:" i]')
            if new_btn:
                detected = (new_btn.get_attribute('aria-label') or '').lower()
                dbg['location_after'] = detected
                logger.info('Location after change: %s', detected)
        except Exception:
            pass

    except Exception as exc:
        logger.warning('Location UI failed: %s', exc)

    return detected, dbg


def _set_radius_ui(page, radius_km: int) -> None:
    """
    Set the search radius in the Marketplace location dialog.
    Facebook uses a React combobox (not a native select) — it's the last
    [role="combobox"] on the page when the dialog is open.
    Options: "1 kilometre", "2 kilometres", ..., "40 kilometres", etc.
    """
    fb_options = [1, 2, 5, 10, 20, 40, 60, 80, 100, 250, 500]
    target = min((r for r in fb_options if r >= radius_km), default=500)
    label = f'{target} kilometre' if target == 1 else f'{target} kilometres'

    try:
        # The radius combobox is the last [role="combobox"] — it has an empty aria-label
        combos = page.query_selector_all('[role="combobox"]')
        radius_combo = None
        for combo in reversed(list(combos)):
            aria = (combo.get_attribute('aria-label') or '').strip()
            if not aria:  # the radius one has no aria-label
                radius_combo = combo
                break

        if not radius_combo:
            logger.debug('Radius combobox not found')
            return

        radius_combo.click()
        page.wait_for_timeout(800)

        # Click the matching option
        option = page.locator(f'[role="option"]:has-text("{label}")')
        if option.count() > 0:
            option.first.click()
            page.wait_for_timeout(500)
            logger.info('Set radius to %s', label)
        else:
            page.keyboard.press('Escape')

    except Exception as exc:
        logger.debug('Radius UI failed (non-critical): %s', exc)


_EXTRACT_DOM_JS = r"""
() => {
    const results = [];
    const seen = new Set();
    document.querySelectorAll('a[href*="/marketplace/item/"]').forEach(link => {
        try {
            const href = link.getAttribute('href') || '';
            const m = href.match(/\/marketplace\/item\/(\d+)/);
            if (!m) return;
            const id = m[1];
            if (seen.has(id)) return;
            seen.add(id);

            const url = 'https://www.facebook.com/marketplace/item/' + id + '/';
            const img = link.querySelector('img');
            const image = img ? (img.src || '') : '';

            const allText = Array.from(link.querySelectorAll('span'))
                .map(s => s.textContent.trim()).filter(Boolean);

            let price = '', priceNum = null;
            for (const t of allText) {
                if (/^£[\d,]+/.test(t) || t === 'Free') {
                    price = t;
                    const pm = t.match(/£([\d,]+)/);
                    if (pm) priceNum = parseFloat(pm[1].replace(/,/g,''));
                    break;
                }
            }

            let title = '';
            for (const t of allText) {
                if (t.length > 4 && t !== price && !/^£/.test(t) && t !== 'Free') {
                    title = t; break;
                }
            }

            let location = '';
            for (const t of [...allText].reverse()) {
                if (t.length > 3 && t !== price && t !== title && !/^£/.test(t)
                        && t !== 'Free' && !/^\d+$/.test(t)) {
                    location = t; break;
                }
            }

            let listed = '';
            for (const t of allText) {
                if (/\b(minute|hour|day|week|month)s?\s+ago\b/i.test(t) || /\bjust now\b/i.test(t)) {
                    listed = t; break;
                }
            }

            const descEls = link.querySelectorAll('[data-testid*="description"]');
            const desc = Array.from(descEls).map(e => e.textContent.trim()).join(' ');

            results.push({ id, url, image, price, priceNum, title, location, listed, description: desc });
        } catch(e) {}
    });
    return results;
}
"""


def _extract_dom(page) -> list:
    return page.evaluate(_EXTRACT_DOM_JS) or []


def _parse_gql(body: dict, out: list) -> None:
    try:
        edges = (
            body.get('data', {})
            .get('marketplace_search', {})
            .get('feed_units', {})
            .get('edges', [])
        )
        for edge in edges:
            node = edge.get('node', {})
            listing = node.get('listing', {}) or node
            lid = str(listing.get('id', ''))
            if not lid:
                continue
            price_amount = listing.get('listing_price', {}).get('amount')
            price_str    = listing.get('listing_price', {}).get('formatted_amount', '')
            title        = listing.get('name', '') or listing.get('marketplace_listing_title', '')
            desc         = listing.get('description', '')
            if not isinstance(desc, str):
                desc = ''
            location = (
                listing.get('location', {})
                .get('reverse_geocode', {})
                .get('city', '')
            )
            image = (
                (listing.get('primary_listing_photo') or {})
                .get('image', {})
                .get('uri', '')
            )
            out.append({
                'id': lid,
                'url': f'https://www.facebook.com/marketplace/item/{lid}/',
                'image': image,
                'price': price_str,
                'priceNum': float(price_amount) if price_amount else None,
                'title': title,
                'location': location,
                'description': desc,
                'listed': '',
                '_from_gql': True,
            })
    except Exception:
        pass


def _merge_gql(dom_items: list, gql_items: list) -> None:
    gql_map = {g['id']: g for g in gql_items}
    for item in dom_items:
        gql = gql_map.get(item.get('id', ''))
        if gql:
            if not item.get('description') and gql.get('description'):
                item['description'] = gql['description']
            if not item.get('location') and gql.get('location'):
                item['location'] = gql['location']
    dom_ids = {i.get('id') for i in dom_items}
    for g in gql_items:
        if g['id'] not in dom_ids:
            dom_items.append(g)


def _post_process(raw: list, excluded_cities: list, no_offer_phrases: list,
                  car_mode: bool = False) -> dict:
    seen: set = set()
    results = []
    excl = [c.lower() for c in excluded_cities if c.strip()]

    for r in raw:
        lid = r.get('id', '')
        if not lid or lid in seen:
            continue
        seen.add(lid)

        title = r.get('title') or ''
        title_lower = title.lower()

        # Filter commercial/spam listings by title
        if any(p in title_lower for p in SPAM_TITLE_PHRASES):
            continue

        location = (r.get('location') or '').lower()
        if excl and any(c in location for c in excl):
            continue

        desc = r.get('description') or ''

        # Car mode: skip spares/non-runners
        if car_mode and is_excluded_car(title, desc):
            continue

        r['offer_status'] = offer_status(desc, no_offer_phrases)
        r['desc_prices']  = extract_prices(desc)

        price_n = r.pop('priceNum', None)
        r['price_numeric'] = price_n

        if r['desc_prices'] and price_n:
            if min(r['desc_prices']) < price_n * 0.95:
                r['price_gap_flag'] = True

        r['platform_redirect'] = platform_redirect_flag(desc)
        r['is_free'] = (r.get('price', '').strip().lower() == 'free'
                        or price_n == 0)

        if car_mode:
            r['car_type'] = classify_car(title, desc)
            r['car_make'] = extract_car_make(title)

        results.append(r)

    return {'success': True, 'results': results, 'total': len(results)}
