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

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "matches.json"
STATUS = ROOT / "data" / "status.json"
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
        d = start
        while d < end:
            span_end = min(d + timedelta(days=30), end)
            url = (f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
                   f"?dates={d:%Y%m%d}-{span_end:%Y%m%d}&limit=500")
            for ev in get_json(url).get("events", []):
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
        for f in ("kickoff_utc", "round", "venue", "uefa_id", "cancelled", "kickoff_confirmed"):
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


def build_ics(store, now):
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
        desc = [f'UEFA {m["competition"]}' + (f' – {m["round"]}' if m.get("round") else "")]
        if m.get("kickoff_confirmed") is False:
            desc.append("Aftrap nog niet definitief.")
        uid = uuid.uuid5(uuid.NAMESPACE_URL, "nl-europa:" + k)
        L += ["BEGIN:VEVENT", f"UID:{uid}@nl-europa-ical", f"DTSTAMP:{stamp}",
              f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{start + timedelta(hours=2):%Y%m%dT%H%M%SZ}",
              fold("SUMMARY:" + esc(title)), fold("DESCRIPTION:" + esc(" ".join(desc)))]
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

    text = build_ics(store, now)
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
