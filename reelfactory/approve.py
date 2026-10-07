"""Approve reels in Telegram before they post.

Every evening `queue` makes the next day's reels and sends each one to Nick's
Telegram chat with ✅ Approve / ❌ Skip buttons. At post time `post` only
publishes a reel that was approved. Replying to a reel's message with text saves
it as feedback (data/feedback.json) so placement/timing can be fixed later, and
counts as a skip: `replies` then makes a replacement reel for that time slot.
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
    raw = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    import re
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)  # tolerate spaces, line breaks or the whole BotFather message
    token = m.group(0) if m else raw.strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret is not set")
    if not m:
        print(f"[telegram] the saved token doesn't look like a bot token (length {len(raw)})")
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
                note = "Got it 👍 saved for the next fixes."
                if e.get("status") != "posted":  # feedback = skip this one and send a replacement
                    e["status"] = "skipped"
                    e["decided_at"] = datetime.now(timezone.utc).isoformat()
                    soon = datetime.fromisoformat(e["slot_iso"]) <= datetime.now(timezone.utc) + timedelta(minutes=15)
                    if not e.get("replaced_by") and not soon:
                        e["needs_replacement"] = True
                        note = "Got it 👍 saved. Skipped this one — a replacement is on the way (within ~30 min)."
                    else:
                        note = "Got it 👍 saved. Skipped this one."
                    try:
                        _tg("editMessageCaption", chat_id=m["chat"]["id"], message_id=e["msg_id"],
                            caption=_caption(e), reply_markup=_buttons(e["id"]))
                    except Exception as ex:
                        print(f"[telegram] couldn't update message: {ex}")
                try:
                    _tg("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"], text=note)
                except Exception:
                    pass
    save_json(TG_STATE, st)
    save_json(FEEDBACK_PATH, fb)
    save_queue(q)


def _caption(e: dict) -> str:
    status = {"pending": "⏳ waiting for you", "approved": "✅ approved — will post", "skipped": "❌ skipped",
              "posted": "📤 posted", "expired": "⌛ not approved in time"}.get(e.get("status"), e.get("status"))
    if e.get("status") == "skipped" and (e.get("needs_replacement") or e.get("replaced_by")):
        status += " (your notes) — replacement sent" if e.get("replaced_by") else " (your notes) — replacement coming"
    if e.get("replaces"):
        status += "\n🔁 replacement"
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
        e = _queue_one(cfg, cid, dt, key, kind, bank, clips, hist, q, rng, out_dir)
        made += 1
        print(f"[queue] {key} {kind}: {e['item_id']} on {e['clip_id']} -> sent to Telegram")
    if made:
        _tg("sendMessage", chat_id=cid, text=f"👆 {made} reels for {day:%A}. Approve the ones you like — "
                                              "only approved reels get posted. No answer = not posted.")
    return made


def _queue_one(cfg, cid, dt, key, kind, bank, clips, hist, q, rng, out_dir, extra=False):
    from .cli import _make
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
    if extra:
        e["extra"] = True
    save_queue(q)
    return e


def pending_replacements(q=None) -> list[dict]:
    """Reels Nick replied to (= skipped) whose slot is still far enough away to send a new one."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=15)
    return [e for e in (q if q is not None else load_queue())
            if e.get("needs_replacement") and datetime.fromisoformat(e["slot_iso"]) > soon]


def make_replacements(cfg) -> int:
    """Render a new reel for every slot Nick replied to, and send it to Telegram."""
    sync_updates(cfg)
    cid = chat_id(cfg)
    q = load_queue()
    todo = pending_replacements(q)
    for e in q:  # too late for these
        if e.get("needs_replacement") and e not in todo:
            e["needs_replacement"] = False
    if not todo or not cid:
        save_queue(q)
        return 0
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    # avoid repeating queued picks AND the reels he just turned down
    hist = list(load_posts()) + [{"item_id": e["item_id"], "category": e.get("category"), "clip_id": e["clip_id"],
                                  "posted_at": e["slot_iso"]} for e in q
                                 if e.get("status") in ("pending", "approved") or e.get("needs_replacement")]
    bank, clips = load_bank(), load_clips()
    rng = random.Random()
    made = 0
    for e in todo:
        dt = datetime.fromisoformat(e["slot_iso"]).astimezone(tz)
        new = _queue_one(cfg, cid, dt, e["slot_key"], e["post_type"], bank, clips, hist, q, rng, OUT_DIR / "queue",
                         extra=bool(e.get("extra")))
        new["replaces"] = e["id"]
        e["needs_replacement"] = False
        e["replaced_by"] = new["id"]
        save_queue(q)
        try:  # the replacement caption says so; the old one now says "replacement sent"
            _tg("editMessageCaption", chat_id=cid, message_id=new["msg_id"], caption=_caption(new),
                reply_markup=_buttons(new["id"]))
            _tg("editMessageCaption", chat_id=cid, message_id=e["msg_id"], caption=_caption(e),
                reply_markup=_buttons(e["id"]))
        except Exception:
            pass
        made += 1
        print(f"[replace] {e['slot_key']}: {e['item_id']} on {e['clip_id']} -> {new['item_id']} on {new['clip_id']}")
    return made


def add_extras(cfg, spec: str) -> int:
    """Extra reels for today, e.g. "regular@21:30,trial@22:30" (Eastern time)."""
    cid = chat_id(cfg)
    if not cid:
        sync_updates(cfg)
        cid = chat_id(cfg)
    if not cid:
        raise RuntimeError("Telegram chat not connected yet")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    today = datetime.now(timezone.utc).astimezone(tz).date()
    q = load_queue()
    posts = load_posts()
    hist = list(posts) + [{"item_id": e["item_id"], "category": e.get("category"), "clip_id": e["clip_id"],
                           "posted_at": e["slot_iso"]} for e in q if e.get("status") in ("pending", "approved")]
    bank, clips = load_bank(), load_clips()
    rng = random.Random()
    made = 0
    for n, part in enumerate([p.strip() for p in spec.split(",") if p.strip()], 1):
        kind, _, hhmm = part.partition("@")
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(today.year, today.month, today.day, hh, mm, tzinfo=tz)
        key = f"{dt:%Y-%m-%d %H:%M} extra{n}-{os.urandom(2).hex()}"
        _queue_one(cfg, cid, dt, key, kind.strip() or "regular", bank, clips, hist, q, rng, OUT_DIR / "queue", extra=True)
        made += 1
    if made:
        _tg("sendMessage", chat_id=cid, text=f"👆 {made} extra test reel(s) for tonight. Approve the ones you want posted.")
    return made


def due_entries(cfg, now=None, grace_min: int = 120) -> list[dict]:
    """Approved reels whose time has come (and that haven't been posted)."""
    try:
        sync_updates(cfg)
    except Exception as ex:
        print(f"[telegram] couldn't read approvals: {ex}")
    now = now or datetime.now(timezone.utc)
    posted = {p.get("slot") for p in load_posts()}
    out = []
    for e in sorted(load_queue(), key=lambda e: e["slot_iso"]):
        t = datetime.fromisoformat(e["slot_iso"])
        if e.get("status") == "approved" and e["slot_key"] not in posted and t <= now < t + timedelta(minutes=grace_min):
            posted.add(e["slot_key"])  # one reel per slot, even if an old + replacement were both approved
            out.append(e)
        elif t <= now < t + timedelta(minutes=grace_min) and e.get("status") == "pending":
            print(f"[approvals] {e['slot_key']} wasn't approved — not posting")
    return out


def approved_for(cfg, slot_key: str) -> dict | None:
    """At post time: the approved queue entry for this slot (or None)."""
    try:
        sync_updates(cfg)
    except Exception as ex:
        print(f"[telegram] couldn't read approvals: {ex}")
    q = load_queue()
    same = [x for x in q if x["slot_key"] == slot_key]
    e = next((x for x in same if x.get("status") == "approved"), same[-1] if same else None)
    if not e:
        print(f"[approvals] nothing was queued for {slot_key}")
        return None
    if e.get("status") != "approved":
        print(f"[approvals] {slot_key} is '{e.get('status')}' — not posting")
        return None
    return e


def mark(slot_key: str, status: str, permalink: str | None = None, cfg=None) -> None:
    q = load_queue()
    same = [e for e in q if e["slot_key"] == slot_key]
    approved = [e for e in same if e.get("status") == "approved"]
    for e in approved or same:  # a skipped original keeps its "skipped" status
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
