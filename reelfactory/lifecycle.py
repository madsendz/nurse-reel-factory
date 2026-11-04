"""Topic lifecycle: try every new idea twice, keep the winners, throw out the rest.

Goal (Nick, 2026-11-02): maximize external link clicks. Each reel already gets an
estimated click count (clicks_attr). A topic is judged by its own reels against the
median clicks of every reel from the last `lifecycle.baseline_days` days, so "good"
moves with the account. Trials and regular reels count the same.

  new    -> tried up to 2 times
  winner -> 1 try >= 2x median, or 2 tries averaging >= 1.3x median; repeated via the daily mix
  retired-> 2 tries that didn't make winner (never picked again; Nick can un-retire with "Keep")
  winner -> new again if its last 3 reposts all fall under the median (one more chance, then out)

Verdicts live in data/topic_verdicts.json. Nick's own dashboard decisions
(data/topic_status.json) win: a toss / 🚫 stays retired for good, and a "Keep" after an
automatic retire puts the topic back in play.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from .config import ROOT, load_json, save_json
from .scoring import judged_posts, parse_ts

VERDICTS_PATH = ROOT / "data" / "topic_verdicts.json"


def load_verdicts() -> dict:
    return load_json(VERDICTS_PATH, {})


def apply_verdicts(items: list[dict], verdicts: dict, human: dict) -> None:
    """Set item['status'] from the automatic verdicts + Nick's overrides (called by load_bank)."""
    for i in items:
        v = (verdicts.get(i.get("id")) or {}).get("status")
        h = human.get(i.get("id"))
        if h == "retired":            # toss / 🚫 on the dashboard: permanent
            i["status"] = "retired"
        elif v == "winner":
            i["status"] = "winner"
        elif h:                       # "Keep" after an automatic retire -> back in play
            i["status"] = h
        elif v:
            i["status"] = v
        if v == "active" and h != "retired":   # a demoted winner is tested again like a new topic
            i["demoted"] = True


def _baseline(posts: list[dict], cfg: dict, now: datetime) -> tuple[float | None, int]:
    lc = cfg.get("lifecycle") or {}
    since = now - timedelta(days=lc.get("baseline_days", 30))
    vals = [float(p["clicks_attr"]) for p in posts
            if p.get("clicks_attr") is not None and parse_ts(p["posted_at"]) >= since]
    if len(vals) < lc.get("min_reels_for_baseline", 10):
        return None, len(vals)
    return max(median(vals), 0.5), len(vals)   # never divide by ~0 on a dead week


def _local_median(p: dict, posts: list[dict], cfg: dict, fallback: float) -> float:
    """Median clicks of the reels posted within a few days of this one. Clicks per reel swing a lot
    from week to week (a reel from a good week beats everything from a bad one), so each reel is
    compared with its neighbours in time, not with the whole month."""
    lc = cfg.get("lifecycle") or {}
    half = timedelta(days=lc.get("neighbour_days", 3))
    t = parse_ts(p["posted_at"])
    vals = [float(q["clicks_attr"]) for q in posts
            if q.get("clicks_attr") is not None and abs(parse_ts(q["posted_at"]) - t) <= half]
    if len(vals) < lc.get("min_neighbours", 8):
        return fallback
    return max(median(vals), 0.5)


def judge(bank: list[dict], posts: list[dict], cfg: dict, now: datetime | None = None) -> dict:
    """Update data/topic_verdicts.json. Returns {promoted: [...], retired: [...], demoted: [...], baseline, free_new}."""
    now = now or datetime.now(timezone.utc)
    lc = cfg.get("lifecycle") or {}
    tries_needed = lc.get("tries", 2)
    win_one, win_avg = lc.get("win_one_try", 2.0), lc.get("win_avg", 1.3)
    demote_after = lc.get("demote_after", 3)
    verdicts = load_verdicts()
    human = load_json(ROOT / "data" / "topic_status.json", {})
    out = {"promoted": [], "retired": [], "demoted": [], "baseline": None, "free_new": 0}

    judged = judged_posts(posts, cfg, now)
    base, n = _baseline(judged, cfg, now)
    out["baseline"] = base
    if base is None:
        print(f"[lifecycle] only {n} judged reels in the window — no verdicts yet")
        return out

    by_item: dict[str, list[dict]] = {}
    for p in judged:
        if p.get("clicks_attr") is not None:
            by_item.setdefault(p["item_id"], []).append(p)

    from .picker import topic_kind
    for item in bank:
        iid = item.get("id")
        if not iid or human.get(iid) == "retired":
            continue
        v = verdicts.get(iid) or {}
        since = parse_ts(v["since"]) if v.get("since") else None
        reels = sorted((p for p in by_item.get(iid, []) if not since or parse_ts(p["posted_at"]) > since),
                       key=lambda p: p["posted_at"])
        ratios = [float(p["clicks_attr"]) / _local_median(p, judged, cfg, base) for p in reels]
        is_winner = v.get("status") == "winner" or (not v and topic_kind(item) == "winner")
        if is_winner:
            tail = ratios[-demote_after:]
            if len(tail) >= demote_after and all(r < 1.0 for r in tail):
                verdicts[iid] = {"status": "active", "since": now.isoformat(), "tries": len(ratios),
                                 "ratio": round(sum(tail) / len(tail), 2), "reason": f"last {demote_after} reposts under the median"}
                out["demoted"].append(iid)
            continue
        if v.get("status") == "retired":
            continue
        if not ratios:
            continue
        avg = sum(ratios) / len(ratios)
        if (len(ratios) == 1 and ratios[0] >= win_one) or (len(ratios) >= tries_needed and avg >= win_avg):
            verdicts[iid] = {"status": "winner", "since": now.isoformat(), "tries": len(ratios),
                             "ratio": round(avg, 2), "reason": "clicks well above the median"}
            out["promoted"].append(iid)
        elif len(ratios) >= tries_needed:
            verdicts[iid] = {"status": "retired", "since": now.isoformat(), "tries": len(ratios),
                             "ratio": round(avg, 2), "reason": f"{len(ratios)} tries, {avg:.1f}x median"}
            out["retired"].append(iid)
    save_json(VERDICTS_PATH, verdicts)
    apply_verdicts(bank, verdicts, human)
    out["free_new"] = sum(1 for i in bank if i.get("status", "active") == "active" and topic_kind(i) == "new")
    out["winners"] = sum(1 for i in bank if i.get("status", "active") != "retired" and topic_kind(i) == "winner")
    for k in ("promoted", "retired", "demoted"):
        for iid in out[k]:
            print(f"[lifecycle] {k}: {iid} ({verdicts[iid]['reason']})")
    return out


def digest(res: dict, cfg: dict) -> str | None:
    """One Telegram line a day — only when something changed or topics are running low."""
    low = (cfg.get("lifecycle") or {}).get("low_new_topics", 21)
    parts = []
    if res["promoted"]:
        parts.append("⬆️ new winner" + ("s" if len(res["promoted"]) > 1 else "") + ": " + ", ".join(res["promoted"]))
    if res["demoted"]:
        parts.append("↩️ winner cooled off, trying again: " + ", ".join(res["demoted"]))
    if res["retired"]:
        parts.append("🗑 thrown out: " + ", ".join(res["retired"]))
    if res.get("free_new", 0) < low:
        parts.append(f"⚠️ only {res['free_new']} new topics left (winners: {res.get('winners', 0)}) — write new topics")
    if not parts:
        return None
    return "📊 Topic review\n" + "\n".join(parts) + (f"\n(median = {res['baseline']:.1f} clicks/reel)" if res.get("baseline") else "")
