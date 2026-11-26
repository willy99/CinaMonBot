"""
Summarise scripts/loadtest.py results: one row of key numbers per run (+ per-shop request rates).

    python3 scripts/loadtest_report.py ~/loadtest/runs/*.json [--json summary.json]

Reads the result JSON and, if it sits next to it, the scratch DB (for the free/premium split).
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def max_per_window(times: list[float], span: float = 60.0) -> int:
    times = sorted(times)
    best = 0
    for i, t in enumerate(times):
        best = max(best, i - bisect_left(times, t - span) + 1)
    return best


def required_per_min(db: Path, free_iv: int, prem_iv: int) -> dict:
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    rows = con.execute(
        "select u.tier, count(*), count(distinct t.canonical_url) from price_trackers t join users u on u.id=t.user_id "
        "group by u.tier").fetchall()
    shops = con.execute(
        "select t.source, sum(case when u.tier='free' then 1.0/? else 1.0/? end) from "
        "(select distinct canonical_url, source, user_id from price_trackers) t join users u on u.id=t.user_id "
        "group by t.source", (free_iv, prem_iv)).fetchall()
    con.close()
    out = {"trackers": {tier: n for tier, n, _ in rows}}
    per_tracker = sum(n / (free_iv if tier == "free" else prem_iv) for tier, n, _ in rows)
    out["checks_per_min"] = round(per_tracker, 1)
    # upper bound for requests: every tracker's URL fetched separately (dedup only helps within one run)
    out["per_shop_checks_per_min"] = {s: round(v, 1) for s, v in shops}
    return out


def summarize(path: Path) -> dict:
    r = json.loads(path.read_text())
    st = r["settings"]
    dur_min = max([x[1] for x in r["checks"]] + [x["t"] for x in r["samples"]] + [1]) / 60
    req_by_shop: dict[str, list[float]] = defaultdict(list)
    status_by_shop: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for t, dom, status, _lat in r["requests"]:
        req_by_shop[dom].append(t)
        status_by_shop[dom][status] += 1
    shops = {}
    for dom, ts in sorted(req_by_shop.items(), key=lambda kv: -len(kv[1])):
        ts.sort()
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        shops[dom] = {"requests": len(ts), "per_min_avg": round(len(ts) / dur_min, 1),
                      "per_min_max": max_per_window(ts), "min_gap_s": round(min(gaps), 2) if gaps else None,
                      "statuses": dict(status_by_shop[dom])}
    late = [c[3] / 60 for c in r["checks"]]
    tg_ok = [x for x in r["telegram"] if x[3]]
    tg_fail = [x for x in r["telegram"] if not x[3]]
    tg_rate = max_per_window([x[0] for x in tg_ok], 1.0) if tg_ok else 0
    samples = r["samples"]
    need = required_per_min(path.with_suffix(".db"), st["FREE_TIER_CHECK_INTERVAL_MINUTES"],
                            st["PREMIUM_CHECK_INTERVAL_MINUTES"])
    overdue = [s["overdue"] for s in samples]
    return {
        "run": path.stem, "scenario": r["scenario"], "users": r["seed"]["users"],
        "trackers": r["seed"]["trackers"], "unique_urls": r["seed"]["unique_urls"],
        "minutes": round(dur_min, 1), "need_checks_per_min": need.get("checks_per_min"),
        "checks": len(r["checks"]), "checks_per_min": round(len(r["checks"]) / dur_min, 1),
        "requests": len(r["requests"]), "requests_per_min": round(len(r["requests"]) / dur_min, 1),
        "late_min_p50": round(pct(late, .5), 1), "late_min_p95": round(pct(late, .95), 1),
        "late_min_max": round(max(late, default=0), 1),
        "overdue_start": overdue[0] if overdue else None, "overdue_end": overdue[-1] if overdue else None,
        "overdue_max": max(overdue, default=0),
        "run_s_p50": round(pct([x["duration_s"] for x in r["runs"]], .5), 1),
        "run_s_max": round(max([x["duration_s"] for x in r["runs"]], default=0), 1),
        "skipped_ticks": r["skipped_ticks"],
        "rss_mb_max": max((s["rss_mb"] for s in samples), default=0),
        "cpu_pct_avg": round(statistics.mean(s["cpu_pct"] for s in samples[1:]) if len(samples) > 1 else 0, 1),
        "db_query_ms_max": max((s["db_query_ms"] for s in samples), default=0),
        "tg_sent": len(tg_ok), "tg_failed": len(tg_fail), "tg_max_per_s": tg_rate,
        "notifications": r["end"]["notifications"], "tracker_status": r["end"]["tracker_status"],
        "domain_blocks": r["end"]["domain_blocks"], "breaker_opened": sorted({d for s in samples for d in s["breaker_open"]}),
        "digest": r.get("digest"), "network_blocked": r["network_blocked"], "healer_calls": r["healer_calls"],
        "need_per_shop": need.get("per_shop_checks_per_min"), "shops": shops,
        "timeline": [{"t_min": round(s["t"] / 60, 2), "overdue": s["overdue"], "worst_late_min": s["worst_late_min"],
                      "rss_mb": s["rss_mb"], "cpu_pct": s["cpu_pct"]} for s in samples],
        "req_per_min_series": {dom: [sum(1 for t in ts if m * 60 <= t < (m + 1) * 60) for m in range(int(dur_min) + 1)]
                               for dom, ts in req_by_shop.items()},
        "settings": st,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    rows = [summarize(p) for p in a.results]
    for s in rows:
        print(f"\n== {s['run']}: {s['users']} users, {s['trackers']} trackers, {s['unique_urls']} urls, {s['minutes']} min")
        print(f"   checks/min {s['checks_per_min']} (needed ≈{s['need_checks_per_min']}), requests/min {s['requests_per_min']}, "
              f"run p50/max {s['run_s_p50']}/{s['run_s_max']} s, skipped ticks {s['skipped_ticks']}")
        print(f"   lateness p50/p95/max {s['late_min_p50']}/{s['late_min_p95']}/{s['late_min_max']} min, "
              f"overdue start→end {s['overdue_start']}→{s['overdue_end']} (max {s['overdue_max']})")
        print(f"   RSS max {s['rss_mb_max']} MB, CPU avg {s['cpu_pct_avg']} %, telegram sent/failed {s['tg_sent']}/{s['tg_failed']} "
              f"(max {s['tg_max_per_s']}/s), notifications {s['notifications']}")
        print(f"   status {s['tracker_status']}, blocks {s['domain_blocks']}, breaker {s['breaker_opened']}, "
              f"network_blocked {s['network_blocked']}, healer {s['healer_calls']}")
        if s["digest"]:
            print(f"   digest {s['digest']}")
        for dom, d in list(s["shops"].items())[:10]:
            print(f"     {dom:16} {d['requests']:5} req  avg {d['per_min_avg']:5}/min  max {d['per_min_max']:3}/min  "
                  f"min gap {d['min_gap_s']} s  need≈{(s['need_per_shop'] or {}).get(dom)}  {d['statuses']}")
    if a.json:
        a.json.write_text(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
