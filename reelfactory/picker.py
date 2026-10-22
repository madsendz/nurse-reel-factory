"""Choose the next reel: topic + clip + CTA.

Uses Thompson-style sampling: every option gets an estimated strength from
past results (shrunk toward its hook style + category average) plus random
noise that's bigger for options with little data. Mostly picks proven winners,
but keeps testing new topics (explore_rate).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from .scoring import feature_estimates, parse_ts, score_posts, SHRINK

NOISE = 0.4  # exploration mostly comes from explore_rate; this just breaks ties


def _sample(est: dict, key: str, prior: float, rng: random.Random) -> float:
    e = est.get(key)
    if e:
        # est = sum(w*s)/(n_eff+SHRINK) was shrunk toward 0; re-shrink toward the prior instead
        mean = (e["est"] * (e["n_eff"] + SHRINK) + SHRINK * prior) / (e["n_eff"] + SHRINK)
        n_eff = e["n_eff"]
    else:
        mean, n_eff = prior, 0.0
    return rng.gauss(mean, NOISE / (1 + n_eff) ** 0.5)


def pick_next(bank: list[dict], clips: list[dict], posts: list[dict], cfg: dict,
              now: datetime | None = None, rng: random.Random | None = None,
              exclude_items: set[str] | None = None, post_type: str = "regular",
              topic_class: str | None = None) -> dict:
    """topic_class: "winner" (proven past reel) or "new" — the daily mix asks for one or the other."""
    now = now or datetime.now(timezone.utc)
    rng = rng or random.Random()
    pc = cfg["picker"]
    scored = score_posts(posts, cfg, now)
    est = feature_estimates(scored)
    history = sorted(posts, key=lambda p: p.get("posted_at", ""))
    exclude_items = exclude_items or set()

    # ---- topic ----
    cooldown = now - timedelta(days=pc.get("topic_cooldown_days", 21))
    recent_items = {p["item_id"] for p in history if p.get("posted_at") and parse_ts(p["posted_at"]) > cooldown}
    candidates = [i for i in bank if i.get("status", "active") == "active"
                  and i["id"] not in recent_items and i["id"] not in exclude_items]
    if topic_class:
        same = [i for i in candidates if topic_kind(i) == topic_class]
        if same:
            candidates = same
        elif candidates:
            print(f"[topics] no unused {topic_class} topics this week — using the other kind")
    if not candidates:  # everything is on cooldown: fall back to the least-recently posted
        print("[topics] every topic was used in the last week — repeating the least recent one (add more topics!)")
        last_used = {p["item_id"]: p["posted_at"] for p in history}
        candidates = sorted([i for i in bank if i.get("status", "active") == "active" and i["id"] not in exclude_items],
                            key=lambda i: last_used.get(i["id"], ""))[:5]
    if not candidates:
        raise RuntimeError("No active topics in content/bank.yaml")

    run = pc.get("max_same_category_in_row", 2)
    last_cats = [p.get("category") for p in history[-run:]]
    if len(last_cats) == run and len(set(last_cats)) == 1:
        filtered = [i for i in candidates if i["category"] != last_cats[0]]
        candidates = filtered or candidates

    posted_ids = {p["item_id"] for p in history}
    mode = "exploit"
    rate = pc.get("trial_explore_rate", 0.6) if post_type == "trial" else pc.get("explore_rate", 0.25)
    if rng.random() < rate:
        fresh = [i for i in candidates if i["id"] not in posted_ids]
        if fresh:
            candidates, mode = fresh, "explore"

    def topic_value(item):
        style_e = est["style"].get(item.get("style"), {}).get("est", 0.0)
        cat_e = est["category"].get(item["category"], {}).get("est", 0.0)
        prior = 0.6 * style_e + 0.4 * cat_e + float(item.get("prior") or 0)  # proven past reels get a head start
        return _sample(est["item_id"], item["id"], prior, rng)

    ranked = sorted(((topic_value(i), i) for i in candidates), key=lambda t: t[0], reverse=True)
    item = ranked[0][1]

    # ---- clip ----
    usable = [c for c in clips if c.get("status", "active") == "active"]
    if not usable:
        raise RuntimeError("No active clips in clips/clips.yaml — run sync-clips and tag them")
    # Nick's rule (2026-10-13): every active clip gets used once before any clip is used again.
    # Candidates = the clips with the fewest uses so far (posted + queued); among those, the ones whose
    # pointing matches the topic's facts best, least-recently-used first, with a little randomness.
    uses, last_use = {}, {}
    for p in history:
        cid = p.get("clip_id")
        if cid:
            uses[cid] = uses.get(cid, 0) + 1
            last_use[cid] = max(last_use.get(cid, ""), p.get("posted_at") or "")
    fewest = min(uses.get(c["id"], 0) for c in usable)
    pool = [c for c in usable if uses.get(c["id"], 0) == fewest]
    recent_clips = [p.get("clip_id") for p in history[-pc.get("clip_cooldown_posts", 4):]]
    pool = [c for c in pool if c["id"] not in recent_clips] or pool
    fits = [c for c in pool if not c.get("categories") or item["category"] in c["categories"]] or pool
    order = sorted(fits, key=lambda c: last_use.get(c["id"], ""))  # oldest use first
    clip = max(order, key=lambda c: round(clip_fit(item, c), 2) + rng.random() * 0.1 - 0.001 * order.index(c))

    # ---- CTA ----
    ctas = cfg["caption"]["ctas"]
    cta = max(range(len(ctas)), key=lambda k: _sample(est["cta"], str(k), 0.0, rng))

    return {"item": item, "clip": clip, "cta": cta, "mode": mode,
            "alternatives": [i["id"] for _, i in ranked[1:4]]}


def topic_kind(item: dict) -> str:
    """'winner' = exact past reel that performed well; 'new' = never-posted idea."""
    return "winner" if (item.get("id", "").startswith("pr-") or float(item.get("prior") or 0) >= 1) else "new"


def clip_fit(item: dict, clip: dict) -> float:
    """Prefer clips where Lexi points exactly as many times as the topic has facts."""
    from .payoffs import slots_for
    nb, slots = len(item.get("beats", [])), slots_for(clip)
    if slots == 0:
        return -0.3                      # no pointing: works, but a pointing match is better
    if slots == nb:
        return 0.0
    if slots < nb:
        return -0.15 * (nb - slots)      # facts get grouped into fewer boxes
    return -0.25 * (slots - nb)          # she'd point at nothing


def build_caption(item: dict, cta_idx: int, cfg: dict, post_type: str = "regular") -> str:
    c = cfg["caption"]
    tags = list(c.get("base_hashtags", [])) + list(c.get("category_hashtags", {}).get(item["category"], []))
    seen, uniq = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    tags = uniq[: c.get("max_hashtags", 6)]
    # Nick 2026-10-22: minimal captions — the title + one CTA (comment or follow), never "link in bio",
    # no fact list, no topic blurb. Trials still open with "follow me if...", so they get a comment CTA.
    ctas = [x for x in c.get("ctas", []) if x]
    if post_type == "trial":
        ctas = [x for x in ctas if not x.lower().startswith("follow")] or ctas
    cta = ctas[cta_idx % len(ctas)] if ctas else ""
    parts = [item["hook"], cta, " ".join(tags)]
    if post_type == "trial":  # Nick: every trial reel's caption starts with "follow me if..."
        openers = [o for o in c.get("trial_openers", []) if o] or ["follow me if you want more nurse-approved facts 🩺"]
        parts.insert(0, openers[sum(map(ord, item["id"])) % len(openers)])  # same opener for the same topic
    return "\n\n".join(p for p in parts if p)[:2200]
