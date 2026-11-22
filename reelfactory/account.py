"""Followers gained per reel (an estimate).

Instagram's API reports new followers for the whole account per day, not per reel (per-reel
follows only show in the app). So every refresh saves each recent reel's view count, and each
day's new followers are split between the reels in proportion to the views each one gained that
day. A reel's `follows_attr` covers its first `follows.days` days (default 2) and is only set
once Instagram has reported those days; until then the reel is judged on views alone.

Bio-link taps are NOT available from the API at all (profile_links_taps only counts the
call / email / text buttons), so link clicks need another source.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import ROOT, load_json, save_json
from .scoring import parse_ts

DAILY_PATH = ROOT / "data" / "account_daily.json"   # {"YYYY-MM-DD": {"follows": n}}
VLOG_PATH = ROOT / "data" / "views_log.json"        # {post_id: {"YYYY-MM-DD": views}}
WEB_PATH = ROOT / "data" / "web_stats.json"         # {"reels": {post_id: {"follows": n, "link_taps": n, "at": iso}}}


def _follows(body: dict) -> int | None:
    rows = body.get("data") or []
    if not rows:
        return None
    tv = rows[0].get("total_value") or {}
    for b in tv.get("breakdowns") or []:
        for r in b.get("results") or []:
            if (r.get("dimension_values") or [None])[0] == "FOLLOWER":
                return int(r.get("value") or 0)
    return None if tv.get("value") is None else int(tv["value"])


def update_daily(ig, cfg: dict, now: datetime) -> dict:
    """New followers per UTC day for the last few days (Instagram's numbers can lag up to 48 h)."""
    daily = load_json(DAILY_PATH, {})
    for back in range(1, 5):
        day = (now - timedelta(days=back)).date()
        key = day.isoformat()
        if back > 2 and (daily.get(key) or {}).get("follows") is not None:
            continue  # settled
        since = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        try:
            body = ig.get(f"{ig.user_id}/insights", metric="follows_and_unfollows", period="day",
                          metric_type="total_value", breakdown="follow_type", since=since, until=since + 86399)
            n = _follows(body)
            if back == 1:
                print(f"[account] {key} follows raw: {json.dumps(body.get('data'))[:300]}")
            if n is not None:
                daily.setdefault(key, {})["follows"] = n
        except Exception as e:
            print(f"[account] follows for {key} failed: {str(e)[:200]}")
    save_json(DAILY_PATH, daily)
    return daily


def snapshot_views(posts: list[dict], now: datetime, keep_days: int = 6) -> dict:
    """Remember today's view count for every recent reel (the last refresh of the day wins)."""
    log = load_json(VLOG_PATH, {})
    today = now.date().isoformat()
    live = set()
    for p in posts:
        if not p.get("posted_at") or (now - parse_ts(p["posted_at"])).days > keep_days:
            continue
        live.add(p["id"])
        v = (p.get("metrics") or {}).get("views")
        if v is not None:
            log.setdefault(p["id"], {})[today] = int(v)
    log = {k: v for k, v in log.items() if k in live}
    save_json(VLOG_PATH, log)
    return log


def attribute_follows(posts: list[dict], daily: dict, log: dict, cfg: dict) -> None:
    """Set post['follows_attr'] for reels whose first days are fully reported."""
    days = (cfg.get("follows") or {}).get("days", 2)
    gains: dict[str, dict[str, float]] = {}        # date -> {post_id: views gained that day}
    by_id = {p["id"]: p for p in posts}
    for pid, series in log.items():
        p = by_id.get(pid)
        if not p:
            continue
        d0 = parse_ts(p["posted_at"]).date()
        dates = sorted(series)
        if not dates or datetime.fromisoformat(dates[0]).date() > d0 + timedelta(days=1):
            continue  # we weren't recording when this reel went up
        prev = 0
        for d in dates:
            gains.setdefault(d, {})[pid] = max(series[d] - prev, 0)
            prev = series[d]
    for pid in log:
        p = by_id.get(pid)
        if not p:
            continue
        d0 = parse_ts(p["posted_at"]).date()
        want = [(d0 + timedelta(days=k)).isoformat() for k in range(days)]
        if any((daily.get(d) or {}).get("follows") is None or pid not in gains.get(d, {}) for d in want[1:]) \
                or (daily.get(want[0]) or {}).get("follows") is None:
            continue  # not fully reported yet
        total = 0.0
        for d in want:
            day_gain = sum(gains.get(d, {}).values())
            if day_gain > 0:
                total += daily[d]["follows"] * gains[d].get(pid, 0) / day_gain
        if not p.get("follows_exact"):
            p["follows_attr"] = round(total, 2)


def apply_web_stats(posts: list[dict]) -> int:
    """Exact per-reel numbers read off Instagram's own dashboard (Nick 2026-11-22: Claude reads each
    recent reel's insights page in Chrome once a day and saves them to data/web_stats.json). They
    replace the estimate above: follows -> follows_attr, link taps (when the page shows them) -> link_clicks."""
    reels = (load_json(WEB_PATH, {}) or {}).get("reels") or {}
    n = 0
    for p in posts:
        w = reels.get(p.get("id"))
        if not w:
            continue
        if w.get("follows") is not None:
            p["follows_attr"], p["follows_exact"] = float(w["follows"]), True
            n += 1
        if w.get("link_taps") is not None:
            p["link_clicks"] = float(w["link_taps"])
    return n
