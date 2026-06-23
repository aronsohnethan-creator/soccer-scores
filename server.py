import os
import threading
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("FOOTBALL_API_KEY")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

LEAGUE_NAMES = {
    "WC": "FIFA World Cup",
    "CL": "Champions League",
    "PL": "Premier League",
    "PD": "La Liga",
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "EC": "European Championship",
}

LEAGUE_IDS = list(LEAGUE_NAMES.keys())

_cache = {}
CACHE_TTL = 300

# Rate limiter: max 9 requests per 60 seconds (leave 1 buffer)
_request_times = []
_rate_lock = threading.Lock()


def cached(key, fetcher):
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < CACHE_TTL:
        return _cache[key]["data"]
    data = fetcher()
    if data is not None:
        _cache[key] = {"data": data, "ts": now}
    return data


def parse_match(m):
    return {
        "id": m["id"],
        "home": m["homeTeam"]["name"],
        "away": m["awayTeam"]["name"],
        "home_crest": m["homeTeam"].get("crest", ""),
        "away_crest": m["awayTeam"].get("crest", ""),
        "home_score": m["score"]["fullTime"]["home"],
        "away_score": m["score"]["fullTime"]["away"],
        "ht_home": m["score"].get("halfTime", {}).get("home"),
        "ht_away": m["score"].get("halfTime", {}).get("away"),
        "status": m["status"],
        "utc_date": m["utcDate"],
        "last_updated": m.get("lastUpdated"),
        "matchday": m.get("matchday"),
    }


def group_by_league(matches):
    grouped = {}
    for m in matches:
        code = m.pop("_league", None)
        if code and code in LEAGUE_NAMES:
            if code not in grouped:
                grouped[code] = {"name": LEAGUE_NAMES[code], "matches": []}
            grouped[code]["matches"].append(m)
    ordered = {}
    for code in LEAGUE_IDS:
        if code in grouped:
            ordered[code] = grouped[code]
    return ordered


def _wait_for_rate_limit():
    with _rate_lock:
        now = time.time()
        _request_times[:] = [t for t in _request_times if now - t < 60]
        if len(_request_times) >= 9:
            wait = 60 - (now - _request_times[0]) + 0.5
            if wait > 0:
                time.sleep(wait)
        _request_times.append(time.time())


def api_get(path, params=None):
    _wait_for_rate_limit()
    try:
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers=HEADERS,
            params=params,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            return {"_rate_limited": True}
    except requests.RequestException:
        pass
    return None


def _fetch_matches():
    """Live and recent matches — uses /matches endpoint (single API call)."""
    today = datetime.utcnow().date()
    date_from = (today - timedelta(days=2)).isoformat()
    date_to = (today + timedelta(days=1)).isoformat()

    data = api_get("/matches", {"dateFrom": date_from, "dateTo": date_to})
    if not data:
        return {}

    matches = []
    for m in data.get("matches", []):
        code = m.get("competition", {}).get("code")
        if code in LEAGUE_NAMES:
            parsed = parse_match(m)
            parsed["_league"] = code
            matches.append(parsed)
    return group_by_league(matches)


def _fetch_competition(code):
    """Fetch all matches for a single competition's current season."""
    data = api_get(f"/competitions/{code}/matches")
    if not data:
        return []
    return [parse_match(m) for m in data.get("matches", [])]


def _fetch_upcoming():
    results = {}
    for code in LEAGUE_IDS:
        cache_key = f"comp_{code}"
        matches = cached(cache_key, lambda c=code: _fetch_competition(c))
        upcoming = [m for m in matches if m["status"] in ("TIMED", "SCHEDULED")]
        if upcoming:
            results[code] = {"name": LEAGUE_NAMES[code], "matches": upcoming[:10]}
    return results


def _fetch_last_results():
    results = {}
    for code in LEAGUE_IDS:
        cache_key = f"comp_{code}"
        matches = cached(cache_key, lambda c=code: _fetch_competition(c))
        finished = [m for m in matches if m["status"] == "FINISHED"]
        if finished:
            results[code] = {"name": LEAGUE_NAMES[code], "matches": finished[-10:]}
    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/matches")
def api_matches():
    return jsonify(cached("matches", _fetch_matches))


@app.route("/api/upcoming")
def api_upcoming():
    return jsonify(cached("upcoming", _fetch_upcoming))


@app.route("/api/results")
def api_results():
    return jsonify(cached("results", _fetch_last_results))


@app.route("/api/teams")
def api_teams():
    def fetch():
        teams = {}
        for code in LEAGUE_IDS:
            data = api_get(f"/competitions/{code}/teams")
            if not data:
                continue
            for t in data.get("teams", []):
                if t["id"] not in teams:
                    teams[t["id"]] = {
                        "id": t["id"],
                        "name": t["name"],
                        "short": t.get("shortName") or t["name"],
                        "crest": t.get("crest", ""),
                        "league": LEAGUE_NAMES.get(code, code),
                    }
        return sorted(teams.values(), key=lambda x: x["name"])

    return jsonify(cached("teams", fetch))


@app.route("/api/team/<int:team_id>/matches")
def api_team_matches(team_id):
    def fetch():
        now = datetime.utcnow()
        current_year = now.year if now.month >= 7 else now.year - 1

        seen_ids = set()
        all_matches = []

        for season in [now.year, current_year, current_year - 1]:
            data = api_get(f"/teams/{team_id}/matches", {"season": season})
            if not data:
                continue
            for m in data.get("matches", []):
                if m["id"] in seen_ids:
                    continue
                seen_ids.add(m["id"])
                all_matches.append({
                    "id": m["id"],
                    "competition": m.get("competition", {}).get("name", ""),
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "home_short": m["homeTeam"].get("shortName") or m["homeTeam"]["name"],
                    "away_short": m["awayTeam"].get("shortName") or m["awayTeam"]["name"],
                    "home_crest": m["homeTeam"].get("crest", ""),
                    "away_crest": m["awayTeam"].get("crest", ""),
                    "home_score": m["score"]["fullTime"]["home"],
                    "away_score": m["score"]["fullTime"]["away"],
                    "status": m["status"],
                    "utc_date": m["utcDate"],
                    "matchday": m.get("matchday"),
                })

        all_matches.sort(key=lambda x: x["utc_date"])
        return all_matches

    result = cached(f"team_{team_id}", fetch)
    return jsonify(result)


@app.route("/api/leaders/<code>")
def api_leaders(code):
    if code not in LEAGUE_NAMES:
        return jsonify({"error": "Unknown competition"}), 404

    def fetch():
        # Try current season first, then previous
        data = api_get(f"/competitions/{code}/scorers", {"limit": 20})
        if data and data.get("_rate_limited"):
            return None
        if not data or not data.get("scorers"):
            now = datetime.utcnow()
            prev = now.year - 1 if now.month >= 7 else now.year - 2
            data = api_get(f"/competitions/{code}/scorers", {"limit": 20, "season": prev})
            if not data or data.get("_rate_limited") or not data.get("scorers"):
                return None

        scorers = data["scorers"]
        season_info = data.get("season", {})
        season_str = str(season_info.get("startDate", "?"))[:4]

        result = {
            "season": f"{season_str}/{int(season_str) + 1}" if season_str.isdigit() else "Current",
            "competition": LEAGUE_NAMES[code],
            "top_scorers": [],
        }
        for s in scorers:
            p = s["player"]
            result["top_scorers"].append({
                "name": p.get("name", ""),
                "nationality": p.get("nationality", ""),
                "position": p.get("position", ""),
                "team": s["team"].get("shortName") or s["team"].get("name", ""),
                "crest": s["team"].get("crest", ""),
                "goals": s.get("goals", 0),
                "assists": s.get("assists") or 0,
                "penalties": s.get("penalties") or 0,
                "played": s.get("playedMatches", 0),
            })
        return result

    result = cached(f"leaders_{code}", fetch)
    if result is None:
        return jsonify({"error": "No data available yet, try again shortly"}), 503
    return jsonify(result)


@app.route("/api/standings/<code>")
def api_standings(code):
    if code not in LEAGUE_NAMES:
        return jsonify({"error": "Unknown competition"}), 404

    def fetch():
        data = api_get(f"/competitions/{code}/standings")
        if not data:
            return None
        if data.get("_rate_limited"):
            return "_rate_limited"
        result = []
        for s in data.get("standings", []):
            if s.get("type") != "TOTAL":
                continue
            group = {
                "group": s.get("group"),
                "stage": s.get("stage"),
                "table": [
                    {
                        "position": row["position"],
                        "team": row["team"]["shortName"] or row["team"]["name"],
                        "crest": row["team"].get("crest", ""),
                        "played": row["playedGames"],
                        "won": row["won"],
                        "draw": row["draw"],
                        "lost": row["lost"],
                        "gf": row["goalsFor"],
                        "ga": row["goalsAgainst"],
                        "gd": row["goalDifference"],
                        "points": row["points"],
                        "form": row.get("form"),
                    }
                    for row in s.get("table", [])
                ],
            }
            result.append(group)
        return result

    result = cached(f"standings_{code}", fetch)
    if result == "_rate_limited":
        _cache.pop(f"standings_{code}", None)
        return jsonify({"error": "Rate limited"}), 429
    if result is None:
        return jsonify({"error": "Standings not available"}), 404
    return jsonify(result)


KNOCKOUT_STAGES = [
    "PLAYOFFS", "LAST_32", "LAST_16",
    "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL",
]

STAGE_LABELS = {
    "PLAYOFFS": "Playoffs",
    "LAST_32": "Round of 32",
    "LAST_16": "Round of 16",
    "QUARTER_FINALS": "Quarter-Finals",
    "SEMI_FINALS": "Semi-Finals",
    "THIRD_PLACE": "Third Place",
    "FINAL": "Final",
}


@app.route("/api/bracket/<code>")
def api_bracket(code):
    if code not in LEAGUE_NAMES:
        return jsonify({"error": "Unknown competition"}), 404

    def fetch():
        data = api_get(f"/competitions/{code}/matches")
        if not data:
            return None
        if data.get("_rate_limited"):
            return []

        ko_matches = [
            m for m in data.get("matches", [])
            if m.get("stage") in KNOCKOUT_STAGES
        ]
        if not ko_matches:
            return []

        # Group by stage, then pair legs into ties
        from collections import defaultdict
        by_stage = defaultdict(list)
        for m in ko_matches:
            by_stage[m["stage"]].append(m)

        bracket = []
        for stage in KNOCKOUT_STAGES:
            if stage not in by_stage:
                continue
            matches = sorted(by_stage[stage], key=lambda x: (x.get("utcDate", ""), x["id"]))

            # Pair two-legged ties by matching home/away teams
            ties = []
            used = set()
            for i, m1 in enumerate(matches):
                if m1["id"] in used:
                    continue
                h1 = m1["homeTeam"].get("id")
                a1 = m1["awayTeam"].get("id")
                leg2 = None
                for m2 in matches[i + 1:]:
                    if m2["id"] in used:
                        continue
                    h2 = m2["homeTeam"].get("id")
                    a2 = m2["awayTeam"].get("id")
                    if h1 and a1 and h1 == a2 and a1 == h2:
                        leg2 = m2
                        used.add(m2["id"])
                        break
                used.add(m1["id"])

                def fmt_team(t):
                    return {
                        "name": t.get("shortName") or t.get("name") or "TBD",
                        "crest": t.get("crest", ""),
                    }

                def fmt_leg(mx):
                    return {
                        "home": fmt_team(mx["homeTeam"]),
                        "away": fmt_team(mx["awayTeam"]),
                        "home_score": mx["score"]["fullTime"]["home"],
                        "away_score": mx["score"]["fullTime"]["away"],
                        "status": mx["status"],
                    }

                tie = {"leg1": fmt_leg(m1)}
                if leg2:
                    tie["leg2"] = fmt_leg(leg2)
                    # Compute aggregate
                    s1h = m1["score"]["fullTime"]["home"] or 0
                    s1a = m1["score"]["fullTime"]["away"] or 0
                    s2h = leg2["score"]["fullTime"]["home"] or 0
                    s2a = leg2["score"]["fullTime"]["away"] or 0
                    # team1 = m1 home, team2 = m1 away
                    tie["agg"] = {
                        "team1": s1h + s2a,
                        "team2": s1a + s2h,
                        "team1_name": fmt_team(m1["homeTeam"])["name"],
                        "team2_name": fmt_team(m1["awayTeam"])["name"],
                    }
                ties.append(tie)

            bracket.append({
                "stage": stage,
                "label": STAGE_LABELS.get(stage, stage),
                "ties": ties,
            })
        return bracket

    result = cached(f"bracket_{code}", fetch)
    if result is None:
        return jsonify({"error": "Bracket not available"}), 404
    return jsonify(result)


@app.route("/api/match/<int:match_id>")
def api_match_detail(match_id):
    def fetch():
        match_data = api_get(f"/matches/{match_id}")
        h2h_data = api_get(f"/matches/{match_id}/head2head", {"limit": 10})
        if not match_data:
            return None

        m = match_data
        detail = {
            "id": m["id"],
            "competition": m.get("competition", {}).get("name", ""),
            "competition_code": m.get("competition", {}).get("code", ""),
            "home": m["homeTeam"]["name"],
            "away": m["awayTeam"]["name"],
            "home_short": m["homeTeam"].get("shortName", m["homeTeam"]["name"]),
            "away_short": m["awayTeam"].get("shortName", m["awayTeam"]["name"]),
            "home_crest": m["homeTeam"].get("crest", ""),
            "away_crest": m["awayTeam"].get("crest", ""),
            "status": m["status"],
            "utc_date": m["utcDate"],
            "last_updated": m.get("lastUpdated"),
            "matchday": m.get("matchday"),
            "stage": m.get("stage"),
            "group": m.get("group"),
            "venue": m.get("venue"),
            "score": {
                "full_time": m["score"]["fullTime"],
                "half_time": m["score"].get("halfTime"),
                "winner": m["score"].get("winner"),
                "duration": m["score"].get("duration"),
            },
            "referees": [
                {"name": r["name"], "type": r.get("type"), "nationality": r.get("nationality")}
                for r in m.get("referees", [])
            ],
        }

        if h2h_data:
            agg = h2h_data.get("aggregates", {})
            home_agg = agg.get("homeTeam", {})
            away_agg = agg.get("awayTeam", {})
            detail["head2head"] = {
                "total_matches": agg.get("numberOfMatches", 0),
                "total_goals": agg.get("totalGoals", 0),
                "home_wins": home_agg.get("wins", 0),
                "away_wins": away_agg.get("wins", 0),
                "draws": home_agg.get("draws", 0),
            }
            detail["past_meetings"] = [
                {
                    "utc_date": pm["utcDate"],
                    "home": pm["homeTeam"]["name"],
                    "away": pm["awayTeam"]["name"],
                    "home_score": pm["score"]["fullTime"]["home"],
                    "away_score": pm["score"]["fullTime"]["away"],
                    "competition": pm.get("competition", {}).get("name", ""),
                }
                for pm in h2h_data.get("matches", [])
                if pm["id"] != match_id and pm["status"] == "FINISHED"
            ]

        return detail

    result = cached(f"match_{match_id}", fetch)
    if result is None:
        return jsonify({"error": "Match not found"}), 404
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
