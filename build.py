#!/usr/bin/env python3
"""Haalt de Sleeper league-data op en bouwt dashboard.html met een ingebakken snapshot.

Gebruik:  python3 build.py [--league 1392804756254126080]
Schrijft: data/snapshot.json en dashboard.html (naast dit script).
Het dashboard probeert bij openen ook zelf live data op te halen; de snapshot is de fallback.
"""
import argparse, json, pathlib, urllib.request, datetime

API = "https://api.sleeper.app/v1"
HERE = pathlib.Path(__file__).resolve().parent


def get(path):
    with urllib.request.urlopen(f"{API}{path}", timeout=30) as r:
        return json.load(r)


def trim_matchups(rows):
    # Alleen wat de KPI's nodig hebben: starters + roster + punten per speler.
    return [{"roster_id": m["roster_id"], "matchup_id": m.get("matchup_id"),
             "points": m.get("points") or 0, "players_points": m.get("players_points") or {},
             "starters": m.get("starters") or [], "players": m.get("players") or []}
            for m in rows]


def trim_tx(rows):
    return [{"type": t["type"], "leg": t.get("leg"), "created": t.get("created"),
             "adds": t.get("adds") or {}, "drops": t.get("drops") or {}, "roster_ids": t.get("roster_ids") or [],
             "draft_picks": t.get("draft_picks") or [], "waiver_budget": t.get("waiver_budget") or []}
            for t in rows if t.get("status") == "complete"]


# Weekstats die het dashboard gebruikt (spelers en team-defenses).
STAT_KEYS = ("fum_lost", "pass_int", "rec_drop", "pass_yd", "rush_yd", "rec_yd", "pass_td", "rush_td", "rec_td",
             "fgm", "xpm", "sack", "int", "fum_rec", "def_td", "def_st_td", "safe", "pts_allow", "pos_rank_ppr")


def fill_k_def_ranks(st):
    """Sleeper geeft kickers en defenses geen weekrang (999). Rang berekenen op pts_ppr."""
    is_k = lambda x: any(k in x for k in ("fga", "xpa", "fgm", "xpm")) and not any(k in x for k in ("pass_att", "rush_att", "rec_tgt"))
    groups = {"K": [k for k, x in st.items() if k.isdigit() and is_k(x)],
              "DEF": [k for k in st if not k.isdigit() and not k.startswith("TEAM_")]}
    for ids in groups.values():
        for i, k in enumerate(sorted(ids, key=lambda k: -(st[k].get("pts_ppr") or 0))):
            st[k]["pos_rank_ppr"] = i + 1


def trim_player(p):
    return {"name": p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip(),
            "pos": p.get("position"), "fpos": p.get("fantasy_positions") or [p.get("position")],
            "team": p.get("team"), "inj": p.get("injury_status")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default="1392804756254126080")
    a = ap.parse_args()
    league = get(f"/league/{a.league}")
    last_week = max(league["settings"].get("last_scored_leg") or 0, 0)
    snap = {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "league": {k: league.get(k) for k in ("league_id", "name", "season", "status", "total_rosters")}
                  | {"settings": {k: league["settings"].get(k) for k in
                                  ("playoff_week_start", "playoff_teams", "last_scored_leg", "start_week")}},
        "users": [{"user_id": u["user_id"], "display_name": u["display_name"],
                   "team_name": (u.get("metadata") or {}).get("team_name")} for u in get(f"/league/{a.league}/users")],
        "rosters": [{"roster_id": r["roster_id"], "owner_id": r.get("owner_id"), "players": r.get("players") or [],
                     "starters": r.get("starters") or [], "reserve": r.get("reserve") or []} for r in get(f"/league/{a.league}/rosters")],
        "roster_positions": league.get("roster_positions") or [],
        "matchups": {str(w): trim_matchups(get(f"/league/{a.league}/matchups/{w}")) for w in range(1, last_week + 1)},
        "transactions": {str(w): trim_tx(get(f"/league/{a.league}/transactions/{w}")) for w in range(1, last_week + 1)},
    }
    # Spelersdatabase (groot, ~15 MB): alleen spelers die in deze league voorkomen bewaren.
    ids = {pid for r in snap["rosters"] for pid in r["players"]}
    ids |= {pid for ms in snap["matchups"].values() for m in ms for pid in m["players"]}
    ids |= {pid for ts in snap["transactions"].values() for t in ts for pid in list(t["adds"]) + list(t["drops"])}
    # Draft (eerste/enige draft van dit seizoen).
    drafts = get(f"/league/{a.league}/drafts") or []
    if drafts:
        d = drafts[0]
        picks = get(f"/draft/{d['draft_id']}/picks") or []
        snap["draft"] = {"type": d.get("type"), "rounds": (d.get("settings") or {}).get("rounds"),
                         "picks": [{"pick_no": p["pick_no"], "round": p["round"], "slot": p["draft_slot"],
                                    "roster_id": p["roster_id"], "player_id": p["player_id"]} for p in picks]}
        ids |= {p["player_id"] for p in picks}
    allp = get("/players/nfl")
    snap["players"] = {pid: trim_player(allp[pid]) for pid in ids if pid in allp}
    # Weekstats (NFL): wie speelde, fumbles lost, interceptions, drops. Alleen league-spelers bewaren.
    sc = league.get("scoring_settings") or {}
    snap["scoring"] = {k: sc.get(k, 0) for k in ("fum_lost", "pass_int", "rec", "rec_yd")}
    # Seizoensranking NFL-breed (PPR) per speler: [positierang, punten].
    season = get(f"/stats/nfl/regular/{league['season']}") or {}
    snap["season_rank"] = {pid: [season[pid].get("pos_rank_ppr"), season[pid].get("pts_ppr")] for pid in ids
                           if pid in season and season[pid].get("pos_rank_ppr")}
    # Projecties (Sleeper, PPR) per speler per week, voor points scored luck.
    snap["proj"] = {}
    for w in range(1, last_week + 1):
        pr = get(f"/projections/nfl/regular/{league['season']}/{w}") or {}
        snap["proj"][str(w)] = {pid: round(pr[pid]["pts_ppr"], 2) for pid in ids if pid in pr and pr[pid].get("pts_ppr")}
    snap["stats"] = {}
    for w in range(1, last_week + 1):
        st = get(f"/stats/nfl/regular/{league['season']}/{w}") or {}
        fill_k_def_ranks(st)
        snap["stats"][str(w)] = {
            "played": sorted(k for k in st if not k.isdigit() and not k.startswith("TEAM_")),
            "p": {pid: {k: st[pid][k] for k in STAT_KEYS if st[pid].get(k)} for pid in ids if pid in st}}
    (HERE / "data").mkdir(exist_ok=True)
    (HERE / "data" / "snapshot.json").write_text(json.dumps(snap, indent=1))
    tpl = (HERE / "template.html").read_text()
    payload = json.dumps(snap, separators=(",", ":")).replace("<", "\\u003c")
    (HERE / "dashboard.html").write_text(tpl.replace("/*__SNAPSHOT__*/null", payload))
    # League scoring (gemiddelde en mediaan per week) apart bewaren voor verdere analyse.
    import statistics
    rows = ["week,gemiddelde,mediaan,hoogste,laagste"]
    for w, ms in snap["matchups"].items():
        pts = [m["points"] for m in ms]
        if any(pts):
            rows.append(f"{w},{statistics.mean(pts):.2f},{statistics.median(pts):.2f},{max(pts):.2f},{min(pts):.2f}")
    (HERE / "data" / "league_scoring.csv").write_text("\n".join(rows) + "\n")
    print(f"OK: {league['name']} {league['season']}, weken 1-{last_week}, {len(snap['rosters'])} teams")


if __name__ == "__main__":
    main()
