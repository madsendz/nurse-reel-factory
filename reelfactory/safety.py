"""Keep reels off Instagram's flag list.

Nick, 2026-11-22: reels Instagram flags get archived, and a flagged account gets shown to fewer
people. Two rules:

  1. Banned words / emojis (config.yaml `safety:`): a topic whose title or facts contain one is
     never posted until it is reworded. Censored spellings (s*x, sp0t, lub3) count — in the
     Oct-Nov data they were flagged ~80% of the time, plain clinical words ~10%.
  2. Strikes: a topic whose reels were flagged `flag_strikes` times is blocked until its text
     is changed (dashboard "Edit text", or `reworded: <date>` on the topic in bank.yaml).

A reel counts as flagged when Instagram stops returning stats for it (it was archived/removed)
while the other reels still report normally — see cli.cmd_refresh (`no_stats_since`).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .scoring import parse_ts


def _flat(x) -> str:
    if isinstance(x, dict):
        return " ".join(f"{k}: {_flat(v)}" for k, v in x.items())
    if isinstance(x, (list, tuple)):
        return " ".join(_flat(v) for v in x)
    return "" if x is None else str(x)


def item_text(item: dict) -> str:
    return _flat([item.get("hook"), item.get("beats")])


def banned_hits(text: str, cfg: dict) -> list[str]:
    """Names of the banned words / patterns found in `text` (empty list = clean)."""
    s = cfg.get("safety") or {}
    low = text.lower()
    hits = [w for w in s.get("banned") or [] if str(w).lower() in low]
    for name, pat in (s.get("banned_patterns") or {}).items():
        try:
            if re.search(pat, text):
                hits.append(name)
        except re.error as e:
            print(f"[safety] bad pattern {name!r}: {e}")
    return hits


def is_flagged(p: dict, now: datetime, cfg: dict) -> bool:
    """Posted a while ago and Instagram no longer reports stats for it = archived after a flag."""
    if not p.get("no_stats_since") or not p.get("posted_at"):
        return False
    hours = (cfg.get("safety") or {}).get("flag_after_hours", 24)
    return (now - parse_ts(p["posted_at"])).total_seconds() / 3600 >= hours


def _ts(x) -> datetime | None:
    """A timestamp from topic_text.json ('at') or a `reworded:` date in bank.yaml."""
    if not x:
        return None
    try:
        s = x.isoformat() if hasattr(x, "isoformat") else str(x)
        d = parse_ts(s if "T" in s else s + "T00:00:00+00:00")
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def cleared_at(item: dict) -> datetime | None:
    """When this topic's wording last changed — flags before that no longer count."""
    times = [t for t in (_ts(item.get("text_edited_at")), _ts(item.get("reworded"))) if t]
    return max(times) if times else None


def strikes(item: dict, posts: list[dict], now: datetime, cfg: dict) -> list[str]:
    """posted_at of this topic's flagged reels since its wording last changed."""
    since = cleared_at(item)
    return sorted(p["posted_at"] for p in posts
                  if p.get("item_id") == item.get("id") and is_flagged(p, now, cfg)
                  and (since is None or parse_ts(p["posted_at"]) > since))
