"""Send a snapshot of the schedule to the relay so the dashboard can show it.

The dashboard (served by the Cloudflare relay) shows every reel at its time slot with its
title, fact boxes, clip, music, status, your notes on that clip and, once posted, its stats.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import requests

from .config import load_bank, load_clips, load_json, load_posts

KEEP = ["id", "slot_key", "slot_iso", "slot_label", "post_type", "status", "item_id", "clip_id", "category",
        "mode", "seconds", "msg_id", "file_id", "replaces", "replaced_by", "redo", "needs_replacement",
        "feedback", "extra", "decided_at", "permalink", "post_now"]
STATS = ["views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time"]


def build_state(cfg, q=None) -> dict:
    from .approve import FEEDBACK_PATH, chat_id, load_queue
    from .cli import _slots
    q = q if q is not None else load_queue()
    bank = {i["id"]: i for i in load_bank()}
    posts = load_posts()
    by_slot = {p.get("slot"): p for p in posts}
    entries = []
    for e in q:
        d = {k: e.get(k) for k in KEEP if e.get(k) is not None}
        it = bank.get(e["item_id"], {})
        d["title"] = it.get("hook")
        d["beats"] = it.get("beats", [])
        if it.get("text_edited_at"):
            d["text_edited"] = True  # Nick's wording (dashboard "Edit text"), not the bank's
        if e.get("music"):
            m = e["music"]
            d["music"] = {"title": m.get("title"), "creator": m.get("creator"), "genre": m.get("genre"),
                          "page": m.get("page")}
        p = by_slot.get(e["slot_key"]) if e.get("status") == "posted" else None
        if p:
            d["permalink"] = p.get("permalink") or d.get("permalink")
            d["posted_at"] = p.get("posted_at")
            met = p.get("metrics") or {}
            d["stats"] = {k: met.get(k) for k in STATS if met.get(k) is not None}
            if p.get("clicks_attr") is not None:
                d["stats"]["link_clicks"] = p.get("clicks_attr")
            if p.get("music_title"):
                d.setdefault("music", {"title": p.get("music_title"), "creator": p.get("music_creator")})
        entries.append(d)
    notes = {}
    for f in load_json(FEEDBACK_PATH, []):
        notes.setdefault(f.get("clip_id"), []).append({"at": f.get("at"), "text": f.get("text"),
                                                       "item_id": f.get("item_id")})
    notes = {k: v[-6:] for k, v in notes.items()}
    # topics: the weekly no-repeat rule + the daily winners/new mix
    from datetime import timedelta
    from .picker import topic_kind
    from .scoring import parse_ts
    now = datetime.now(timezone.utc)
    days = (cfg.get("picker") or {}).get("topic_cooldown_days", 7)
    used = {p["item_id"] for p in posts if p.get("posted_at") and parse_ts(p["posted_at"]) > now - timedelta(days=days)}
    used |= {e["item_id"] for e in q if e.get("status") in ("pending", "approved")}
    counts = {"winner": {"active": 0, "free": 0}, "new": {"active": 0, "free": 0}}
    review = []
    for it in bank.values():
        k = topic_kind(it)
        if it.get("status", "active") == "active":
            counts[k]["active"] += 1
            counts[k]["free"] += it["id"] not in used
        elif it.get("status") == "review" and it["id"].startswith("nw-"):
            review.append({"id": it["id"], "hook": it.get("hook"), "beats": it.get("beats", []),
                           "caption": it.get("caption"), "category": it.get("category")})
    # clips: Nick's permanent settings (Clips tab) + how often each one has been used
    uses = {}
    for p in posts:
        if p.get("clip_id"):
            uses.setdefault(p["clip_id"], []).append(p.get("posted_at") or "")
    clips_out = []
    for c in load_clips():
        u = sorted(x for x in uses.get(c["id"], []) if x)
        clips_out.append({"id": c["id"], "status": c.get("status"), "duration": c.get("duration"),
                          "title_y": c.get("title_y"), "start": c.get("start") or 0, "end": c.get("end"),
                          "events_manual": c.get("events_manual"),
                          "auto_events": [{"t": e.get("t"), "dir": e.get("dir")} for e in ((c.get("auto") or {}).get("events") or [])],
                          "uses": len(u), "last_used": u[-1] if u else None})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "timezone": cfg.get("timezone", "America/New_York"),
            "chat_id": chat_id(cfg), "schedule": [{"time": t, "type": k} for t, k in _slots(cfg)],
            "entries": entries, "clip_notes": notes, "clips": clips_out,
            "topics": {"counts": counts, "cooldown_days": days, "review": review}}


def push(cfg, q=None) -> None:
    relay = (cfg.get("approvals") or {}).get("relay_url")
    if not relay:
        return
    from .approve import _token
    r = requests.post(relay.rstrip("/") + "/state", json=build_state(cfg, q), timeout=60,
                      headers={"Authorization": "Bearer " + hashlib.sha256(_token().encode()).hexdigest()})
    if r.status_code >= 300:
        print(f"[dashboard] push → {r.status_code} {r.text[:120]}")
