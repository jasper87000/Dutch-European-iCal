#!/usr/bin/env python3
"""Builds docs/nl-europa.ics: every Champions League, Europa League and
Conference League match of a Dutch club, kept up to date.

Safeguards
- Two independent live sources (UEFA match data, ESPN scoreboard); either one is enough.
- Results are merged into data/matches.json; matches are never deleted, so a
  bad day can never empty the calendar.
- If a source suddenly returns far fewer matches than we already know about,
  its data is ignored for that run.
- Every match keeps a fixed calendar ID, so a changed kick-off updates the
  existing entry instead of creating a duplicate.
- The calendar file is validated before it replaces the old one.
- data/status.json records what happened; the workflow fails (GitHub sends an
  email) when no live source worked, after still publishing the last good feed.
"""
import json, re, sys, unicodedata, uuid, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

AMS = ZoneInfo("Europe/Amsterdam")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "matches.json"
STATUS = ROOT / "data" / "status.json"
TABLE = ROOT / "data" / "standings.json"
OUT = ROOT / "docs" / "nl-europa.ics"

COMPS = {  # name: (UEFA competitionId, ESPN league slug, short label)
    "Champions League": (1, "uefa.champions", "CL"),
    "Europa League": (14, "uefa.europa", "EL"),
    "Conference League": (2019, "uefa.europa.conf", "ECL"),
}

# Dutch clubs: canonical name -> extra spellings used by data sources
DUTCH = {
    "Ajax": ["afc ajax", "ajax amsterdam"],
    "PSV": ["psv eindhoven"],
    "Feyenoord": ["feyenoord rotterdam"],
    "AZ": ["az alkmaar"],
    "FC Twente": ["twente", "fc twente enschede"],
    "FC Utrecht": ["utrecht"],
    "NEC": ["nec nijmegen", "n.e.c.", "nec nijmegen"],
    "Go Ahead Eagles": ["g.a. eagles", "go ahead eagles deventer"],
    "sc Heerenveen": ["heerenveen"],
    "FC Groningen": ["groningen"],
    "Sparta Rotterdam": ["sparta rotterdam"],
    "Fortuna Sittard": ["fortuna sittard"],
    "Heracles Almelo": ["heracles"],
    "PEC Zwolle": ["pec zwolle", "zwolle"],
    "NAC Breda": ["nac breda", "nac"],
    "Willem II": ["willem ii"],
    "Vitesse": ["vitesse arnhem"],
    "RKC Waalwijk": ["rkc waalwijk", "rkc"],
    "Excelsior": ["excelsior rotterdam"],
    "Telstar": ["telstar"],
    "SC Cambuur": ["cambuur"],
    "FC Volendam": ["volendam"],
    "Almere City": ["almere city fc", "almere"],
}
FILLER = {"fc", "sc", "ac", "cf", "afc", "rb", "fk", "nk", "sk", "club", "cd", "rc", "as", "ss",
          "ssc", "sv", "vfb", "vfl", "tsg", "1899", "04", "de", "real", "calcio", "bc", "sporting"}


def norm(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s).split()


def loose(name):
    words = [w for w in norm(name) if w not in FILLER]
    return " ".join(words) or " ".join(norm(name))


_DUTCH_LOOKUP = {}
for canon, alts in DUTCH.items():
    for n in [canon] + alts:
        _DUTCH_LOOKUP[" ".join(norm(n))] = canon
        _DUTCH_LOOKUP[loose(n)] = canon


def dutch(name, country=None):
    """Canonical Dutch club name, or None."""
    key = " ".join(norm(name))
    if key in _DUTCH_LOOKUP:
        return _DUTCH_LOOKUP[key]
    if loose(name) in _DUTCH_LOOKUP:
        return _DUTCH_LOOKUP[loose(name)]
    if country in ("NED", "NL", "NLD", "Netherlands"):
        return name.strip()
    return None


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nl-europa-ical)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def season_year(today):
    # UEFA labels a season by the year it ends: 2026/27 -> 2027
    return today.year + 1 if today.month >= 7 else today.year


# ---------------------------------------------------------------- sources
def from_uefa(year):
    found = []
    for comp, (cid, _, _) in COMPS.items():
        offset = 0
        while True:
            url = (f"https://match.uefa.com/v5/matches?competitionId={cid}&seasonYear={year}"
                   f"&limit=100&offset={offset}&order=ASC")
            page = get_json(url)
            if isinstance(page, dict):
                page = page.get("matches") or page.get("data") or []
            for m in page:
                h, a = m.get("homeTeam") or {}, m.get("awayTeam") or {}
                hn = h.get("internationalName") or h.get("teamName") or ""
                an = a.get("internationalName") or a.get("teamName") or ""
                hd, ad = dutch(hn, h.get("countryCode")), dutch(an, a.get("countryCode"))
                if not (hd or ad):
                    continue
                ko = (m.get("kickOffTime") or {}).get("dateTime")
                if not ko:
                    continue
                rec = {"competition": comp, "home": hd or hn, "away": ad or an,
                       "kickoff_utc": ko[:19] + "Z", "source": "uefa", "uefa_id": str(m.get("id"))}
                rnd = (m.get("round") or {}).get("metaData", {}).get("name")
                if rnd:
                    rec["round"] = rnd
                st = (m.get("stadium") or {})
                city = (st.get("city") or {}).get("translations", {}).get("name", {}).get("EN")
                sname = (st.get("translations") or {}).get("officialName", {}).get("EN")
                if sname or city:
                    rec["venue"] = ", ".join(x for x in (sname, city) if x)
                sc = (m.get("score") or {}).get("total")
                if m.get("status") == "FINISHED" and sc and sc.get("home") is not None:
                    rec["score"] = [sc["home"], sc["away"]]
                if m.get("status") in ("CANCELLED", "ABANDONED"):
                    rec["cancelled"] = True
                md = (m.get("matchday") or {}).get("longName")
                if md:
                    rec["matchday"] = md
                if "LEG" in str(m.get("type", "")) and m.get("type") != "SINGLE_LEG":
                    num = (m.get("leg") or {}).get("number")
                    if num in (1, 2):
                        rec["leg"] = num
                rec["kickoff_confirmed"] = not (m.get("kickOffTime") or {}).get("dateUndecided", False)
                found.append(rec)
            if len(page) < 100:
                break
            offset += 100
    return found


def from_espn(today):
    found = []
    start = datetime(today.year if today.month >= 7 else today.year - 1, 7, 1)
    end = datetime(start.year + 1, 6, 15)
    for comp, (_, slug, _) in COMPS.items():
        base = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"

        def events_for(a, b):
            try:
                return get_json(f"{base}?dates={a:%Y%m%d}-{b:%Y%m%d}&limit=200").get("events", [])
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
            evs = []  # range not accepted: ask day by day (UEFA plays Tue-Thu)
            day = a
            while day <= b:
                if day.weekday() in (1, 2, 3):
                    evs += get_json(f"{base}?dates={day:%Y%m%d}").get("events", [])
                day += timedelta(days=1)
            return evs

        d = start
        while d < end:
            span_end = min(d + timedelta(days=6), end)
            for ev in events_for(d, span_end):
                comp_ = (ev.get("competitions") or [{}])[0]
                teams = {c.get("homeAway"): c for c in comp_.get("competitors", [])}
                if "home" not in teams or "away" not in teams:
                    continue
                hn = teams["home"]["team"].get("displayName", "")
                an = teams["away"]["team"].get("displayName", "")
                hd, ad = dutch(hn), dutch(an)
                if not (hd or ad):
                    continue
                ko = ev.get("date", "")
                if len(ko) == 17:  # 2026-10-20T19:00Z
                    ko = ko[:16] + ":00Z"
                rec = {"competition": comp, "home": hd or hn, "away": ad or an,
                       "kickoff_utc": ko[:19] + "Z" if not ko.endswith("Z") else ko, "source": "espn"}
                st = (ev.get("status") or {}).get("type", {})
                if st.get("completed"):
                    try:
                        rec["score"] = [int(teams["home"]["score"]), int(teams["away"]["score"])]
                    except (KeyError, ValueError, TypeError):
                        pass
                venue = (comp_.get("venue") or {}).get("fullName")
                if venue:
                    rec["venue"] = venue
                found.append(rec)
            d = span_end + timedelta(days=1)
    return found


# ---------------------------------------------------------------- merge
def ts(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def side_key(m):
    hd, ad = dutch(m["home"]), dutch(m["away"])
    return (m["competition"], hd if hd else "", ad if ad else "")


def find_existing(store, rec):
    sk = side_key(rec)
    t = ts(rec["kickoff_utc"])
    if rec.get("uefa_id"):
        for k, m in store.items():
            if m.get("uefa_id") == rec["uefa_id"]:
                return k
    best = None
    for k, m in store.items():
        if side_key(m) != sk:
            continue
        opp_new = loose(rec["away"] if sk[1] else rec["home"])
        opp_old = loose(m["away"] if sk[1] else m["home"])
        if opp_new == opp_old or opp_new in opp_old or opp_old in opp_new:
            return k
        if abs((ts(m["kickoff_utc"]) - t).total_seconds()) <= 2 * 86400 and best is None:
            best = k
    return best


def merge(store, incoming, prefer):
    changed = 0
    for rec in incoming:
        k = find_existing(store, rec)
        if k is None:
            k = f'{rec["competition"]}|{rec["home"]}|{rec["away"]}|{rec["kickoff_utc"][:10]}'
            store[k] = dict(rec)
            changed += 1
            continue
        old = store[k]
        new = dict(old)
        authoritative = rec["source"] == prefer or old.get("source") in ("seed", rec["source"])
        for f in ("kickoff_utc", "round", "matchday", "leg", "venue", "uefa_id", "cancelled", "kickoff_confirmed"):
            if f in rec and (authoritative or f not in old):
                new[f] = rec[f]
        if "score" in rec:
            new["score"] = rec["score"]
        if rec["source"] == "uefa":  # UEFA names are the nicest; keep Dutch canonical names
            new["home"], new["away"] = rec["home"], rec["away"]
        if authoritative:
            new["source"] = rec["source"]
        if new != old:
            store[k] = new
            changed += 1
    return changed


# ---------------------------------------------------------------- calendar
def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        cb = ch.encode("utf-8")
        if len(cur) + len(cb) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += cb
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


# ---------------------------------------------------------------- extra info
ROUND_NL = {
    "league phase": "Competitiefase", "play-offs": "Play-offs", "play-off round": "Play-offs",
    "first qualifying round": "1e voorronde", "second qualifying round": "2e voorronde",
    "third qualifying round": "3e voorronde", "knockout phase play-offs": "Tussenronde",
    "knockout round play-offs": "Tussenronde", "round of 16": "Achtste finale",
    "quarter-finals": "Kwartfinale", "semi-finals": "Halve finale", "final": "Finale",
}
NL_MONTHS = {m: i + 1 for i, m in enumerate(["januari", "februari", "maart", "april", "mei", "juni", "juli",
                                              "augustus", "september", "oktober", "november", "december"])}
ZIGGO = {
    "Champions League": "https://www.ziggo.nl/entertainment/sport/voetbal/champions-league/programma",
    "Europa League": "https://www.ziggo.nl/entertainment/sport/voetbal/europa-league/programma",
    "Conference League": "https://www.ziggo.nl/entertainment/sport/voetbal/conference-league/programma",
}


def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (nl-europa-ical)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def tv_from_ziggo(store):
    """Adds the Dutch TV channel (Ziggo Sport holds the rights) to matches Ziggo lists."""
    import html as _html
    found = 0
    for comp, url in ZIGGO.items():
        page = get_text(url)
        for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
            cells = re.findall(r"<td>(.*?)</td>", row, re.S)
            if len(cells) < 4:
                continue
            clean = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in cells]
            teams, date, chan_html = clean[0], clean[1], cells[3]
            if " - " not in teams:
                continue
            h, a = teams.split(" - ", 1)
            hd, ad = dutch(h), dutch(a)
            if not (hd or ad):
                continue
            dm = re.search(r"(\d{1,2}) (\w+) (\d{4})", date)
            if not dm or dm.group(2).lower() not in NL_MONTHS:
                continue
            day = f"{dm.group(3)}-{NL_MONTHS[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
            items = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
                     for x in re.findall(r"<li>(.*?)</li>", chan_html, re.S)]
            chan = " / ".join(i for i in items if i) or clean[3]
            chan = re.sub(r"^Switch op\s*", "", chan).strip()
            if not chan or chan.lower() in ("-", "n.t.b.", "nnb", "n.n.b."):
                continue
            for m in store.values():
                if m["competition"] == comp and abs((ts(m["kickoff_utc"]) - ts(day + "T12:00:00")).total_seconds()) <= 36 * 3600:
                    if (hd and dutch(m["home"]) == hd) or (ad and dutch(m["away"]) == ad):
                        if m.get("tv") != chan:
                            m["tv"] = chan
                        found += 1
                        break
    return found


def standings_from_uefa(year):
    table = {}
    for comp, (cid, _, _) in COMPS.items():
        data = get_json(f"https://standings.uefa.com/v1/standings?competitionId={cid}&seasonYear={year}")
        for grp in data if isinstance(data, list) else [data]:
            if (grp.get("group") or {}).get("metaData", {}).get("groupName") not in (None, "League"):
                continue
            for it in grp.get("items", []):
                name = dutch((it.get("team") or {}).get("internationalName", ""),
                             (it.get("team") or {}).get("countryCode"))
                if name and it.get("rank"):
                    table[f"{comp}|{name}"] = {"rank": it["rank"], "points": it.get("points", 0),
                                               "played": it.get("played", 0)}
    return table


def round_text(m):
    r = m.get("round")
    parts = [ROUND_NL.get((r or "").lower(), r)] if r else []
    md = m.get("matchday")
    if md and (r or "").lower() == "league phase":
        num = re.search(r"\d+", md)
        parts.append(f"speelronde {num.group()}" if num else md)
    if m.get("leg") == 1:
        parts.append("heenwedstrijd")
    elif m.get("leg") == 2:
        parts.append("return")
    return ", ".join(p for p in parts if p)


def build_ics(store, now, table=None):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//nl-europa-ical//NL", "CALSCALE:GREGORIAN",
         "METHOD:PUBLISH", "X-WR-CALNAME:Nederlandse clubs in Europa",
         "X-WR-CALDESC:Champions League\\, Europa League en Conference League – automatisch bijgewerkt",
         "X-WR-TIMEZONE:Europe/Amsterdam", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
         "X-PUBLISHED-TTL:PT6H"]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for k, m in sorted(store.items(), key=lambda kv: kv[1]["kickoff_utc"]):
        start = ts(m["kickoff_utc"])
        short = COMPS[m["competition"]][2]
        title = f'⚽ {m["home"]} - {m["away"]} ({short})'
        if m.get("score"):
            title = f'⚽ {m["home"]} {m["score"][0]}-{m["score"][1]} {m["away"]} ({short})'
        if m.get("cancelled"):
            title = "AFGELAST: " + title
        rt = round_text(m)
        desc = [f'UEFA {m["competition"]}' + (f" – {rt}" if rt else "")]
        local = start.astimezone(AMS)
        desc.append(f"🕘 Aftrap {local:%H:%M} (NL-tijd)" +
                    (" – nog niet definitief" if m.get("kickoff_confirmed") is False else ""))
        if m.get("tv"):
            desc.append(f"📺 {m['tv']}")
        elif start > now:
            desc.append("📺 Ziggo Sport (zender volgt)")
        if m.get("venue"):
            desc.append(f"🏟️ {m['venue']}")
        if m.get("score"):
            desc.append(f"⚽ Uitslag: {m['home']} {m['score'][0]}-{m['score'][1]} {m['away']}")
        if table and start > now and (m.get("round") or "").lower() == "league phase":
            for side in (m["home"], m["away"]):
                club = dutch(side)
                pos = table.get(f'{m["competition"]}|{club}') if club else None
                if pos:
                    desc.append(f"📊 {club}: {pos['rank']}e in de competitiefase "
                                f"({pos['points']} pt uit {pos['played']} duels)")
        uid = uuid.uuid5(uuid.NAMESPACE_URL, "nl-europa:" + k)
        L += ["BEGIN:VEVENT", f"UID:{uid}@nl-europa-ical", f"DTSTAMP:{stamp}",
              f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{start + timedelta(hours=2):%Y%m%dT%H%M%SZ}",
              fold("SUMMARY:" + esc(title)), fold("DESCRIPTION:" + esc("\n".join(desc)))]
        if m.get("venue"):
            L.append(fold("LOCATION:" + esc(m["venue"])))
        if m.get("cancelled"):
            L.append("STATUS:CANCELLED")
        L.append("END:VEVENT")
    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n"


def validate(text, expected):
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.rstrip().endswith("END:VCALENDAR")
    n = text.count("BEGIN:VEVENT")
    assert n == text.count("END:VEVENT") == expected, "event count mismatch"
    assert n > 0, "empty calendar"
    uids = re.findall(r"^UID:(.+)$", text, re.M)
    assert len(uids) == len(set(uids)), "duplicate UIDs"


# ---------------------------------------------------------------- main
def main():
    now = datetime.now(timezone.utc)
    store = json.loads(DATA.read_text(encoding="utf-8"))["matches"] if DATA.exists() else {}
    before = len(store)
    status = {"run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "sources": {}}
    year = season_year(now)
    current = [m for m in store.values() if ts(m["kickoff_utc"]) > now - timedelta(days=330)]

    worked = 0
    for name, fn in (("uefa", lambda: from_uefa(year)), ("espn", lambda: from_espn(now))):
        try:
            got = fn()
        except Exception as e:  # network, format change, anything
            status["sources"][name] = f"error: {type(e).__name__}: {e}"[:300]
            continue
        # Sanity check: a working source should know at least most of what we know
        if len(current) >= 10 and len(got) < 0.6 * len(current):
            status["sources"][name] = f"ignored: only {len(got)} matches (expected ~{len(current)})"
            continue
        changed = merge(store, got, prefer="uefa")
        status["sources"][name] = f"ok: {len(got)} matches, {changed} changed/added"
        worked += 1

    # Extras: failures here never block the calendar
    try:
        status["tv"] = f"ok: {tv_from_ziggo(store)} channels"
    except Exception as e:
        status["tv"] = f"error: {type(e).__name__}: {e}"[:300]
    table = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    try:
        fresh = standings_from_uefa(year)
        if fresh:
            table = fresh
            TABLE.write_text(json.dumps(table, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        status["standings"] = f"ok: {len(fresh)} Dutch clubs"
    except Exception as e:
        status["standings"] = f"error (using last known): {type(e).__name__}: {e}"[:300]

    text = build_ics(store, now, table)
    validate(text, len(store))
    assert len(store) >= before, "matches would be lost"

    DATA.write_text(json.dumps({"matches": store}, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="")
    status["matches_in_feed"] = len(store)
    status["live_sources_ok"] = worked
    STATUS.write_text(json.dumps(status, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(status, indent=1))
    return 0 if worked else 2


if __name__ == "__main__":
    sys.exit(main())
