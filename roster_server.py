"""
SCOUT — Roster Proxy Server
============================
Runs on your server. The front-end calls:
  GET /api/roster?url=https://rolltide.com/sports/mens-basketball/roster
  GET /api/roster?url=...&gender=women

Returns JSON:
  { "source": "sidearm"|"espn"|"generic", "players": [...], "error": null }

Each player:
  { num, name, pos, ht, yr, hometown, photo_url, stats: {ppg,rpg,apg,fg,fg3,ft} }

Deploy:
  pip install flask flask-cors requests beautifulsoup4 lxml
  python roster_server.py
  # or with gunicorn:
  gunicorn -w 4 -b 0.0.0.0:5050 roster_server:app
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re, logging, time
from functools import lru_cache

app = Flask(__name__)
CORS(app)  # allow your front-end origin — tighten in production
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scout")

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Rotate through several realistic UAs to avoid single-UA blocks
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

import random

SESSION = requests.Session()
SESSION.headers.update(BROWSER_HEADERS)


def fetch(url, timeout=15):
    """Fetch a URL, rotating User-Agent on retry. Returns (BeautifulSoup, raw_text) or raises."""
    last_err = None
    for ua in random.sample(USER_AGENTS, len(USER_AGENTS)):
        try:
            SESSION.headers["User-Agent"] = ua
            # Hit the base domain first to pick up cookies (helps with Sidearm WAF)
            from urllib.parse import urlparse
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            try:
                SESSION.get(base, timeout=6, allow_redirects=True)
            except Exception:
                pass
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 403:
                body = r.text.strip()
                last_err = f"403 from {url} — {body[:80]}"
                log.warning(last_err)
                continue  # try next UA
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml"), r.text
        except requests.HTTPError as e:
            last_err = str(e)
            continue
        except Exception as e:
            raise
    raise requests.HTTPError(last_err or f"All UAs blocked for {url}")


# ---------------------------------------------------------------------------
# Utility parsers
# ---------------------------------------------------------------------------
def clean(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def parse_height(raw):
    """'6-3', '6\'3"', '6 ft 3 in' → '6-3'"""
    if not raw:
        return ""
    m = re.search(r"(\d)\D+(\d{1,2})", raw)
    return f"{m.group(1)}-{m.group(2)}" if m else clean(raw)


def parse_stat(raw):
    """'14.2' → 14.2, blank / '--' → None"""
    if not raw:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", raw))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sidearm Sports scraper
# Sidearm powers ~65% of NCAA D1 and most D2/D3/NJCAA/NAIA sites.
# Their roster page HTML is consistent across versions.
# ---------------------------------------------------------------------------
def extract_player_from_element(el, base_url):
    """Extract player dict from any Sidearm element (card, row, li)."""
    # Jersey number
    num = ""
    for sel in [".s-person-card__content__jersey", ".roster_jersey", "[class*='jersey']",
                "td:first-child", ".jersey"]:
        el2 = el.select_one(sel)
        if el2:
            num = clean(el2.get_text())
            if re.match(r"^\d{1,2}$", num.lstrip("0") or "0"):
                break
            num = ""

    # Name
    name = ""
    for sel in [
        ".s-person-details__personal-single-line-name",
        ".s-person__name", ".full-name", ".roster_name",
        "[class*='full-name']", "[class*='roster_name']",
        "a[href*='/roster/']", "a[href*='/athlete']",
        "td.name", ".name a", ".name",
    ]:
        el2 = el.select_one(sel)
        if el2:
            candidate = clean(el2.get_text())
            if candidate and len(candidate) > 2 and not candidate.isdigit():
                name = candidate
                break

    if not name:
        # fallback: second <td> often has name in table layouts
        tds = el.find_all("td")
        if len(tds) >= 2:
            candidate = clean(tds[1].get_text())
            if candidate and len(candidate) > 2:
                name = candidate

    # Position
    pos = ""
    for sel in [".s-person-details__personal-item--position",
                ".roster_pos", "[class*='position']", "td.pos",
                "[class*='roster_pos']"]:
        el2 = el.select_one(sel)
        if el2:
            pos = clean(el2.get_text())
            break

    # Height
    ht = ""
    for sel in [".s-person-details__personal-item--height",
                ".roster_ht", "[class*='height']", "td.ht",
                "[class*='roster_ht']"]:
        el2 = el.select_one(sel)
        if el2:
            ht = parse_height(el2.get_text())
            break

    # Year / Class
    yr = ""
    for sel in [".s-person-details__personal-item--academic-year",
                ".roster_yr", "[class*='year']", "[class*='academic']",
                "td.yr", "td.class", "[class*='roster_yr']"]:
        el2 = el.select_one(sel)
        if el2:
            candidate = clean(el2.get_text())
            if candidate and len(candidate) <= 20:
                yr = candidate
                break

    # Hometown
    hometown = ""
    for sel in [".roster_hometown", "[class*='hometown']", "[class*='city']",
                "td.hometown", "[class*='roster_hometown']"]:
        el2 = el.select_one(sel)
        if el2:
            hometown = clean(el2.get_text())
            break

    # Photo
    photo_url = ""
    for sel in ["img.s-person-card__header__image", "img[class*='roster']",
                "img[class*='headshot']", "img[class*='player']", "img"]:
        img = el.select_one(sel)
        if img:
            src = img.get("data-src") or img.get("src", "")
            if src and "silhouette" not in src and "placeholder" not in src and "spacer" not in src:
                photo_url = src if src.startswith("http") else base_url.rstrip("/") + src
            break

    return {"num": num, "name": name, "pos": pos, "ht": ht, "yr": yr,
            "hometown": hometown, "photo_url": photo_url, "stats": {}}


def scrape_sidearm(soup, base_url):
    """
    Comprehensive Sidearm scraper covering v1 (legacy table), v2 (2019-2022),
    and v3 (2022+ card layout). Tries selectors in order until players found.
    """
    players = []

    # ── v3: .s-person-card ──────────────────────────────────────────────
    cards = soup.select(".s-person-card")
    if cards:
        for card in cards:
            p = extract_player_from_element(card, base_url)
            if p["name"]:
                players.append(p)
        if players:
            log.info(f"Sidearm v3 cards: {len(players)} players")
            return players

    # ── v2: <ul> roster list ────────────────────────────────────────────
    items = soup.select("ul.roster-list li, ul[class*='roster'] li")
    if items:
        for item in items:
            p = extract_player_from_element(item, base_url)
            if p["name"]:
                players.append(p)
        if players:
            log.info(f"Sidearm v2 ul-li: {len(players)} players")
            return players

    # ── v2: table rows with class rosterItem / odd / even ───────────────
    for row_sel in ["tr.rosterItem", "tr.odd, tr.even", "tr[class*='rosterItem']",
                    ".roster_item", "[class*='roster-item']"]:
        rows = soup.select(row_sel)
        if len(rows) >= 3:  # need at least a few rows to be a real roster
            for row in rows:
                p = extract_player_from_element(row, base_url)
                if p["name"] and len(p["name"]) > 2:
                    players.append(p)
            if players:
                log.info(f"Sidearm v2 row selector '{row_sel}': {len(players)} players")
                return players

    # ── v2/legacy: biggest table that looks like a roster ───────────────
    tables = soup.find_all("table")
    for table in sorted(tables, key=lambda t: len(t.find_all("tr")), reverse=True):
        text = table.get_text(" ").lower()
        # score how roster-like this table is
        score = (
            text.count("guard") * 3 + text.count("forward") * 3 +
            text.count("center") * 3 +
            len(re.findall(r"\b\d-\d{1,2}\b", text)) * 2 +
            text.count(" fr") + text.count(" so") + text.count(" jr") + text.count(" sr")
        )
        if score < 3:
            continue

        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # detect header
        header_row = rows[0]
        headers = [clean(th.get_text()).lower() for th in header_row.find_all(["th", "td"])]

        col = {}
        for i, h in enumerate(headers):
            if any(x in h for x in ["no", "#", "num", "jersey"]):  col.setdefault("num", i)
            elif any(x in h for x in ["name", "player", "athlete"]): col.setdefault("name", i)
            elif h in ("pos", "position"):                             col.setdefault("pos", i)
            elif any(x in h for x in ["ht", "height"]):               col.setdefault("ht", i)
            elif any(x in h for x in ["yr", "year", "cl", "class"]):  col.setdefault("yr", i)
            elif any(x in h for x in ["hometown", "city", "from"]):   col.setdefault("hometown", i)

        def cell(tds, key, fallback_idx=None):
            idx = col.get(key, fallback_idx)
            return clean(tds[idx].get_text()) if idx is not None and idx < len(tds) else ""

        for row in rows[1:]:
            tds = row.find_all(["td", "th"])
            if len(tds) < 2:
                continue
            name = cell(tds, "name", 1)
            # skip header-like repeats
            if not name or len(name) < 3 or name.lower() in ("name", "player", "athlete"):
                continue
            # skip rows that are section headers (single merged cell)
            if len(tds) == 1:
                continue

            # photo from img in row
            photo_url = ""
            img = row.select_one("img")
            if img:
                src = img.get("data-src") or img.get("src", "")
                if src and "silhouette" not in src and "placeholder" not in src:
                    photo_url = src if src.startswith("http") else base_url.rstrip("/") + src

            p = {
                "num":      cell(tds, "num", 0),
                "name":     name,
                "pos":      cell(tds, "pos"),
                "ht":       parse_height(cell(tds, "ht")),
                "yr":       cell(tds, "yr"),
                "hometown": cell(tds, "hometown"),
                "photo_url": photo_url,
                "stats":    {},
            }
            players.append(p)

        if players:
            log.info(f"Sidearm table scrape: {len(players)} players")
            return players

    # ── last resort: any link to a player profile page ──────────────────
    profile_links = soup.select("a[href*='/sports/mens-basketball/roster/'],"
                                "a[href*='/sports/womens-basketball/roster/']")
    seen = set()
    for a in profile_links:
        name = clean(a.get_text())
        if name and len(name) > 2 and name not in seen:
            seen.add(name)
            players.append({"num": "", "name": name, "pos": "", "ht": "",
                            "yr": "", "hometown": "", "photo_url": "", "stats": {}})
    if players:
        log.info(f"Sidearm profile links fallback: {len(players)} players")

    return players


# ---------------------------------------------------------------------------
# ESPN public API scraper (fallback for non-Sidearm sites)
# Uses the same ESPN hidden API that powers espn.com
# ---------------------------------------------------------------------------
ESPN_TEAM_SEARCH = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams"
ESPN_ROSTER_TPL  = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{id}/roster"

ESPN_HEADERS = {
    **BROWSER_HEADERS,
    "Origin":  "https://www.espn.com",
    "Referer": "https://www.espn.com/mens-college-basketball/",
}

@lru_cache(maxsize=512)
def espn_team_id(school_name):
    """Look up ESPN team ID by school name. Cached."""
    try:
        r = requests.get(ESPN_TEAM_SEARCH, params={"limit": 1000},
                         headers=ESPN_HEADERS, timeout=12)
        r.raise_for_status()
        teams = r.json()["sports"][0]["leagues"][0]["teams"]
        name_lower = school_name.lower()
        for t in teams:
            dn = t["team"]["displayName"].lower()
            if name_lower in dn or dn in name_lower:
                return t["team"]["id"]
    except Exception as e:
        log.warning(f"ESPN team lookup failed: {e}")
    return None


def scrape_espn(school_name):
    """Fetch roster from ESPN API."""
    tid = espn_team_id(school_name)
    if not tid:
        return []
    url = ESPN_ROSTER_TPL.format(id=tid)
    try:
        r = requests.get(url, headers=ESPN_HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"ESPN roster fetch failed: {e}")
        return []

    players = []
    for athlete in data.get("athletes", []):
        info = athlete.get("athlete", athlete)
        pos_info = info.get("position", {})
        ht_raw = info.get("displayHeight", "") or info.get("height", "")
        stats_list = athlete.get("statistics", {}).get("splits", {}).get("categories", [])

        # flatten stats
        stat_map = {}
        for cat in stats_list:
            for s in cat.get("stats", []):
                stat_map[s.get("abbreviation", "").lower()] = s.get("displayValue")

        photo = info.get("headshot", {}).get("href", "") or info.get("links", [{}])[0].get("href", "")

        p = {
            "num":       str(info.get("jersey", "")),
            "name":      info.get("displayName", ""),
            "pos":       pos_info.get("abbreviation", "") or pos_info.get("name", ""),
            "ht":        parse_height(str(ht_raw)),
            "yr":        info.get("experience", {}).get("abbreviation", "") or info.get("eligibility", ""),
            "hometown":  info.get("birthPlace", {}).get("city", ""),
            "photo_url": photo if isinstance(photo, str) else "",
            "stats": {
                "ppg": parse_stat(stat_map.get("pts") or stat_map.get("ppg")),
                "rpg": parse_stat(stat_map.get("reb") or stat_map.get("rpg")),
                "apg": parse_stat(stat_map.get("ast") or stat_map.get("apg")),
                "fg":  parse_stat(stat_map.get("fg%") or stat_map.get("fgpct")),
                "fg3": parse_stat(stat_map.get("3p%") or stat_map.get("fg3pct")),
                "ft":  parse_stat(stat_map.get("ft%") or stat_map.get("ftpct")),
            },
        }
        if p["name"]:
            players.append(p)

    return players


# ---------------------------------------------------------------------------
# Generic HTML scraper (last resort — works on many custom sites)
# ---------------------------------------------------------------------------
def scrape_generic(soup, url):
    """Heuristic scraper: finds any table or list that looks like a roster."""
    players = []

    # Strategy 1: find the biggest table that has player-like data
    tables = soup.find_all("table")
    best_table = None
    best_score = 0

    for table in tables:
        text = table.get_text(" ").lower()
        score = sum([
            text.count("guard") * 3,
            text.count("forward") * 3,
            text.count("center") * 3,
            text.count(" fr ") + text.count(" so ") + text.count(" jr ") + text.count(" sr "),
            len(re.findall(r"\b\d-\d{1,2}\b", text)) * 2,  # heights
            len(table.find_all("tr")) // 2,
        ])
        if score > best_score:
            best_score = score
            best_table = table

    if best_table and best_score > 5:
        rows = best_table.find_all("tr")
        # detect header row
        header = []
        if rows:
            header = [clean(th.get_text()).lower() for th in rows[0].find_all(["th", "td"])]

        # map column indices
        col = {}
        for i, h in enumerate(header):
            if any(x in h for x in ["no", "num", "#", "jersey"]):
                col.setdefault("num", i)
            elif any(x in h for x in ["name", "player", "athlete"]):
                col.setdefault("name", i)
            elif "pos" in h:
                col.setdefault("pos", i)
            elif any(x in h for x in ["ht", "height"]):
                col.setdefault("ht", i)
            elif any(x in h for x in ["yr", "year", "cl", "class", "eligibility"]):
                col.setdefault("yr", i)
            elif any(x in h for x in ["hometown", "city", "origin"]):
                col.setdefault("hometown", i)

        def cell(tds, key):
            idx = col.get(key)
            if idx is not None and idx < len(tds):
                return clean(tds[idx].get_text())
            return ""

        for row in rows[1:]:
            tds = row.find_all(["td", "th"])
            if len(tds) < 2:
                continue
            name = cell(tds, "name") or (clean(tds[1].get_text()) if len(tds) > 1 else "")
            if not name or len(name) < 3 or name.lower() in ("name", "player"):
                continue
            p = {
                "num":      cell(tds, "num"),
                "name":     name,
                "pos":      cell(tds, "pos"),
                "ht":       parse_height(cell(tds, "ht")),
                "yr":       cell(tds, "yr"),
                "hometown": cell(tds, "hometown"),
                "photo_url": "",
                "stats":    {},
            }
            players.append(p)

    return players


# ---------------------------------------------------------------------------
# Stat scraper (stats page → augment existing player list)
# ---------------------------------------------------------------------------
def scrape_stats(stats_url, players):
    """
    Fetch a stats page and attach per-player stats to the player list.
    Matches by jersey number or name.
    """
    if not stats_url or not players:
        return players

    try:
        soup, _ = fetch(stats_url)
    except Exception as e:
        log.warning(f"Stats fetch failed for {stats_url}: {e}")
        return players

    # Build a lookup by number and by name
    by_num  = {p["num"]: p for p in players if p.get("num")}
    by_name = {p["name"].lower(): p for p in players if p.get("name")}

    tables = soup.find_all("table")
    for table in tables:
        text = table.get_text(" ").lower()
        if not any(x in text for x in ["ppg", "pts", "reb", "ast", "fg%"]):
            continue

        rows = table.find_all("tr")
        if not rows:
            continue

        header = [clean(th.get_text()).lower() for th in rows[0].find_all(["th", "td"])]

        col = {}
        for i, h in enumerate(header):
            if any(x in h for x in ["no","#","num","jersey"]):  col.setdefault("num", i)
            elif any(x in h for x in ["name","player"]):         col.setdefault("name", i)
            elif h in ("pts","ppg","points"):                     col.setdefault("ppg", i)
            elif h in ("reb","rpg","rebounds","total reb"):       col.setdefault("rpg", i)
            elif h in ("ast","apg","assists"):                    col.setdefault("apg", i)
            elif "fg%" in h or "fg pct" in h:                    col.setdefault("fg", i)
            elif "3p%" in h or "3fg%" in h:                      col.setdefault("fg3", i)
            elif "ft%" in h:                                      col.setdefault("ft", i)

        def cell(tds, key):
            idx = col.get(key)
            return clean(tds[idx].get_text()) if idx is not None and idx < len(tds) else ""

        for row in rows[1:]:
            tds = row.find_all(["td", "th"])
            if not tds:
                continue
            num  = cell(tds, "num").lstrip("0") or cell(tds, "num")
            name = cell(tds, "name").lower()

            target = by_num.get(num) or by_name.get(name)
            if not target:
                continue

            target["stats"] = {
                "ppg": parse_stat(cell(tds, "ppg")),
                "rpg": parse_stat(cell(tds, "rpg")),
                "apg": parse_stat(cell(tds, "apg")),
                "fg":  parse_stat(cell(tds, "fg")),
                "fg3": parse_stat(cell(tds, "fg3")),
                "ft":  parse_stat(cell(tds, "ft")),
            }

    return players


# ---------------------------------------------------------------------------
# Detect CMS
# ---------------------------------------------------------------------------
def detect_cms(soup, url):
    text = str(soup)[:5000]
    if "sidearm" in text.lower() or "sidearmsports" in text.lower():
        return "sidearm"
    if "prestosports" in text.lower():
        return "presto"
    if "arbitersports" in text.lower():
        return "arbiter"
    if "espn.com" in url:
        return "espn"
    return "generic"


# ---------------------------------------------------------------------------
# Main route
# ---------------------------------------------------------------------------
@app.route("/api/roster")
def get_roster():
    roster_url = request.args.get("url", "").strip()
    stats_url  = request.args.get("stats_url", "").strip()
    school     = request.args.get("school", "").strip()
    gender     = request.args.get("gender", "men").strip()

    if not roster_url and not school:
        return jsonify({"error": "url or school param required", "players": []}), 400

    players = []
    source  = "unknown"

    # ---- Try scraping the roster URL ----
    if roster_url:
        try:
            soup, raw = fetch(roster_url)
            cms = detect_cms(soup, roster_url)
            log.info(f"Fetching {roster_url} → CMS: {cms}")

            if cms == "sidearm":
                players = scrape_sidearm(soup, roster_url)
                source = "sidearm"
            else:
                players = scrape_generic(soup, roster_url)
                source = "generic"

            # If HTML scraping got nothing, try ESPN
            if not players and school:
                players = scrape_espn(school)
                source = "espn"

        except requests.HTTPError as e:
            err_str = str(e)
            log.warning(f"HTTP error for {roster_url}: {err_str}")
            if "403" in err_str:
                # Site is blocking the server IP — fall through to ESPN
                log.info(f"Site blocked server IP, trying ESPN for {school}")
            # fall through to ESPN below
        except Exception as e:
            log.error(f"Scrape error: {e}")
            return jsonify({"error": str(e), "players": []}), 500

    # ---- ESPN fallback ----
    if not players and school:
        log.info(f"Falling back to ESPN for {school}")
        players = scrape_espn(school)
        source = "espn"

    # ---- Attach stats ----
    if stats_url and players:
        log.info(f"Fetching stats from {stats_url}")
        players = scrape_stats(stats_url, players)

    # ---- Clean up numbers ----
    for p in players:
        p["num"] = re.sub(r"^0+", "", p.get("num", "")) or p.get("num", "")

    log.info(f"Returning {len(players)} players via {source}")
    return jsonify({
        "source":  source,
        "count":   len(players),
        "players": players,
        "error":   None,
    })


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "ts": int(time.time())})


@app.route("/api/debug")
def debug():
    """
    Full diagnostic endpoint. Runs every scraper strategy and reports results.
    Usage: /api/debug?url=https://marywoodpacers.com/sports/mens-basketball/roster
    """
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url param required"}), 400
    try:
        soup, raw_text = fetch(url)
        cms = detect_cms(soup, url)

        # Run every selector and report counts
        selector_hits = {}
        for sel in [
            ".s-person-card", "tr.rosterItem", "tr.odd", "tr.even",
            "ul.roster-list li", ".roster_item", "[class*='rosterItem']",
            "a[href*='/roster/']", ".roster_name", ".full-name",
            "table tbody tr",
        ]:
            try:
                hits = soup.select(sel)
                if hits:
                    selector_hits[sel] = len(hits)
            except Exception:
                pass

        # Table inventory
        table_info = []
        for i, t in enumerate(soup.find_all("table")):
            rows = t.find_all("tr")
            header_cells = [clean(c.get_text()) for c in rows[0].find_all(["th","td"])] if rows else []
            sample_row = [clean(c.get_text()) for c in rows[1].find_all(["th","td"])] if len(rows) > 1 else []
            table_info.append({
                "index": i,
                "rows": len(rows),
                "classes": t.get("class", []),
                "id": t.get("id", ""),
                "header": header_cells[:10],
                "sample_row": sample_row[:10],
            })

        # Actually run the full scraper pipeline
        players_sidearm  = scrape_sidearm(soup, url)
        players_generic  = scrape_generic(soup, url) if not players_sidearm else []

        return jsonify({
            "status_code": 200,
            "cms_detected": cms,
            "content_length": len(raw_text),
            "selector_hits": selector_hits,
            "tables": table_info,
            "scraper_results": {
                "sidearm": len(players_sidearm),
                "generic": len(players_generic),
            },
            "first_3_players": players_sidearm[:3] or players_generic[:3],
            "first_500_chars": raw_text[:500],
        })
    except Exception as e:
        return jsonify({"error": str(e), "url": url}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
