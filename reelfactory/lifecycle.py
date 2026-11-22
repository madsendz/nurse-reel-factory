"""Topic lifecycle: try every new idea twice, keep the winners, throw out the rest.

Version 2 (Nick, 2026-11-22). A reel is judged on views + followers gained + link clicks, each
compared with the typical reel of the same type (trial / regular) posted within a few days of
it. Metrics that aren't available for a reel are left out and the rest carry the weight (link
clicks stopped being trackable on 2026-10-27 when the bio link changed).

  new     -> tried up to 2 times
  winner  -> a reel with `hit_views`+ views, or 1 try >= 2x typical, or 2 tries averaging >= 1.3x
  retired -> 2 tries that didn't make winner (Nick can un-retire with "Keep")
  winner  -> new again if its last 3 reposts all fall under typical (one more chance, then out)
  flagged -> Instagram flagged its reels `safety.flag_strikes` times: blocked until reworded

Verdicts live in data/topic_verdicts.json (entries carry "v": 2; older click-only verdicts are
ignored, which also brings back the topics retired while click tracking was dead). Nick's own
dashboard decisions (data/topic_status.json) win: a toss / 🚫 stays retired for good, and a
"Keep" after an automatic retire puts the topic back in play.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from . import safety
from .config import ROOT, load_json, save_json
from .scoring import judged_posts, parse_ts

VERDICTS_PATH = ROOT / "data" / "topic_verdicts.json"
VERSION = 2
METRICS = ("views", "follows", "link_clicks")
FLOOR = {"views": 100.0, "follows": 0.5, "link_clicks": 0.5}   # never divide by ~0 on a dead week
CAP = 20.0                                                     # one metric can't count for more than 20x


def load_verdicts() -> dict:
    return {k: v for k, v in load_json(VERDICTS_PATH, {}).items() if (v or {}).get("v") == VERSION}


def apply_verdicts(items: list[dict], verdicts: dict, human: dict) -> None:
    """Set item['status'] from the automatic verdicts + Nick's overrides (called by load_bank)."""
    for i in items:
        v = (verdicts.get(i.get("id")) or {}).get("status")
        h = human.get(i.get("id"))
        if h == "retired":            # toss / 🚫 on the dashboard: permanent
            i["status"] = "retired"
        elif v == "flagged":          # flagged by Instagram: blocked until the text changes
            i["status"] = "flagged"
        elif v == "winner":
            i["status"] = "winner"
        elif h:                       # "Keep" after an automatic retire -> back in play
            i["status"] = h
        elif v:
            i["status"] = v
        if v == "active" and h != "retired":   # a demoted winner / reworded topic is tested again like a new one
            i["demoted"] = True


def metric_value(p: dict, m: str, cfg: dict) -> float | None:
    if m == "views":
        v = (p.get("metrics") or {}).get("views")
    elif m == "follows":
        v = p.get("follows_attr")
    else:  # link clicks: only while a tracker was running
        until = (cfg.get("lifecycle") or {}).get("link_clicks_until")
        v = p.get("clicks_attr") if until and p.get("posted_at", "") < str(until) else p.get("link_clicks")
    return None if v is None else float(v)


def _typical(p: dict, m: str, judged: list[dict], cfg: dict) -> float | None:
    """Median of metric `m` over the reels posted within a few days of `p` (same type first). Results
    swing a lot from week to week, so each reel is compared with its neighbours in time."""
    lc = cfg.get("lifecycle") or {}
    half = timedelta(days=lc.get("neighbour_days", 3))
    need = lc.get("min_neighbours", 8)
    t = parse_ts(p["posted_at"])
    near = [q for q in judged if abs(parse_ts(q["posted_at"]) - t) <= half]
    month = [q for q in judged if abs(parse_ts(q["posted_at"]) - t) <= timedelta(days=lc.get("baseline_days", 30))]
    for pool in ([q for q in near if q.get("post_type") == p.get("post_type")], near, month):
        vals = [x for x in (metric_value(q, m, cfg) for q in pool) if x is not None]
        if len(vals) >= need:
            return max(median(vals), FLOOR[m])
    return None


def reel_ratio(p: dict, judged: list[dict], cfg: dict) -> float | None:
    """How this reel did vs a typical one (1.0 = typical), across the metrics it has."""
    w = (cfg.get("lifecycle") or {}).get("weights") or {"views": 1, "follows": 1, "link_clicks": 1}
    num = den = 0.0
    for m in METRICS:
        v = metric_value(p, m, cfg)
        if v is None or not w.get(m):
            continue
        typ = _typical(p, m, judged, cfg)
        if typ is None:
            continue
        num += w[m] * min(v / typ, CAP)
        den += w[m]
    return num / den if den else None


def judge(bank: list[dict], posts: list[dict], cfg: dict, now: datetime | None = None, save: bool = True) -> dict:
    """Update data/topic_verdicts.json. Returns what changed (promoted / retired / demoted / flagged / blocked)."""
    now = now or datetime.now(timezone.utc)
    lc = cfg.get("lifecycle") or {}
    tries_needed = lc.get("tries", 2)
    win_one, win_avg = lc.get("win_one_try", 2.0), lc.get("win_avg", 1.3)
    demote_after, hit = lc.get("demote_after", 3), lc.get("hit_views", 100000)
    need_strikes = (cfg.get("safety") or {}).get("flag_strikes", 2)
    verdicts = load_verdicts()
    human = load_json(ROOT / "data" / "topic_status.json", {})
    out = {"promoted": [], "retired": [], "demoted": [], "flagged": [], "cleared": [], "baseline": None, "free_new": 0}

    judged = judged_posts(posts, cfg, now)
    month = [float(p["metrics"]["views"]) for p in judged if (p.get("metrics") or {}).get("views") is not None
             and parse_ts(p["posted_at"]) >= now - timedelta(days=lc.get("baseline_days", 30))]
    enough = len(month) >= lc.get("min_reels_for_baseline", 10)
    out["baseline"] = median(month) if enough else None
    if not enough:
        print(f"[lifecycle] only {len(month)} judged reels in the window — no win/lose verdicts yet")

    by_item: dict[str, list[dict]] = {}
    for p in judged:
        by_item.setdefault(p["item_id"], []).append(p)

    from .picker import topic_kind
    for item in bank:
        iid = item.get("id")
        if not iid or human.get(iid) == "retired":
            continue
        v = verdicts.get(iid) or {}
        # ---- flagged by Instagram: blocked until reworded ----
        flags = safety.strikes(item, posts, now, cfg)
        if len(flags) >= need_strikes:
            if v.get("status") != "flagged":
                verdicts[iid] = {"v": VERSION, "status": "flagged", "since": now.isoformat(), "tries": len(flags),
                                 "ratio": 0, "reason": f"flagged by Instagram {len(flags)} times — reword it"}
                out["flagged"].append(iid)
            continue
        if v.get("status") == "flagged":   # reworded since: one fresh start, tested like a new topic
            c = safety.cleared_at(item) or now
            v = verdicts[iid] = {"v": VERSION, "status": "active", "since": c.isoformat(), "tries": 0,
                                 "ratio": 0, "reason": "reworded after flags"}
            out["cleared"].append(iid)
        if not enough:
            continue
        since = parse_ts(v["since"]) if v.get("since") else None
        reels = sorted((p for p in by_item.get(iid, []) if not since or parse_ts(p["posted_at"]) > since),
                       key=lambda p: p["posted_at"])
        scored = [(r, p) for r, p in ((reel_ratio(p, judged, cfg), p) for p in reels) if r is not None]
        ratios = [r for r, _ in scored]
        best_views = max((float((p.get("metrics") or {}).get("views") or 0) for _, p in scored), default=0)
        is_winner = v.get("status") == "winner" or (not v and topic_kind(item) == "winner")
        if is_winner:
            tail = ratios[-demote_after:]
            if len(tail) >= demote_after and all(r < 1.0 for r in tail):
                verdicts[iid] = {"v": VERSION, "status": "active", "since": now.isoformat(), "tries": len(ratios),
                                 "ratio": round(sum(tail) / len(tail), 2),
                                 "reason": f"last {demote_after} reposts under typical"}
                out["demoted"].append(iid)
            continue
        if v.get("status") == "retired" or not ratios:
            continue
        avg = sum(ratios) / len(ratios)
        if best_views >= hit or (len(ratios) == 1 and ratios[0] >= win_one) or (len(ratios) >= tries_needed and avg >= win_avg):
            why = f"a reel with {int(best_views):,} views" if best_views >= hit else f"{avg:.1f}x a typical reel"
            verdicts[iid] = {"v": VERSION, "status": "winner", "since": now.isoformat(), "tries": len(ratios),
                             "ratio": round(avg, 2), "reason": why}
            out["promoted"].append(iid)
        elif len(ratios) >= tries_needed:
            verdicts[iid] = {"v": VERSION, "status": "retired", "since": now.isoformat(), "tries": len(ratios),
                             "ratio": round(avg, 2), "reason": f"{len(ratios)} tries, {avg:.1f}x a typical reel"}
            out["retired"].append(iid)
    if save:
        save_json(VERDICTS_PATH, verdicts)
    apply_verdicts(bank, verdicts, human)
    for i in bank:   # banned words win over everything except Nick's toss (same rule as config.load_bank)
        if i.get("status", "active") in ("active", "winner") and safety.banned_hits(safety.item_text(i), cfg):
            i["status"] = "flagged"
    out["verdicts"] = verdicts
    out["free_new"] = sum(1 for i in bank if i.get("status", "active") == "active" and topic_kind(i) == "new")
    out["winners"] = sum(1 for i in bank if i.get("status", "active") in ("active", "winner") and topic_kind(i) == "winner")
    out["blocked"] = sorted(i["id"] for i in bank if i.get("status") == "flagged")
    for k in ("promoted", "retired", "demoted", "flagged", "cleared"):
        for iid in out[k]:
            print(f"[lifecycle] {k}: {iid} ({verdicts[iid]['reason']})")
    return out


def digest(res: dict, cfg: dict) -> str | None:
    """One Telegram message — only when something changed or topics are running low."""
    low = (cfg.get("lifecycle") or {}).get("low_new_topics", 21)
    parts = []
    if res["promoted"]:
        parts.append("⬆️ new winner" + ("s" if len(res["promoted"]) > 1 else "") + ": " + ", ".join(res["promoted"]))
    if res["demoted"]:
        parts.append("↩️ winner cooled off, trying again: " + ", ".join(res["demoted"]))
    if res["retired"]:
        parts.append("🗑 thrown out: " + ", ".join(res["retired"]))
    if res.get("flagged"):
        parts.append("🚩 flagged by Instagram twice — blocked until reworded: " + ", ".join(res["flagged"]))
    if res.get("cleared"):
        parts.append("✅ reworded, back in testing: " + ", ".join(res["cleared"]))
    if res.get("free_new", 0) < low:
        parts.append(f"⚠️ only {res['free_new']} new topics left (winners: {res.get('winners', 0)}, "
                     f"blocked: {len(res.get('blocked') or [])}) — write new topics")
    if not parts:
        return None
    return "📊 Topic review\n" + "\n".join(parts) + (f"\n(typical reel = {res['baseline']:,.0f} views)" if res.get("baseline") else "")
