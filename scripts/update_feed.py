#!/usr/bin/env python3
"""
Auto-poller for the Free-Agency watch boards.

Two jobs each run:
  1. Roster diff  -> detects official ADD/REMOVE on the NHL roster feed.
  2. News pull    -> fetches Google News headlines for the team (server-side,
                     no CORS proxy needed) and writes them to feed.json.

Both write into feed.json, which the web page polls every few minutes.

Environment variables (set in the workflow file):
  TEAM        NHL tricode   -> CAR or NYR
  TEAM_NAME   Display name  -> Hurricanes or Rangers
  NEWS_QUERY  Google News search string (optional; skip news if empty)
  NTFY_TOPIC  ntfy.sh topic for phone alerts (optional)
"""
import json, os, sys, re, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

TEAM = os.environ.get("TEAM", "NYR").upper()
TEAM_NAME = os.environ.get("TEAM_NAME", "Rangers")
NEWS_QUERY = os.environ.get("NEWS_QUERY", "").strip()
NEWS_SITES = [s.strip() for s in os.environ.get("NEWS_SITES", "").split(",") if s.strip()]
NEWS_SITE_TERMS = os.environ.get("NEWS_SITE_TERMS", "").strip()
RUMOR_SITES = [s.strip() for s in os.environ.get("RUMOR_SITES", "").split(",") if s.strip()]
RUMOR_INSIDERS = [s.strip() for s in os.environ.get("RUMOR_INSIDERS", "").split(",") if s.strip()]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
FEED_FILE = "feed.json"
STATE_FILE = "roster-state.json"
MAX_NEWS = 40
MAX_TOTAL = 200


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fa-watch-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_json(url):
    return json.loads(fetch_text(url))


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def ntfy(title, message):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            "https://ntfy.sh/" + NTFY_TOPIC,
            data=message.encode("utf-8"), method="POST",
            headers={"Title": title, "Tags": "ice_hockey"})
        urllib.request.urlopen(req, timeout=15)
        print("ntfy sent")
    except Exception as e:
        print("ntfy failed:", e)


# ---- shared id hash (must match the board's newsHash so both sources dedupe) ----
def to36(n):
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n > 0:
        out = digits[n % 36] + out
        n //= 36
    return out


def news_id(title):
    s = re.sub(r"\s+", " ", title.lower()).strip()
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return "news-" + to36(h)


# ---- roster ----
def roster_players(roster):
    out = {}
    if not isinstance(roster, dict):
        return out
    for group in ("forwards", "defensemen", "goalies"):
        for p in (roster.get(group) or []):
            pid = str(p.get("id", "")).strip()
            fn = (p.get("firstName") or {}).get("default", "")
            ln = (p.get("lastName") or {}).get("default", "")
            name = (fn + " " + ln).strip()
            if pid and name:
                out[pid] = name
    return out


def get_roster():
    urls = [
        "https://api-web.nhle.com/v1/roster/%s/current" % TEAM,
        "https://api-web.nhle.com/v1/roster/%s/20262027" % TEAM,
        "https://api-web.nhle.com/v1/roster/%s/20252026" % TEAM,
    ]
    best = {}
    for u in urls:
        try:
            players = roster_players(fetch_json(u))
            print("Tried %s -> %d players" % (u, len(players)))
            if len(players) > len(best):
                best = players
        except Exception as e:
            print("Failed %s -> %s" % (u, e))
    return best


# ---- news ----
def parse_date(pub):
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(pub)
        return d.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")


RUMOR_SIGNALS = [
    "rumor", "rumour", "rumored", "rumoured", "rumor mill", "linked", "linked to",
    "interest", "interested", "in talks", "trade talks", "talks with", "reportedly",
    "targeting", "pursuing", "pursuit of", "eyeing", "in the mix", "sweepstakes",
    "trade bait", "asking price", "shopping", "suitors", "on the block", "trade candidate",
    "speculation", "kicking tires", "market for", "monitoring", "could trade", "may trade",
    "would trade", "open to", "wish list", "potential trade", "possible trade",
    "trade target", "free agent target", "expected to sign", "expected to trade",
]


def classify(title):
    t = " " + title.lower() + " "
    for kw in RUMOR_SIGNALS:
        if kw in t:
            return "rumor"
    return "news"


def parse_news_xml(xml_text):
    out = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        print("  parse failed:", e)
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        src_el = item.find("source")
        outlet = (src_el.text.strip() if src_el is not None and src_el.text else "")
        if not outlet:
            m = re.search(r"\s-\s([^-]+)$", title)
            if m:
                outlet = m.group(1).strip()
        title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
        link = (item.findtext("link") or "").strip()
        out.append({
            "id": news_id(title), "t": parse_date(item.findtext("pubDate") or ""),
            "type": classify(title), "auto": True, "title": title, "body": "",
            "src": outlet or "Google News", "out": "", "players": [], "url": link
        })
        if len(out) >= 15:
            break
    return out


def news_queries():
    """Each entry: (query_string, forced_type_or_None). Rumor sources first so
    their rumor tag wins when the same headline also appears in general news."""
    qs = []
    for dom in RUMOR_SITES:
        q = "site:" + dom
        if NEWS_SITE_TERMS:
            q += " (%s)" % NEWS_SITE_TERMS
        qs.append((q, "rumor"))
    for nm in RUMOR_INSIDERS:
        q = '"%s"' % nm
        if NEWS_SITE_TERMS:
            q += " (%s)" % NEWS_SITE_TERMS
        qs.append((q, "rumor"))
    if NEWS_QUERY:
        qs.append((NEWS_QUERY, None))
    for dom in NEWS_SITES:
        q = "site:" + dom
        if NEWS_SITE_TERMS:
            q += " (%s)" % NEWS_SITE_TERMS
        qs.append((q, None))
    return qs


def pull_news():
    queries = news_queries()
    if not queries:
        print("No news queries set; skipping news.")
        return []
    seen, out = set(), []
    for q, forced in queries:
        url = ("https://news.google.com/rss/search?q=" +
               urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en")
        try:
            items = parse_news_xml(fetch_text(url))
        except Exception as e:
            print("  news fetch failed [%s]: %s" % (q, e))
            continue
        print("  [%s] -> %d%s" % (q, len(items), " (rumor)" if forced else ""))
        for it in items:
            if forced:
                it["type"] = forced
            if it["id"] not in seen:
                seen.add(it["id"])
                out.append(it)
    print("Pulled %d unique headlines." % len(out))
    return out


def trim(feed):
    auto = [i for i in feed if i.get("auto")]
    other = [i for i in feed if not i.get("auto")]
    auto.sort(key=lambda i: i.get("t", ""), reverse=True)
    auto = auto[:MAX_NEWS]
    combined = other + auto
    combined.sort(key=lambda i: i.get("t", ""), reverse=True)
    return combined[:MAX_TOTAL]


def main():
    feed = load(FEED_FILE, [])
    if not isinstance(feed, list):
        feed = []
    seen = set(i.get("id") for i in feed)
    changed = False

    # --- NEWS ---
    for it in pull_news():
        if it["id"] not in seen:
            feed.append(it)
            seen.add(it["id"])
            changed = True

    # --- ROSTER ---
    current = get_roster()
    if current:
        state = load(STATE_FILE, None)
        if not isinstance(state, dict) or "players" not in state:
            save(STATE_FILE, {"players": current,
                              "updated": datetime.datetime.utcnow().isoformat() + "Z"})
            print("Baseline roster saved (%d players)." % len(current))
        else:
            prev = state.get("players", {})
            added = [(pid, nm) for pid, nm in current.items() if pid not in prev]
            removed = [(pid, nm) for pid, nm in prev.items() if pid not in current]
            if added or removed:
                now = datetime.datetime.utcnow()
                stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
                day = now.strftime("%Y%m%d")
                for pid, nm in added:
                    it = {"id": "auto-add-%s-%s" % (pid, day), "t": stamp,
                          "type": "roster", "celebrate": True,
                          "title": "%s added to the %s roster" % (nm, TEAM_NAME),
                          "body": "%s now appears on %s' official NHL roster. Auto-detected from the league roster feed \u2014 could be a signing, trade, or recall." % (nm, TEAM_NAME),
                          "src": "NHL roster feed", "out": "", "players": [nm]}
                    if it["id"] not in seen:
                        feed.append(it); seen.add(it["id"]); changed = True
                for pid, nm in removed:
                    it = {"id": "auto-rem-%s-%s" % (pid, day), "t": stamp,
                          "type": "roster",
                          "title": "%s no longer on the %s roster" % (nm, TEAM_NAME),
                          "body": "%s has come off %s' official NHL roster. Auto-detected \u2014 could be a trade, release, or assignment." % (nm, TEAM_NAME),
                          "src": "NHL roster feed", "out": "", "players": [nm]}
                    if it["id"] not in seen:
                        feed.append(it); seen.add(it["id"]); changed = True
                save(STATE_FILE, {"players": current, "updated": now.isoformat() + "Z"})
                parts = []
                if added:
                    parts.append("Added: " + ", ".join(nm for _, nm in added))
                if removed:
                    parts.append("Off roster: " + ", ".join(nm for _, nm in removed))
                ntfy("%s roster update" % TEAM_NAME, "  |  ".join(parts))
                print("Roster: +%d / -%d" % (len(added), len(removed)))
            else:
                print("No roster change.")
    else:
        print("Roster feed unreadable; leaving roster as-is.")

    if changed:
        feed = trim(feed)
        save(FEED_FILE, feed)
        print("feed.json updated (%d items)." % len(feed))
    else:
        if not os.path.exists(FEED_FILE):
            save(FEED_FILE, [])
        print("No feed changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
