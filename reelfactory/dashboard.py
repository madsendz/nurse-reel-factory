"""Send a snapshot of the schedule to the relay so the dashboard can show it.

The dashboard (served by the Cloudflare relay) shows every reel at its time slot with its
title, fact boxes, clip, music, status, your notes on that clip and, once posted, its stats.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import requests

from .config import load_bank, load_json, load_posts

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
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "timezone": cfg.get("timezone", "America/New_York"),
            "chat_id": chat_id(cfg), "schedule": [{"time": t, "type": k} for t, k in _slots(cfg)],
            "entries": entries, "clip_notes": notes}


def push(cfg, q=None) -> None:
    relay = (cfg.get("approvals") or {}).get("relay_url")
    if not relay:
        return
    from .approve import _token
    r = requests.post(relay.rstrip("/") + "/state", json=build_state(cfg, q), timeout=60,
                      headers={"Authorization": "Bearer " + hashlib.sha256(_token().encode()).hexdigest()})
    if r.status_code >= 300:
        print(f"[dashboard] push → {r.status_code} {r.text[:120]}")
