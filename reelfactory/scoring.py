"""Score past reels (views + followers gained + shares/saves; link clicks while they were tracked)
and learn which topics, hook styles, categories, clips, and CTAs work best."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

METRICS = ("views", "follows", "clicks", "shares", "saved")
FEATURES = ("item_id", "style", "category", "clip_id", "cta")
SHRINK = 1.5  # how many "average posts" of pull toward the prior


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


CLICKS_UNTIL = "2026-10-27"  # the bio link changed that day; link clicks can't be tracked after it


def _metric(post: dict, name: str) -> float | None:
    """None = this reel has no number for that metric (it is then left out, not counted as 0)."""
    if name == "clicks":
        ok = post.get("clicks_attr") is not None and post.get("posted_at", "") < CLICKS_UNTIL
        return float(post["clicks_attr"]) if ok else None
    if name == "follows":
        return None if post.get("follows_attr") is None else float(post["follows_attr"])
    return float((post.get("metrics") or {}).get(name) or 0.0)


def judged_posts(posts: list[dict], cfg: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    min_age = cfg["scoring"].get("min_age_hours", 48)
    out = []
    for p in posts:
        if not p.get("metrics") or not p.get("posted_at") or p.get("no_stats_since"):
            continue  # no stats / archived after a flag: not judged (safety.py counts those)
        age_h = (now - parse_ts(p["posted_at"])).total_seconds() / 3600
        if age_h >= min_age:
            out.append(p)
    return out


def score_posts(posts: list[dict], cfg: dict, now: datetime | None = None) -> list[dict]:
    """Attach `score` (weighted z-score) and `weight` (recency) to each judged post."""
    now = now or datetime.now(timezone.utc)
    judged = judged_posts(posts, cfg, now)
    if not judged:
        return []
    weights = cfg["scoring"]["weights"]
    half_life = cfg["scoring"].get("recency_half_life_days", 30)
    # Trial reels reach non-followers, so compare each type against its own kind
    def kind(p):
        return p.get("post_type", "regular")

    stats = {}
    for k in {kind(p) for p in judged}:
        group = [p for p in judged if kind(p) == k]
        for m in METRICS:
            vals = [math.log1p(x) for x in (_metric(p, m) for p in group) if x is not None]
            if len(vals) < 5:
                continue  # too few reels have this metric yet
            mu = sum(vals) / len(vals)
            sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / len(vals)) or 1.0
            stats[(k, m)] = (mu, sd)
    scored = []
    for p in judged:
        s = wsum = 0.0
        for m in METRICS:
            x = _metric(p, m)
            if x is None or (kind(p), m) not in stats or not weights.get(m):
                continue
            mu, sd = stats[(kind(p), m)]
            s += weights[m] * (math.log1p(x) - mu) / sd
            wsum += weights[m]
        s = s / wsum if wsum else 0.0
        age_d = (now - parse_ts(p["posted_at"])).total_seconds() / 86400
        scored.append({**p, "score": round(s, 4), "weight": 0.5 ** (age_d / half_life)})
    return scored


def feature_estimates(scored: list[dict]) -> dict[str, dict[str, dict]]:
    """For each feature value: shrunken mean score + effective sample size."""
    acc: dict[str, dict[str, list[float]]] = {f: defaultdict(lambda: [0.0, 0.0, 0]) for f in FEATURES}
    for p in scored:
        for f in FEATURES:
            key = p.get(f)
            if key is None:
                continue
            a = acc[f][str(key)]
            a[0] += p["weight"] * p["score"]
            a[1] += p["weight"]
            a[2] += 1
    out: dict[str, dict[str, dict]] = {}
    for f, vals in acc.items():
        out[f] = {k: {"est": sw / (w + SHRINK), "n_eff": w, "n": n, "raw_mean": sw / w if w else 0.0}
                  for k, (sw, w, n) in vals.items()}
    return out


def leaderboard(scored: list[dict], est: dict, top: int = 10) -> str:
    """Markdown report of what's working."""
    lines = ["# Nurse Reel Factory — performance report", ""]
    if not scored:
        return "\n".join(lines + ["No reels old enough to judge yet."])
    lines += [f"Judged reels: **{len(scored)}** · score = views, followers gained, shares/saves", ""]
    best = sorted(scored, key=lambda p: p["score"], reverse=True)
    lines += ["## Top reels", "", "| Score | Type | Follows | Views | Shares | Topic | Clip | Posted |",
              "|---|---|---|---|---|---|---|---|"]
    for p in best[:top]:
        m = p.get("metrics") or {}
        link = f"[{p['item_id']}]({p['permalink']})" if p.get("permalink") else p["item_id"]
        lines.append(f"| {p['score']:+.2f} | {p.get('post_type', 'regular')} | {p.get('follows_attr') if p.get('follows_attr') is not None else '-'} | {m.get('views', 0)} | {m.get('shares', 0)} "
                     f"| {link} | {p.get('clip_id')} | {p['posted_at'][:10]} |")
    for f, title in (("style", "Hook styles"), ("category", "Categories"), ("clip_id", "Clips"), ("cta", "CTAs")):
        rows = sorted(est.get(f, {}).items(), key=lambda kv: kv[1]["est"], reverse=True)
        if not rows:
            continue
        lines += ["", f"## {title}", "", "| Value | Strength | Reels |", "|---|---|---|"]
        for k, v in rows:
            lines.append(f"| {k} | {v['est']:+.2f} | {v['n']} |")
    worst = best[-3:] if len(best) > 5 else []
    if worst:
        lines += ["", "## Weakest recent reels", ""]
        lines += [f"- {p['item_id']} ({p['score']:+.2f})" for p in worst]
    return "\n".join(lines) + "\n"
