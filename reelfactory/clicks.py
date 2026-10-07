"""Pull hourly nurselexi.com/n visits from Cloudflare Web Analytics and
attribute them to the reels that were live at the time.

Instagram doesn't report link clicks per reel, so we estimate: each hour's
clicks are split between reels posted in the last `attribution_window_hours`,
weighted by each reel's views and how fresh it was (newer = more credit).
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

from .config import CLICKS_PATH, load_json, save_json
from .scoring import parse_ts

GQL = "https://api.cloudflare.com/client/v4/graphql"

QUERY = """
query($account: String!, $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject) {
  viewer { accounts(filter: {accountTag: $account}) {
    rumPageloadEventsAdaptiveGroups(filter: $filter, limit: 5000, orderBy: [datetimeHour_ASC]) {
      count
      sum { visits }
      dimensions { datetimeHour requestPath }
    }
  } }
}
"""


def fetch_hourly(cfg: dict, since: datetime, until: datetime) -> dict[str, int]:
    token, account = os.environ.get("CF_API_TOKEN"), os.environ.get("CF_ACCOUNT_ID")
    if not token or not account:
        print("[clicks] CF_API_TOKEN / CF_ACCOUNT_ID not set — skipping click fetch")
        return {}
    c = cfg["clicks"]
    filt = {"AND": [
        {"datetime_geq": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "datetime_lt": until.strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"requestHost": c["host"]},
        {"requestPath_like": c["path_prefix"].rstrip("/") + "%"},
    ]}
    site_tag = os.environ.get("CF_SITE_TAG")
    if site_tag:
        filt["AND"].append({"siteTag": site_tag})
    resp = requests.post(GQL, json={"query": QUERY, "variables": {"account": account, "filter": filt}},
                         headers={"Authorization": f"Bearer {token}"}, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Cloudflare GraphQL error: {body['errors']}")
    hourly: dict[str, int] = defaultdict(int)
    prefix = c["path_prefix"].rstrip("/")
    for acct in body["data"]["viewer"]["accounts"]:
        for g in acct["rumPageloadEventsAdaptiveGroups"]:
            path = g["dimensions"]["requestPath"]
            # match "/n" and "/n/..." but not "/nurse-other"
            if path == prefix or path.startswith(prefix + "/"):
                hourly[g["dimensions"]["datetimeHour"]] += int(g["count"])
    return dict(hourly)


def update_click_log(cfg: dict, days: int = 10) -> dict[str, int]:
    """Refresh the last `days` of hourly clicks into data/clicks.json and return the full log."""
    log = load_json(CLICKS_PATH, {})
    until = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    since = until - timedelta(days=days)
    fresh = fetch_hourly(cfg, since, until)
    if fresh:
        # replace the window entirely so late-arriving data is corrected
        log = {k: v for k, v in log.items() if parse_ts(k) < since}
        log.update(fresh)
        save_json(CLICKS_PATH, dict(sorted(log.items())))
    return log


def attribute(posts: list[dict], hourly: dict[str, int], cfg: dict) -> None:
    """Set post['clicks_attr'] in place."""
    c = cfg["clicks"]
    window = c.get("attribution_window_hours", 48)
    half_life = c.get("decay_half_life_hours", 12)
    live = [p for p in posts if p.get("posted_at") and p.get("ig_media_id")]
    totals = defaultdict(float)
    for hour_s, n in hourly.items():
        if not n:
            continue
        hour = parse_ts(hour_s)
        shares = []
        for p in live:
            age = (hour - parse_ts(p["posted_at"])).total_seconds() / 3600
            if -1 < age <= window:  # include the posting hour itself
                views = float((p.get("metrics") or {}).get("views") or 1)
                shares.append((p["id"], math.log1p(views) * 0.5 ** (max(age, 0) / half_life)))
        tot = sum(w for _, w in shares)
        for pid, w in shares:
            totals[pid] += n * w / tot
    for p in live:
        p["clicks_attr"] = round(totals.get(p["id"], 0.0), 2)
