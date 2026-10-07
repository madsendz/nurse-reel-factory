"""Approve reels in Telegram before they post.

Every evening `queue` makes the next day's reels and sends each one to Nick's
Telegram chat with ✅ Approve / ❌ Skip buttons. At post time `post` only
publishes a reel that was approved. Replying to a reel's message with text saves
it as feedback (data/feedback.json) so placement/timing can be fixed later.
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import picker as pk
from .config import ROOT, OUT_DIR, load_bank, load_clips, load_json, load_posts, save_json

QUEUE_PATH = ROOT / "data" / "queue.json"
TG_STATE = ROOT / "data" / "telegram.json"
FEEDBACK_PATH = ROOT / "data" / "feedback.json"
API = "https://api.telegram.org/bot{token}/{method}"


def enabled(cfg) -> bool:
    return bool((cfg.get("approvals") or {}).get("enabled"))


def _tg(method: str, files=None, **data):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret is not set")
    for k, v in list(data.items()):
        if isinstance(v, (dict, list)):
            data[k] = json.dumps(v)
    r = requests.post(API.format(token=token, method=method), data=data, files=files, timeout=120)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body}")
    return body["result"]


def _state() -> dict:
    return load_json(TG_STATE, {"offset": 0, "chat_id": None})


def chat_id(cfg) -> int | None:
    cid = os.environ.get("TELEGRAM_CHAT_ID") or (cfg.get("approvals") or {}).get("telegram_chat_id") or _state().get("chat_id")
    return int(cid) if cid else None


def load_queue() -> list[dict]:
    return load_json(QUEUE_PATH, [])


def save_queue(q: list[dict]) -> None:
    # keep about 10 days of history
    cut = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    save_json(QUEUE_PATH, [e for e in q if e.get("slot_iso", "") >= cut])


def _buttons(entry_id: str):
    return {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"a:{entry_id}"},
                                 {"text": "❌ Skip", "callback_data": f"s:{entry_id}"}]]}


def sync_updates(cfg) -> None:
    """Read button taps + replies from Telegram and store them in the queue."""
    st = _state()
    q = load_queue()
    by_id = {e["id"]: e for e in q}
    by_msg = {e.get("msg_id"): e for e in q if e.get("msg_id")}
    updates = _tg("getUpdates", offset=st.get("offset", 0), timeout=0, allowed_updates=["message", "callback_query"])
    fb = load_json(FEEDBACK_PATH, [])
    for u in updates:
        st["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            cq = u["callback_query"]
            kind, _, eid = (cq.get("data") or "").partition(":")
            e = by_id.get(eid)
            if not e:
                continue
            if e.get("status") in ("posted",):
                continue
            e["status"] = "approved" if kind == "a" else "skipped"
            e["decided_at"] = datetime.now(timezone.utc).isoformat()
            try:
                _tg("answerCallbackQuery", callback_query_id=cq["id"],
                    text="Approved ✅" if kind == "a" else "Skipped ❌")
            except Exception:
                pass  # too old to answer; the edit below still shows the choice
            try:
                _tg("editMessageCaption", chat_id=cq["message"]["chat"]["id"], message_id=cq["message"]["message_id"],
                    caption=_caption(e), reply_markup=_buttons(e["id"]))
            except Exception as ex:
                print(f"[telegram] couldn't update message: {ex}")
        elif "message" in u:
            m = u["message"]
            if not st.get("chat_id"):
                st["chat_id"] = m["chat"]["id"]
                print(f"[telegram] connected to chat {m['chat']['id']}")
            text = (m.get("text") or "").strip()
            reply = (m.get("reply_to_message") or {}).get("message_id")
            if reply and reply in by_msg and text:
                e = by_msg[reply]
                e.setdefault("feedback", []).append(text)
                fb.append({"at": datetime.now(timezone.utc).isoformat(), "item_id": e["item_id"],
                           "clip_id": e["clip_id"], "slot": e["slot_key"], "text": text})
                try:
                    _tg("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"],
                        text="Got it 👍 saved for the next fixes.")
                except Exception:
                    pass
    save_json(TG_STATE, st)
    save_json(FEEDBACK_PATH, fb)
    save_queue(q)


def _caption(e: dict) -> str:
    status = {"pending": "⏳ waiting for you", "approved": "✅ approved — will post", "skipped": "❌ skipped",
              "posted": "📤 posted", "expired": "⌛ not approved in time"}.get(e.get("status"), e.get("status"))
    kind = "TRIAL reel" if e["post_type"] == "trial" else "Regular reel"
    return (f"{e['slot_label']} · {kind}\n{e['item_id']} on {e['clip_id']}\nStatus: {status}\n\n"
            "Tap ✅ to post it or ❌ to skip. Reply to this video with any notes (text placement, timing).")


def _slot_list(cfg, day: datetime):
    from .cli import _slots
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    for t, kind in _slots(cfg):
        hh, mm = map(int, t.split(":"))
        dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        yield dt, dt.strftime("%Y-%m-%d %H:%M"), kind


def make_queue(cfg, days_ahead: int = 1) -> int:
    """Render tomorrow's reels and send them to Telegram for approval."""
    from .cli import _make
    cid = chat_id(cfg)
    if not cid:
        sync_updates(cfg)
        cid = chat_id(cfg)
    if not cid:
        raise RuntimeError("Telegram chat not connected yet: open your bot in Telegram and press Start, then run again")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    day = (datetime.now(timezone.utc).astimezone(tz) + timedelta(days=days_ahead)).date()
    q = load_queue()
    have = {e["slot_key"] for e in q}
    posts = load_posts()
    # pretend queued reels are already posted so tomorrow's picks don't repeat topics/clips
    hist = list(posts) + [{"item_id": e["item_id"], "category": e.get("category"), "clip_id": e["clip_id"],
                           "posted_at": e["slot_iso"]} for e in q if e.get("status") in ("pending", "approved")]
    bank, clips = load_bank(), load_clips()
    rng = random.Random()
    made = 0
    out_dir = OUT_DIR / "queue"
    for dt, key, kind in _slot_list(cfg, datetime(day.year, day.month, day.day)):
        if key in have or dt <= datetime.now(timezone.utc) + timedelta(minutes=10):
            continue
        choice = pk.pick_next(bank, clips, hist, cfg, now=dt.astimezone(timezone.utc), rng=rng, post_type=kind)
        out, caption, meta = _make(cfg, choice, out_dir, key.replace(" ", "_").replace(":", ""))
        e = {"id": os.urandom(4).hex(), "slot_key": key, "slot_iso": dt.astimezone(timezone.utc).isoformat(),
             "slot_label": dt.strftime("%a %b %d, %-I:%M %p"), "post_type": kind,
             "item_id": choice["item"]["id"], "category": choice["item"]["category"], "clip_id": choice["clip"]["id"],
             "cta": choice["cta"], "mode": choice["mode"], "seconds": meta["seconds"], "status": "pending"}
        with open(out, "rb") as f:
            msg = _tg("sendVideo", files={"video": (out.name, f, "video/mp4")}, chat_id=cid, caption=_caption(e),
                      supports_streaming="true", reply_markup=_buttons(e["id"]))
        e["msg_id"] = msg["message_id"]
        q.append(e)
        hist.append({"item_id": e["item_id"], "category": e["category"], "clip_id": e["clip_id"], "posted_at": e["slot_iso"]})
        save_queue(q)
        made += 1
        print(f"[queue] {key} {kind}: {e['item_id']} on {e['clip_id']} -> sent to Telegram")
    if made:
        _tg("sendMessage", chat_id=cid, text=f"👆 {made} reels for {day:%A}. Approve the ones you like — "
                                              "only approved reels get posted. No answer = not posted.")
    return made


def approved_for(cfg, slot_key: str) -> dict | None:
    """At post time: the approved queue entry for this slot (or None)."""
    try:
        sync_updates(cfg)
    except Exception as ex:
        print(f"[telegram] couldn't read approvals: {ex}")
    q = load_queue()
    e = next((x for x in q if x["slot_key"] == slot_key), None)
    if not e:
        print(f"[approvals] nothing was queued for {slot_key}")
        return None
    if e.get("status") != "approved":
        print(f"[approvals] {slot_key} is '{e.get('status')}' — not posting")
        return None
    return e


def mark(slot_key: str, status: str, permalink: str | None = None, cfg=None) -> None:
    q = load_queue()
    for e in q:
        if e["slot_key"] == slot_key:
            e["status"] = status
            if permalink:
                e["permalink"] = permalink
            cid = chat_id(cfg or {})
            if cid and e.get("msg_id"):
                try:
                    _tg("editMessageCaption", chat_id=cid, message_id=e["msg_id"],
                        caption=_caption(e) + (f"\n{permalink}" if permalink else ""))
                except Exception:
                    pass
    save_queue(q)
