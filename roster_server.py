"""
SCOUT — Roster Proxy Server (production)
Deploy: gunicorn -w 4 -b 0.0.0.0:$PORT roster_server:app
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, brotli, re, logging, time, random
from bs4 import BeautifulSoup
from functools import lru_cache
from urllib.parse import urlparse

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scout")

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
})

def fetch(url, timeout=15):
    """Fetch URL, handle brotli, rotate UA."""
    for ua in random.sample(USER_AGENTS, len(USER_AGENTS)):
        try:
            SESSION.headers["User-Agent"] = ua
            base = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
            try: SESSION.get(base, timeout=5)
            except: pass
            r = SESSION.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 403:
                log.warning(f"403 for {url}")
                continue
            r.raise_for_status()
            enc = r.headers.get("Content-Encoding", "").lower()
            if enc == "br":
                try:
                    text = brotli.decompress(r.content).decode("utf-8", errors="replace")
                except Exception as e:
                    log.warning(f"Brotli fail: {e}")
                    text = r.text
            else:
                text = r.text
            return BeautifulSoup(text, "lxml"), text
        except requests.HTTPError:
            continue
    raise Exception(f"Could not fetch {url}")

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def parse_height(raw):
    m = re.search(r"(\d)['\-]\s*(\d{1,2})", raw or "")
    return f"{m.group(1)}-{m.group(2)}" if m else clean(raw or "")

def parse_stat(raw):
    try: return float(re.sub(r"[^\d.]", "", raw or ""))
    except: return None

JUNK_NAMES = {"full bio", "bio", "name", "player", "athlete", "view bio",
              "profile", "more", "details", "statistic", "image", "title",
              "head coach", "assistant coach", "coach"}

def is_junk(name):
    return (not name or len(name) < 3 or
            name.lower().strip(".") in JUNK_NAMES or
            name.lower().startswith("full bio") or
            re.match(r"^[\d\s]+$", name))

NORM_POS = {"guard":"G","forward":"F","center":"C","g":"G","f":"F","c":"C",
            "pg":"G","sg":"G","sf":"F","pf":"F","g/f":"G/F","f/c":"F/C"}

def norm_pos(raw):
    first = (raw or "").split()[0].rstrip(".")
    return NORM_POS.get(first.lower(), first.upper()[:3])

# ---------------------------------------------------------------------------
# Sidearm scraper — handles v3 cards, v2 tables, sidearm-table-grid-template
# ---------------------------------------------------------------------------
def scrape_sidearm(soup, base_url):
    players = []

    # ── Strategy 1: v3 .s-person-card (2022+ layout) ────────────────────
    cards = soup.select(".s-person-card")
    if cards:
        for card in cards:
            p = _parse_card(card, base_url)
            if p and not is_junk(p["name"]):
                players.append(p)
        if len(players) >= 3:
            log.info(f"v3 cards: {len(players)}")
            return players
        players = []

    # ── Strategy 2: sidearm-table-grid-template (Sidearm v2 table) ──────
    # This is the exact class Marywood and many D2/D3 schools use
    for tbl in soup.select("table.sidearm-table-grid-template-1, "
                           "table[class*='sidearm-table-grid-template']"):
        rows = tbl.find_all("tr")
        if len(rows) < 3:
            continue
        header = [clean(c.get_text()).lower() for c in rows[0].find_all(["th","td"])]
        # skip coach tables (header has "title" or "image" but no "name" with player context)
        if "title" in header or ("name" in header and "pos" not in " ".join(header) and "ht" not in " ".join(header)):
            continue
        col = _map_columns(header)
        ps = _parse_table_rows(rows[1:], col, base_url)
        if ps:
            players.extend(ps)
        if len(players) >= 3:
            log.info(f"sidearm-table-grid-template: {len(players)}")
            return players
        players = []

    # ── Strategy 3: any sidearm-table with roster-like headers ──────────
    for tbl in soup.select("table.sidearm-table"):
        rows = tbl.find_all("tr")
        if len(rows) < 3:
            continue
        header = [clean(c.get_text()).lower() for c in rows[0].find_all(["th","td"])]
        h_str = " ".join(header)
        if not any(x in h_str for x in ["full name","player","ht","pos.","cl."]):
            continue
        col = _map_columns(header)
        ps = _parse_table_rows(rows[1:], col, base_url)
        if len(ps) >= 3:
            players.extend(ps)
            log.info(f"sidearm-table: {len(players)}")
            return players
        players = []

    # ── Strategy 4: v2 tr.rosterItem / tr.odd / tr.even ─────────────────
    for sel in ["tr.rosterItem", "tr.odd, tr.even", "tr[class*='rosterItem']"]:
        rows = soup.select(sel)
        if len(rows) >= 3:
            for row in rows:
                p = _parse_card(row, base_url)
                if p and not is_junk(p["name"]):
                    players.append(p)
            if len(players) >= 3:
                log.info(f"tr selector '{sel}': {len(players)}")
                return players
            players = []

    # ── Strategy 5: best-scored generic table ───────────────────────────
    best, best_score = None, 0
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 4:
            continue
        header = [clean(c.get_text()).lower() for c in rows[0].find_all(["th","td"])]
        h_str = " ".join(header)
        score = (
            ("full name" in h_str or "player" in h_str) * 15 +
            ("pos" in h_str) * 8 +
            ("ht" in h_str or "height" in h_str) * 8 +
            ("cl." in h_str or "yr" in h_str or "class" in h_str) * 6 +
            ("hometown" in h_str or "high school" in h_str) * 4 +
            len(rows)
        )
        if score > best_score:
            best_score, best = score, tbl
    if best and best_score > 10:
        rows = best.find_all("tr")
        header = [clean(c.get_text()).lower() for c in rows[0].find_all(["th","td"])]
        col = _map_columns(header)
        ps = _parse_table_rows(rows[1:], col, base_url)
        if ps:
            log.info(f"best-table (score {best_score}): {len(ps)}")
            return ps

    # ── Strategy 6: profile links (names only, last resort) ─────────────
    seen = set()
    for a in soup.select("a[href*='/sports/mens-basketball/roster/'],"
                         "a[href*='/sports/womens-basketball/roster/']"):
        name = clean(a.get_text())
        if name and not is_junk(name) and name not in seen:
            seen.add(name)
            players.append({"num":"","name":name,"pos":"","ht":"",
                            "yr":"","hometown":"","photo_url":"","stats":{}})
    if players:
        log.info(f"profile links: {len(players)}")
    return players


def _map_columns(header):
    """Map header strings to column indices."""
    col = {}
    for i, h in enumerate(header):
        if re.match(r"^[#]$|^no\.?$|^num", h) and "name" not in h:
            col.setdefault("num", i)
        elif any(x in h for x in ["full name","player name","athlete","name"]) and "prev" not in h and "title" not in h:
            col.setdefault("name", i)
        elif h in ("pos.","pos","position"):
            col.setdefault("pos", i)
        elif h in ("ht.","ht","height"):
            col.setdefault("ht", i)
        elif any(x in h for x in ["cl.","yr.","year","class","eligib"]):
            col.setdefault("yr", i)
        elif any(x in h for x in ["hometown","city","high school","from","origin"]):
            col.setdefault("hometown", i)
    return col


def _parse_table_rows(rows, col, base_url):
    """Parse <tr> elements using column map."""
    players = []
    for row in rows:
        tds = row.find_all(["td","th"])
        if len(tds) < 2:
            continue
        def cell(key, fb=None):
            idx = col.get(key, fb)
            return clean(tds[idx].get_text()) if idx is not None and idx < len(tds) else ""
        name = cell("name", 2 if len(tds) > 2 else 1)
        if is_junk(name):
            continue
        if sum(1 for td in tds if td.get_text(strip=True)) < 2:
            continue
        # hometown: stop at " / " separator
        hometown_raw = cell("hometown")
        hometown = hometown_raw.split(" / ")[0].strip() if hometown_raw else ""
        # photo
        photo_url = ""
        img = row.select_one("img")
        if img:
            src = img.get("data-src") or img.get("src","")
            if src and not any(x in src for x in ["silhouette","placeholder","spacer","default"]):
                photo_url = src if src.startswith("http") else base_url.rstrip("/") + src
        players.append({
            "num":       re.sub(r"\D","", cell("num",0)) or cell("num",0),
            "name":      name,
            "pos":       norm_pos(cell("pos")),
            "ht":        parse_height(cell("ht")),
            "yr":        cell("yr"),
            "hometown":  hometown,
            "photo_url": photo_url,
            "stats":     {},
        })
    return players


def _parse_card(el, base_url):
    """Extract player from a card/row element using CSS selector cascade."""
    name = ""
    for sel in [".s-person-details__personal-single-line-name",".s-person__name",
                ".full-name",".roster_name","[class*='full-name']","[class*='roster_name']",
                "a[href*='/roster/']",".name a",".name"]:
        el2 = el.select_one(sel)
        if el2:
            candidate = clean(el2.get_text())
            if not is_junk(candidate):
                name = candidate
                break
    if not name:
        tds = el.find_all("td")
        for td in tds[1:3]:
            candidate = clean(td.get_text())
            if not is_junk(candidate):
                name = candidate
                break
    if not name:
        return None
    num = ""
    for sel in [".s-person-card__content__jersey",".roster_jersey","[class*='jersey']"]:
        el2 = el.select_one(sel)
        if el2:
            raw = clean(el2.get_text())
            if re.match(r"^\d{1,2}$", raw.lstrip("0") or "0"):
                num = raw
                break
    pos = ""
    for sel in [".s-person-details__personal-item--position",".roster_pos","[class*='position']","td.pos"]:
        el2 = el.select_one(sel)
        if el2:
            pos = norm_pos(clean(el2.get_text()))
            break
    ht = ""
    for sel in [".s-person-details__personal-item--height",".roster_ht","[class*='height']","td.ht"]:
        el2 = el.select_one(sel)
        if el2:
            ht = parse_height(clean(el2.get_text()))
            break
    yr = ""
    for sel in [".s-person-details__personal-item--academic-year",".roster_yr","[class*='year']","[class*='academic']","td.yr"]:
        el2 = el.select_one(sel)
        if el2:
            candidate = clean(el2.get_text())
            if candidate and len(candidate) <= 20:
                yr = candidate
                break
    hometown = ""
    for sel in [".roster_hometown","[class*='hometown']","td.hometown"]:
        el2 = el.select_one(sel)
        if el2:
            hometown = clean(el2.get_text()).split(" / ")[0]
            break
    photo_url = ""
    for sel in ["img.s-person-card__header__image","img[class*='roster']","img[class*='headshot']","img"]:
        img = el.select_one(sel)
        if img:
            src = img.get("data-src") or img.get("src","")
            if src and not any(x in src for x in ["silhouette","placeholder","spacer","default"]):
                photo_url = src if src.startswith("http") else base_url.rstrip("/") + src
            break
    return {"num":num,"name":name,"pos":pos,"ht":ht,"yr":yr,
            "hometown":hometown,"photo_url":photo_url,"stats":{}}


# ---------------------------------------------------------------------------
# Generic HTML scraper (non-Sidearm sites)
# ---------------------------------------------------------------------------
def scrape_generic(soup, base_url):
    best, best_score = None, 0
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 4:
            continue
        text = tbl.get_text(" ").lower()
        header = " ".join(clean(c.get_text()).lower()
                          for c in rows[0].find_all(["th","td"]))
        score = (
            ("name" in header or "player" in header) * 10 +
            ("pos" in header) * 5 + ("ht" in header) * 5 +
            ("yr" in header or "cl" in header) * 4 +
            text.count("guard") * 2 + text.count("forward") * 2 +
            text.count("center") * 2 +
            len(re.findall(r"\b\d-\d{1,2}\b", text)) * 2 +
            len(rows)
        )
        if score > best_score:
            best_score, best = score, tbl
    if not best or best_score < 8:
        return []
    rows = best.find_all("tr")
    header = [clean(c.get_text()).lower() for c in rows[0].find_all(["th","td"])]
    col = _map_columns(header)
    return _parse_table_rows(rows[1:], col, base_url)


# ---------------------------------------------------------------------------
# Stats page scraper
# ---------------------------------------------------------------------------
def scrape_stats(stats_url, players):
    if not stats_url or not players:
        return players
    try:
        soup, _ = fetch(stats_url)
    except Exception as e:
        log.warning(f"Stats fetch failed: {e}")
        return players
    by_num  = {p["num"]: p for p in players if p.get("num")}
    by_name = {p["name"].lower(): p for p in players if p.get("name")}
    for table in soup.find_all("table"):
        text = table.get_text(" ").lower()
        if not any(x in text for x in ["ppg","pts","reb","ast","fg%"]):
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [clean(th.get_text()).lower() for th in rows[0].find_all(["th","td"])]
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
            tds = row.find_all(["td","th"])
            if not tds:
                continue
            num  = re.sub(r"\D","", cell(tds,"num")).lstrip("0")
            name = cell(tds,"name").lower()
            target = by_num.get(num) or by_name.get(name)
            if not target:
                continue
            target["stats"] = {
                "ppg": parse_stat(cell(tds,"ppg")),
                "rpg": parse_stat(cell(tds,"rpg")),
                "apg": parse_stat(cell(tds,"apg")),
                "fg":  parse_stat(cell(tds,"fg")),
                "fg3": parse_stat(cell(tds,"fg3")),
                "ft":  parse_stat(cell(tds,"ft")),
            }
    return players


# ---------------------------------------------------------------------------
# ESPN fallback
# ---------------------------------------------------------------------------
ESPN_TEAM_URL   = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams"
ESPN_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams/{id}/roster"
ESPN_HEADERS = {
    "User-Agent": USER_AGENTS[0],
    "Origin": "https://www.espn.com",
    "Referer": "https://www.espn.com/mens-college-basketball/",
}

@lru_cache(maxsize=512)
def espn_team_id(school_name):
    try:
        r = requests.get(ESPN_TEAM_URL, params={"limit":1000}, headers=ESPN_HEADERS, timeout=12)
        r.raise_for_status()
        teams = r.json()["sports"][0]["leagues"][0]["teams"]
        lower = school_name.lower()
        for t in teams:
            dn = t["team"]["displayName"].lower()
            if lower in dn or dn in lower:
                return t["team"]["id"]
    except Exception as e:
        log.warning(f"ESPN team lookup: {e}")
    return None

def scrape_espn(school_name):
    tid = espn_team_id(school_name)
    if not tid:
        return []
    try:
        r = requests.get(ESPN_ROSTER_URL.format(id=tid), headers=ESPN_HEADERS, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"ESPN roster: {e}")
        return []
    players = []
    for athlete in data.get("athletes", []):
        info = athlete.get("athlete", athlete)
        pos_info = info.get("position", {})
        stats_cats = athlete.get("statistics",{}).get("splits",{}).get("categories",[])
        stat_map = {}
        for cat in stats_cats:
            for s in cat.get("stats",[]):
                stat_map[s.get("abbreviation","").lower()] = s.get("displayValue")
        players.append({
            "num":       str(info.get("jersey","")),
            "name":      info.get("displayName",""),
            "pos":       pos_info.get("abbreviation","") or pos_info.get("name",""),
            "ht":        parse_height(str(info.get("displayHeight","") or "")),
            "yr":        info.get("experience",{}).get("abbreviation",""),
            "hometown":  info.get("birthPlace",{}).get("city",""),
            "photo_url": info.get("headshot",{}).get("href",""),
            "stats": {
                "ppg": parse_stat(stat_map.get("pts") or stat_map.get("ppg")),
                "rpg": parse_stat(stat_map.get("reb") or stat_map.get("rpg")),
                "apg": parse_stat(stat_map.get("ast") or stat_map.get("apg")),
                "fg":  parse_stat(stat_map.get("fg%") or stat_map.get("fgpct")),
                "fg3": parse_stat(stat_map.get("3p%") or stat_map.get("fg3pct")),
                "ft":  parse_stat(stat_map.get("ft%") or stat_map.get("ftpct")),
            },
        })
    return [p for p in players if p["name"]]


# ---------------------------------------------------------------------------
# CMS detection
# ---------------------------------------------------------------------------
def detect_cms(soup, url):
    text = str(soup)[:8000].lower()
    if "sidearm" in text or "sidearmsports" in text:
        return "sidearm"
    if "prestosports" in text:
        return "presto"
    return "generic"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/api/roster")
def get_roster():
    roster_url = request.args.get("url","").strip()
    stats_url  = request.args.get("stats_url","").strip()
    school     = request.args.get("school","").strip()
    gender     = request.args.get("gender","men").strip()

    if not roster_url and not school:
        return jsonify({"error":"url or school required","players":[]}), 400

    players, source = [], "unknown"

    if roster_url:
        try:
            soup, _ = fetch(roster_url)
            cms = detect_cms(soup, roster_url)
            log.info(f"Fetching {roster_url} CMS={cms}")
            if cms == "sidearm":
                players = scrape_sidearm(soup, roster_url)
                source = "sidearm"
            else:
                players = scrape_generic(soup, roster_url)
                source = "generic"
        except Exception as e:
            log.warning(f"Scrape failed: {e}")

    if not players and school:
        log.info(f"ESPN fallback for {school}")
        players = scrape_espn(school)
        source = "espn"

    if stats_url and players:
        players = scrape_stats(stats_url, players)

    for p in players:
        p["num"] = re.sub(r"^0+","", p.get("num","")) or p.get("num","")

    log.info(f"Returning {len(players)} players via {source}")
    return jsonify({"source":source,"count":len(players),"players":players,"error":None})


@app.route("/api/debug")
def debug():
    url = request.args.get("url","").strip()
    if not url:
        return jsonify({"error":"url param required"}), 400
    try:
        soup, raw = fetch(url)
        cms = detect_cms(soup, url)

        selector_hits = {}
        for sel in [".s-person-card","tr.rosterItem","tr.odd","tr.even",
                    "ul.roster-list li",".roster_name",
                    "table.sidearm-table-grid-template-1",
                    "a[href*='/roster/']","table tbody tr"]:
            try:
                hits = soup.select(sel)
                if hits: selector_hits[sel] = len(hits)
            except: pass

        table_info = []
        for i,t in enumerate(soup.find_all("table")):
            rows = t.find_all("tr")
            header = [clean(c.get_text()) for c in rows[0].find_all(["th","td"])] if rows else []
            sample = [clean(c.get_text()) for c in rows[1].find_all(["th","td"])] if len(rows)>1 else []
            table_info.append({"index":i,"rows":len(rows),"classes":t.get("class",[]),
                               "header":header[:10],"sample_row":sample[:10]})

        players = scrape_sidearm(soup, url) or scrape_generic(soup, url)

        return jsonify({
            "status_code": 200,
            "cms_detected": cms,
            "content_length": len(raw),
            "selector_hits": selector_hits,
            "tables": table_info,
            "scraper_results": len(players),
            "first_3_players": players[:3],
        })
    except Exception as e:
        return jsonify({"error":str(e),"url":url}), 500


@app.route("/api/health")
def health():
    return jsonify({"status":"ok","ts":int(time.time())})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
