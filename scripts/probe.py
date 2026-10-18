import json, urllib.request, pathlib
out = pathlib.Path("probe"); out.mkdir(exist_ok=True)
def get(u):
    r = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(r, timeout=30) as f: return f.read().decode("utf-8", "replace")
    except Exception as e: return f"ERROR {e}"
t = get("https://match.uefa.com/v5/matches?competitionId=1&seasonYear=2027&limit=3&offset=40&order=ASC")
(out/"uefa_match.json").write_text(t[:20000])
for name, u in {
 "standings1": "https://standings.uefa.com/v1/standings?competitionId=1&seasonYear=2027",
 "espn": "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard?dates=20261020",
 "ziggo_cl": "https://www.ziggo.nl/entertainment/sport/voetbal/champions-league/programma",
 "ziggo_el": "https://www.ziggo.nl/entertainment/sport/voetbal/europa-league/programma",
 "ziggo_ecl": "https://www.ziggo.nl/entertainment/sport/voetbal/conference-league/programma",
}.items():
    (out/f"{name}.txt").write_text(get(u)[:300000])
