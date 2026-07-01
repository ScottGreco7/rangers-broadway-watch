#!/usr/bin/env python3
"""
Auto-poller for the Free-Agency watch boards.

Checks the NHL's official roster feed for the team and writes any roster
ADDITIONS or REMOVALS into feed.json (at the repo root). The web page polls
feed.json every few minutes and merges in anything new.

Environment variables (set in the workflow file):
  TEAM       NHL tricode  -> CAR (Hurricanes) or NYR (Rangers)
  TEAM_NAME  Display name -> Hurricanes or Rangers

Notes / honest limits:
  * This detects roster MEMBERSHIP changes, not contract terms, and can't tell a
    signing from a trade from a recall -> every auto item is tagged "roster".
  * It lags the news by however long the NHL takes to update the official roster.
  * The first run just saves a baseline and emits nothing (so it doesn't post the
    whole roster as "new"). Changes are detected from the second run onward.
"""
import json, os, sys, datetime, urllib.request

TEAM = os.environ.get("TEAM", "NYR").upper()
TEAM_NAME = os.environ.get("TEAM_NAME", "Rangers")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
FEED_FILE = "feed.json"
STATE_FILE = "roster-state.json"
MAX_ITEMS = 60


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fa-watch-bot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


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
    """Send a free phone push via ntfy.sh, if NTFY_TOPIC is set."""
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
    """Try a few endpoint variants and keep the fullest result."""
    urls = [
        "https://api-web.nhle.com/v1/roster/%s/current" % TEAM,
        "https://api-web.nhle.com/v1/roster/%s/20262027" % TEAM,
        "https://api-web.nhle.com/v1/roster/%s/20252026" % TEAM,
    ]
    best = {}
    for u in urls:
        try:
            data = fetch_json(u)
            players = roster_players(data)
            print("Tried %s -> %d players" % (u, len(players)))
            if len(players) > len(best):
                best = players
        except Exception as e:
            print("Failed %s -> %s" % (u, e))
    return best


def main():
    if not os.path.exists(FEED_FILE):
        save(FEED_FILE, [])

    current = get_roster()
    if not current:
        print("Could not read any roster players; leaving feed unchanged.")
        return 0

    state = load(STATE_FILE, None)
    if not isinstance(state, dict) or "players" not in state:
        save(STATE_FILE, {"players": current,
                          "updated": datetime.datetime.utcnow().isoformat() + "Z"})
        print("Baseline saved (%d players). No events on first run." % len(current))
        return 0

    prev = state.get("players", {})
    added = [(pid, nm) for pid, nm in current.items() if pid not in prev]
    removed = [(pid, nm) for pid, nm in prev.items() if pid not in current]
    if not added and not removed:
        print("No roster change.")
        return 0

    feed = load(FEED_FILE, [])
    if not isinstance(feed, list):
        feed = []
    seen = set(i.get("id") for i in feed)
    now = datetime.datetime.utcnow()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S")
    day = now.strftime("%Y%m%d")

    def push(item):
        if item["id"] not in seen:
            feed.append(item)
            seen.add(item["id"])

    for pid, nm in added:
        push({
            "id": "auto-add-%s-%s" % (pid, day),
            "t": stamp, "type": "roster", "celebrate": True,
            "title": "%s added to the %s roster" % (nm, TEAM_NAME),
            "body": "%s now appears on %s' official NHL roster. Auto-detected from the league roster feed \u2014 could be a signing, trade, or recall." % (nm, TEAM_NAME),
            "src": "NHL roster feed", "out": "", "players": [nm]
        })
    for pid, nm in removed:
        push({
            "id": "auto-rem-%s-%s" % (pid, day),
            "t": stamp, "type": "roster",
            "title": "%s no longer on the %s roster" % (nm, TEAM_NAME),
            "body": "%s has come off %s' official NHL roster. Auto-detected \u2014 could be a trade, release, or assignment." % (nm, TEAM_NAME),
            "src": "NHL roster feed", "out": "", "players": [nm]
        })

    feed.sort(key=lambda i: i.get("t", ""), reverse=True)
    feed = feed[:MAX_ITEMS]
    save(FEED_FILE, feed)
    save(STATE_FILE, {"players": current,
                      "updated": now.isoformat() + "Z"})

    parts = []
    if added:
        parts.append("Added: " + ", ".join(nm for _, nm in added))
    if removed:
        parts.append("Off roster: " + ", ".join(nm for _, nm in removed))
    ntfy("%s roster update" % TEAM_NAME, "  |  ".join(parts))

    print("Added %d, removed %d. Feed now has %d items." % (len(added), len(removed), len(feed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
