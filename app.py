"""
UK Number Plate Word Finder
Flask backend — served on localhost:8080
"""

import os
import re
import json
import time
import secrets
import threading
import logging
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from bs4 import BeautifulSoup

from wordmatch import word_to_plate_patterns, plate_to_words, is_english_word, load_dictionary, plate_match_score
from scraper import search_all_sites, _cf_get, _abs_url, _FAST_SCRAPERS
from meals import RECIPES, SUPERMARKETS
from groceries import search_ingredient_prices, STORE_ORDER
from fb_scanner import (
    fb_grab_browser_cookies as _fb_grab_cookies,
    fb_open_login_browser   as _fb_open_browser,
    fb_scan                 as _fb_scan,
    DEFAULT_NO_OFFER_PHRASES,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static')
CORS(app)

# ---------------------------------------------------------------------------
# Cache — keyed by normalised search term, TTL = 10 minutes
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[list, float]] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 600

# FB Marketplace sessions: token -> {cookies, created}
_fb_sessions: dict = {}
_fb_sessions_lock = threading.Lock()

# Pending open-browser logins: login_id -> {status, cookies, error}
_fb_pending: dict = {}
_fb_pending_lock = threading.Lock()


def _cache_get(key: str) -> list | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[1] < CACHE_TTL:
            return entry[0]
        if entry:
            del _cache[key]
    return None


def _cache_set(key: str, data: list) -> None:
    with _cache_lock:
        _cache[key] = (data, time.time())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    resp = send_from_directory('static', 'index.html')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/search')
def api_search():
    """Search for plates matching a word.

    ?q=WORD      - search by English word (generates all plate encodings)
    ?plate=AB12  - search for a specific plate across all sites
    """
    word = request.args.get('q', '').strip().upper()
    exact_plate = request.args.get('plate', '').strip().upper()

    if not word and not exact_plate:
        return jsonify({'error': 'Provide ?q=WORD or ?plate=AB12', 'results': []}), 400

    if word:
        word = re.sub(r'[^A-Z0-9]', '', word)
        if len(word) < 2:
            return jsonify({'error': 'Enter at least 2 letters', 'results': []}), 400
        if len(word) > 8:
            return jsonify({'error': 'Maximum 8 letters', 'results': []}), 400
        cache_key = f'word:{word}'
    else:
        exact_plate = re.sub(r'[^A-Z0-9]', '', exact_plate)
        cache_key = f'plate:{exact_plate}'

    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info('cache hit for %s', cache_key)
        return jsonify({'results': cached, 'from_cache': True})

    # -----------------------------------------------------------------------
    # Collect plate patterns to search
    # -----------------------------------------------------------------------
    if word:
        patterns = word_to_plate_patterns(word)
        patterns = patterns[:8]
        logger.info('Searching %d patterns for word %s', len(patterns), word)
    else:
        patterns = [exact_plate]

    # -----------------------------------------------------------------------
    # Fan out scrapes in parallel — one thread per pattern per site would
    # be too many; instead scrape all sites for each pattern sequentially
    # across patterns in parallel.
    # -----------------------------------------------------------------------
    all_results: list[dict] = []
    results_lock = threading.Lock()

    def scrape_pattern(pattern: str) -> None:
        found = search_all_sites(pattern)
        with results_lock:
            all_results.extend(found)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(scrape_pattern, patterns))

    # -----------------------------------------------------------------------
    # Deduplicate by normalised plate string
    # -----------------------------------------------------------------------
    seen: set[str] = set()
    unique: list[dict] = []
    search_word = word or exact_plate
    for r in all_results:
        key = re.sub(r'\s', '', r.get('plate', '')).upper()
        if key and key not in seen and len(key) >= 2:
            seen.add(key)
            possible_words = [w for w in plate_to_words(r['plate']) if is_english_word(w)]
            r['matches'] = sorted(set(possible_words))
            r['match_score'] = plate_match_score(r['plate'], search_word)
            unique.append(r)

    def _match_tier(score):
        if score >= 90: return 0
        if score >= 70: return 1
        if score >= 40: return 2
        if score >   0: return 3
        return 4

    # Default sort: match tier (exact first), then cheapest within tier
    unique.sort(key=lambda r: (
        _match_tier(r.get('match_score', 0)),
        r.get('price_numeric') or float('inf'),
    ))

    _cache_set(cache_key, unique)
    logger.info('Returning %d unique results for %s', len(unique), cache_key)
    return jsonify({'results': unique, 'from_cache': False, 'patterns_searched': len(patterns)})


@app.route('/api/meal-planner/recipes')
def api_meal_recipes():
    return jsonify({'recipes': RECIPES, 'supermarkets': SUPERMARKETS})


@app.route('/api/meal-planner/prices')
def api_ingredient_prices():
    """Return live supermarket prices for a grocery ingredient query.

    ?q=beef+mince  →  [{store, price, price_str, url, product}, …]
    """
    query = request.args.get('q', '').strip()
    if not query or len(query) > 100:
        return jsonify({'error': 'Provide ?q=ingredient (max 100 chars)', 'results': []}), 400
    results = search_ingredient_prices(query)
    return jsonify({'results': results, 'stores': STORE_ORDER})


# ---------------------------------------------------------------------------
# FB Marketplace Scanner
# ---------------------------------------------------------------------------

@app.route('/api/fb-scanner/login', methods=['POST'])
def api_fb_login():
    """Grab Facebook cookies from the user's local Firefox profile."""
    result = _fb_grab_cookies()
    if not result['success']:
        return jsonify(result), 401

    token = secrets.token_hex(32)
    with _fb_sessions_lock:
        _fb_sessions[token] = {'cookies': result['cookies'], 'created': time.time()}

    resp = jsonify({'success': True})
    resp.set_cookie('fb_sess', token, httponly=True, samesite='Lax', max_age=86400)
    return resp


@app.route('/api/fb-scanner/login/open-browser', methods=['POST'])
def api_fb_open_browser():
    """Launch a visible browser window for manual Facebook login."""
    login_id = secrets.token_hex(8)
    with _fb_pending_lock:
        _fb_pending[login_id] = {'status': 'opening', 'cookies': None, 'error': None}
    threading.Thread(
        target=_fb_open_browser,
        args=(_fb_pending, login_id),
        daemon=True,
    ).start()
    return jsonify({'id': login_id})


@app.route('/api/fb-scanner/login/poll')
def api_fb_login_poll():
    """Poll whether an open-browser login has completed."""
    login_id = request.args.get('id', '')
    with _fb_pending_lock:
        state = _fb_pending.get(login_id)
    if not state:
        return jsonify({'status': 'not_found'}), 404

    if state['status'] == 'success':
        token = secrets.token_hex(32)
        with _fb_sessions_lock:
            _fb_sessions[token] = {'cookies': state['cookies'], 'created': time.time()}
        with _fb_pending_lock:
            _fb_pending.pop(login_id, None)
        resp = jsonify({'status': 'success'})
        resp.set_cookie('fb_sess', token, httponly=True, samesite='Lax', max_age=86400)
        return resp

    return jsonify({'status': state['status'], 'error': state.get('error')})


@app.route('/api/fb-scanner/logout', methods=['POST'])
def api_fb_logout():
    token = request.cookies.get('fb_sess')
    if token:
        with _fb_sessions_lock:
            _fb_sessions.pop(token, None)
    resp = jsonify({'success': True})
    resp.delete_cookie('fb_sess')
    return resp


@app.route('/api/fb-scanner/status')
def api_fb_status():
    token = request.cookies.get('fb_sess')
    with _fb_sessions_lock:
        logged_in = bool(token and token in _fb_sessions)
    return jsonify({'logged_in': logged_in})


@app.route('/api/fb-scanner/scan', methods=['POST'])
def api_fb_scan_route():
    token = request.cookies.get('fb_sess')
    with _fb_sessions_lock:
        session = _fb_sessions.get(token) if token else None
    if not session:
        return jsonify({'success': False, 'error': 'Not connected to Facebook', 'auth': False}), 401

    # Always re-read cookies fresh from Firefox so we never use a stale session.
    # The fb_sess token just proves the user went through Connect; the actual
    # Facebook cookies are read live each time to pick up any new cookies Firefox has.
    fresh = _fb_grab_cookies()
    if fresh.get('success'):
        cookies = fresh['cookies']
        with _fb_sessions_lock:
            _fb_sessions[token]['cookies'] = cookies
    else:
        cookies = session['cookies']

    data = request.get_json(force=True, silent=True) or {}

    def _f(key):
        try:
            v = data.get(key)
            return float(v) if v not in (None, '', 0) else None
        except (ValueError, TypeError):
            return None

    try:
        radius_km = int(data.get('radius_km') or 40)
    except (ValueError, TypeError):
        radius_km = 40

    result = _fb_scan(
        cookies=cookies,
        query=(data.get('query') or '').strip(),
        location=(data.get('location') or 'Cheltenham, UK').strip(),
        radius_km=radius_km,
        excluded_cities=[c.strip() for c in (data.get('excluded_cities') or []) if c.strip()],
        min_price=_f('min_price'),
        max_price=_f('max_price'),
        condition=data.get('condition', 'any'),
        no_offer_phrases=data.get('no_offer_phrases') or DEFAULT_NO_OFFER_PHRASES,
    )
    return jsonify(result)


@app.route('/api/car-finder/scan', methods=['POST'])
def api_car_finder_scan():
    """UK-wide car search with automatic spares/non-runner exclusion."""
    token = request.cookies.get('fb_sess')
    with _fb_sessions_lock:
        session = _fb_sessions.get(token) if token else None
    if not session:
        return jsonify({'success': False, 'error': 'Not connected to Facebook', 'auth': False}), 401

    # Always re-read fresh cookies
    fresh = _fb_grab_cookies()
    if fresh.get('success'):
        cookies = fresh['cookies']
        with _fb_sessions_lock:
            _fb_sessions[token]['cookies'] = cookies
    else:
        cookies = session['cookies']

    data = request.get_json(force=True, silent=True) or {}

    def _f(key):
        try:
            v = data.get(key)
            return float(v) if v not in (None, '', 0) else None
        except (ValueError, TypeError):
            return None

    query = (data.get('query') or 'supercar').strip()

    result = _fb_scan(
        cookies=cookies,
        query=query,
        location='London, UK',   # UK-wide: London + 500km covers all of GB
        radius_km=500,
        excluded_cities=[],
        min_price=_f('min_price'),
        max_price=_f('max_price'),
        condition='used',
        no_offer_phrases=[],
        car_mode=True,
    )
    return jsonify(result)


@app.route('/api/status')
def api_status():
    dict_size = len(load_dictionary())
    return jsonify({'status': 'ok', 'dictionary_words': dict_size})


# ---------------------------------------------------------------------------
# Featured plates
# ---------------------------------------------------------------------------

import random as _random

# Standard UK current-format plate: AB12 CDE — not personalised, skip them
_STANDARD_PLATE_RE = re.compile(r'^[A-Z]{2}[0-9]{2}[A-Z]{3}$')

_FEATURED_WORDS = [
    'BOSS', 'VIP', 'KING', 'ACE', 'HERO', 'BEAST', 'WOLF', 'DUKE', 'FAST', 'GOLD',
    'RIDE', 'RACE', 'FIRE', 'DART', 'RUSH', 'BOLT', 'LION', 'HAWK', 'APEX', 'BLAZE',
    'TANK', 'RAGE', 'STAR', 'CASH', 'FLEX', 'GRIP', 'ICON', 'JADE', 'LUXE', 'MATE',
]
_FEATURED_POOL_TTL = 1800  # rebuild pool every 30 minutes

_featured_pool: list[dict] = []
_featured_pool_ts: float = 0.0
_featured_pool_lock = threading.Lock()
_featured_pool_building = False


def _build_featured_pool() -> None:
    global _featured_pool, _featured_pool_ts, _featured_pool_building
    with _featured_pool_lock:
        if _featured_pool_building:
            return
        _featured_pool_building = True

    try:
        all_results: list[dict] = []
        results_lock = threading.Lock()

        # One top pattern per word — keeps scrape count low
        patterns: list[tuple[str, str]] = []
        for word in _FEATURED_WORDS:
            pats = word_to_plate_patterns(word)
            if pats:
                patterns.append((pats[0], word))

        def scrape_one(pw: tuple[str, str]) -> None:
            pattern, word = pw
            try:
                # Use only fast scrapers — don't compete with live user searches
                found = search_all_sites(pattern, scrapers=_FAST_SCRAPERS)
                with results_lock:
                    for r in found:
                        r['match_score'] = plate_match_score(r['plate'], word)
                        all_results.append(r)
            except Exception as exc:
                logger.debug('Featured scrape %s: %s', pattern, exc)

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(scrape_one, patterns))

        seen: set[str] = set()
        unique: list[dict] = []
        for r in all_results:
            key = re.sub(r'\s', '', r.get('plate', '')).upper()
            if not key or key in seen or len(key) < 2:
                continue
            if _STANDARD_PLATE_RE.match(key):
                continue
            seen.add(key)
            possible_words = [w for w in plate_to_words(r['plate']) if is_english_word(w)]
            r['matches'] = sorted(set(possible_words))
            unique.append(r)

        with _featured_pool_lock:
            _featured_pool = unique
            _featured_pool_ts = time.time()
            logger.info('Featured pool built: %d personalised plates', len(unique))
    except Exception as exc:
        logger.warning('Featured pool build error: %s', exc)
    finally:
        with _featured_pool_lock:
            _featured_pool_building = False


@app.route('/api/featured')
def api_featured():
    with _featured_pool_lock:
        pool = list(_featured_pool)
        ts = _featured_pool_ts
        building = _featured_pool_building

    # Trigger background rebuild if pool is empty or stale
    if not building and (not pool or time.time() - ts > _FEATURED_POOL_TTL):
        threading.Thread(target=_build_featured_pool, daemon=True).start()

    if not pool:
        return jsonify({'results': [], 'loading': True})

    sample = _random.sample(pool, min(12, len(pool)))
    return jsonify({'results': sample, 'loading': False})


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

_NEWS_CACHE_KEY = '__news__'
_NEWS_TTL = 900  # 15 minutes


def _fetch_plate_news() -> list[dict]:
    queries = [
        'UK personalised number plates DVLA registration',
        'private number plate cherished registration UK',
    ]
    items: list[dict] = []
    seen: set[str] = set()

    for q in queries:
        url = (
            f'https://news.google.com/rss/search?q={q.replace(" ", "+")}'
            '&hl=en-GB&gl=GB&ceid=GB:en'
        )
        try:
            import requests as _req
            resp = _req.get(url, timeout=12, headers={'User-Agent': 'Mozilla/5.0'})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for item in root.iter('item'):
                raw_title = item.findtext('title', '')
                # Google appends " - Source Name" — strip it
                title = re.sub(r'\s*-\s*[^-]+$', '', raw_title).strip()
                link = item.findtext('link', '')
                if not title or title in seen:
                    continue
                seen.add(title)
                desc_html = item.findtext('description', '')
                desc = BeautifulSoup(desc_html, 'lxml').get_text()[:260] if desc_html else ''
                pub = item.findtext('pubDate', '')
                source_el = item.find('source')
                source = source_el.text if source_el is not None else re.sub(r'.* - ', '', raw_title)
                items.append({
                    'title': title,
                    'url': link,
                    'description': desc,
                    'published': pub,
                    'source': source,
                })
        except Exception as exc:
            logger.warning('News RSS %r: %s', q, exc)

    return items[:24]


@app.route('/api/news')
def api_news():
    with _cache_lock:
        entry = _cache.get(_NEWS_CACHE_KEY)
        if entry and time.time() - entry[1] < _NEWS_TTL:
            return jsonify({'articles': entry[0], 'from_cache': True})
    articles = _fetch_plate_news()
    with _cache_lock:
        _cache[_NEWS_CACHE_KEY] = (articles, time.time())
    return jsonify({'articles': articles})


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------

_FRIENDS_CACHE_KEY = '__friends__'
_FRIENDS_TTL = 600  # 10 minutes


def _scrape_torqwales() -> dict:
    base = 'https://www.torqwales.co.uk'
    out: dict = {'platform': 'website', 'url': base, 'available': False, 'sections': []}
    try:
        resp = _cf_get(base)
        if not resp:
            return out
        soup = BeautifulSoup(resp.text, 'lxml')

        # Pull meta before stripping anything
        name_meta = soup.find('meta', property='og:site_name')
        desc_meta = soup.find('meta', property='og:description')
        out.update({
            'available': True,
            'name': (name_meta or {}).get('content', 'Torqwales'),
            'description': (desc_meta or {}).get('content', ''),
        })

        # Strip navigation, header, footer, cart, form noise
        for tag in soup.select(
            'header, footer, nav, form, noscript, script, style, '
            '[data-section-type="header"], [data-section-type="footer"], '
            '[data-section-type="index"], '
            '.Cart, .cart, [id*="cart"], [class*="cart"], '
            '[class*="header-nav"], [class*="mobile-bar"], '
            '[class*="skip"], [aria-label="skip"]'
        ):
            tag.decompose()

        # Also strip form inputs, selects, buttons — they add size/cart noise
        for tag in soup.select('select, option, input, button, label, [class*="product-price"], [class*="quantity"]'):
            tag.decompose()

        # Sections to skip entirely (not useful on a friends page)
        _SKIP_TITLES = {'contact us', 'follow us on social media', 'follow us'}

        sections = []
        for sec in soup.select('[data-section-id]'):
            heading_el = sec.select_one('h1, h2, h3, h4')
            title = heading_el.get_text(strip=True) if heading_el else ''

            if title.lower() in _SKIP_TITLES:
                continue

            # Collect <p> text — skip short fragments and size-option noise
            paras = []
            seen_p: set[str] = set()
            for p in sec.select('p'):
                txt = p.get_text(strip=True)
                if (len(txt) > 20
                        and txt not in seen_p
                        and not re.search(r'\bIn\s+Sizes?\s+[XS]', txt, re.I)):
                    seen_p.add(txt)
                    paras.append(txt)
            body = ' '.join(paras)[:400]

            # Fallback: get_text line-by-line, drop obvious noise
            if not body:
                lines = [
                    ln for ln in sec.get_text('\n', strip=True).splitlines()
                    if len(ln) > 25 and not re.match(r'^[A-Z0-9\s\(\)/£]{1,30}$', ln)
                ]
                body = ' '.join(lines)[:400]

            # Best image in section (prefer one with a meaningful alt)
            img_url = ''
            img_alt = ''
            for img in sec.select('img[src]'):
                src = img.get('src', '')
                alt = img.get('alt', '')
                if src:
                    img_url = src
                    img_alt = alt
                    if alt:  # stop at first image that has descriptive alt text
                        break

            # CTA links — external URLs or internal non-shop pages
            cta_links = []
            seen_l: set[str] = set()
            for a in sec.select('a[href]'):
                href = a['href']
                label = a.get_text(strip=True)
                if (href and label and len(label) > 2
                        and href not in seen_l
                        and not re.search(r'/shop|/cart|javascript', href, re.I)):
                    seen_l.add(href)
                    cta_links.append({'label': label[:60], 'url': href})

            # Skip decoration-only sections (hero image with no useful text)
            if not body and not title:
                continue


            # Give untitled content sections a sensible heading
            if not title and body:
                title = 'About'

            if body or img_url:
                sections.append({
                    'title': title,
                    'body': body,
                    'image': img_url,
                    'image_alt': img_alt,
                    'links': cta_links[:2],
                })

        out['sections'] = sections
    except Exception as exc:
        logger.debug('torqwales scrape: %s', exc)
    return out


def _fetch_tiktok_profile() -> dict:
    out: dict = {
        'platform': 'tiktok',
        'url': 'https://www.tiktok.com/@torqwales',
        'available': False,
    }
    try:
        resp = _cf_get('https://www.tiktok.com/@torqwales')
        if not resp:
            return out
        soup = BeautifulSoup(resp.text, 'lxml')
        script = soup.find('script', id='__UNIVERSAL_DATA_FOR_REHYDRATION__')
        if not script:
            return out
        data = json.loads(script.string)
        ud = data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {})
        user  = ud.get('userInfo', {}).get('user', {})
        stats = ud.get('userInfo', {}).get('stats', {})
        out.update({
            'available': True,
            'name': user.get('nickname', 'TorqWales'),
            'handle': f'@{user.get("uniqueId", "torqwales")}',
            'bio': user.get('signature', ''),
            'avatar': user.get('avatarMedium', ''),
            'followers': stats.get('followerCount', 0),
            'following': stats.get('followingCount', 0),
            'likes': stats.get('heartCount', 0),
            'videos': stats.get('videoCount', 0),
        })
    except Exception as exc:
        logger.debug('TikTok profile: %s', exc)
    return out


def _fetch_instagram_profile() -> dict:
    out: dict = {
        'platform': 'instagram',
        'url': 'https://www.instagram.com/torqwales/',
        'available': False,
    }
    try:
        resp = _cf_get('https://www.instagram.com/torqwales/')
        if not resp:
            return out
        soup = BeautifulSoup(resp.text, 'lxml')
        desc_meta  = soup.find('meta', {'name': 'description'}) or soup.find('meta', property='og:description')
        title_meta = soup.find('meta', property='og:title')
        img_meta   = soup.find('meta', property='og:image')
        desc  = (desc_meta  or {}).get('content', '')
        title = (title_meta or {}).get('content', '')
        m = re.search(r'([\d,]+)\s+Followers?,\s*([\d,]+)\s+Following,\s*([\d,]+)\s+Posts?', desc)
        bio = re.sub(r'^[^-]+-\s*', '', desc).strip() if ' - ' in desc else desc
        out.update({
            'available': True,
            'name': re.sub(r'\s*[@(•].*', '', title).strip(),
            'handle': '@torqwales',
            'bio': bio,
            'avatar': (img_meta or {}).get('content', ''),
            'followers': int(m.group(1).replace(',', '')) if m else 0,
            'following': int(m.group(2).replace(',', '')) if m else 0,
            'posts': int(m.group(3).replace(',', '')) if m else 0,
        })
    except Exception as exc:
        logger.debug('Instagram profile: %s', exc)
    return out


def _fetch_linktree_profile() -> dict:
    out: dict = {
        'platform': 'linktree',
        'url': 'https://linktr.ee/torqwales',
        'available': False,
    }
    try:
        resp = _cf_get('https://linktr.ee/torqwales')
        if not resp:
            return out
        soup = BeautifulSoup(resp.text, 'lxml')
        desc_meta = soup.find('meta', property='og:description')
        img_meta  = soup.find('meta', property='og:image')
        # Collect all outbound links (skip internal linktr.ee ones)
        links = []
        seen_hrefs: set[str] = set()
        for a in soup.select('a[href]'):
            href = a.get('href', '')
            text = a.get_text(strip=True)
            if href and text and len(text) > 2 and 'linktr' not in href and href not in seen_hrefs:
                seen_hrefs.add(href)
                links.append({'label': text[:60], 'url': href})
        out.update({
            'available': True,
            'name': 'TORQWALES',
            'handle': '@torqwales',
            'bio': (desc_meta or {}).get('content', ''),
            'avatar': (img_meta or {}).get('content', ''),
            'links': links[:8],
        })
    except Exception as exc:
        logger.debug('Linktree: %s', exc)
    return out


def _fetch_friends_data() -> dict:
    with ThreadPoolExecutor(max_workers=4) as pool:
        fw = pool.submit(_scrape_torqwales)
        ft = pool.submit(_fetch_tiktok_profile)
        fi = pool.submit(_fetch_instagram_profile)
        fl = pool.submit(_fetch_linktree_profile)
        website   = fw.result()
        tiktok    = ft.result()
        instagram = fi.result()
        linktree  = fl.result()
    return {
        'website': website,
        'social': [linktree, instagram, tiktok],
    }


@app.route('/api/friends')
def api_friends():
    with _cache_lock:
        entry = _cache.get(_FRIENDS_CACHE_KEY)
        if entry and time.time() - entry[1] < _FRIENDS_TTL:
            return jsonify({'data': entry[0], 'from_cache': True})
    data = _fetch_friends_data()
    with _cache_lock:
        _cache[_FRIENDS_CACHE_KEY] = (data, time.time())
    return jsonify({'data': data})


# Run at import time so gunicorn workers also pre-warm the featured pool
load_dictionary()
threading.Thread(target=_build_featured_pool, daemon=True).start()

if __name__ == '__main__':
    _port = int(os.environ.get('PORT', 8080))
    logger.info('Starting server on http://localhost:%d', _port)
    app.run(host='0.0.0.0', port=_port, debug=False, threaded=True)
