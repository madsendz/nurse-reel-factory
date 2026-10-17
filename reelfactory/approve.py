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
from .config import ROOT, OUT_DIR, load_bank, load_clips, load_json, load_posts, save_clips, save_json

QUEUE_PATH = ROOT / "data" / "queue.json"
TG_STATE = ROOT / "data" / "telegram.json"
FEEDBACK_PATH = ROOT / "data" / "feedback.json"
API = "https://api.telegram.org/bot{token}/{method}"


def enabled(cfg) -> bool:
    return bool((cfg.get("approvals") or {}).get("enabled"))


def _token() -> str:
    raw = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    import re
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)  # tolerate spaces, line breaks or the whole BotFather message
    token = m.group(0) if m else raw.strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret is not set")
    if not m:
        print(f"[telegram] the saved token doesn't look like a bot token (length {len(raw)})")
    return token


def _get_updates(cfg, offset: int) -> list[dict]:
    """Button taps + messages. With a relay (Cloudflare Worker that answers taps instantly),
    read them from the relay; otherwise poll Telegram directly."""
    relay = (cfg.get("approvals") or {}).get("relay_url")
    if relay:
        import hashlib
        r = requests.get(relay.rstrip("/") + "/updates", params={"offset": offset}, timeout=60,
                         headers={"Authorization": "Bearer " + hashlib.sha256(_token().encode()).hexdigest()})
        r.raise_for_status()
        return r.json()
    return _tg("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"])


APPROVE_WORDS = {"approve", "approved", "yes", "post", "post it", "ok", "good", "✅", "👍"}


def _tg(method: str, files=None, **data):
    token = _token()
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
    updates = _get_updates(cfg, st.get("offset", 0))
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
            if reply and reply in by_msg and text.lower().strip(" .!") in APPROVE_WORDS:
                e = by_msg[reply]  # "approve" as a reply = same as tapping ✅
                if e.get("status") != "posted":
                    e["status"] = "approved"
                    e["decided_at"] = datetime.now(timezone.utc).isoformat()
                    try:
                        _tg("editMessageCaption", chat_id=m["chat"]["id"], message_id=e["msg_id"],
                            caption=_caption(e), reply_markup=_buttons(e["id"]))
                        if not (cfg.get("approvals") or {}).get("relay_url"):  # the relay already said so
                            _tg("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"],
                                text="Approved ✅ it will post at its time.")
                    except Exception:
                        pass
            elif reply and reply in by_msg and text:
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
                    if not (cfg.get("approvals") or {}).get("relay_url"):  # the relay already answered
                        _tg("sendMessage", chat_id=m["chat"]["id"], reply_to_message_id=m["message_id"], text=note)
                except Exception:
                    pass
    _apply_dashboard_actions(cfg, q, by_id, st, fb)
    save_json(TG_STATE, st)
    save_json(FEEDBACK_PATH, fb)
    save_queue(q)
    try:
        from . import dashboard
        dashboard.push(cfg, q)
    except Exception as ex:
        print(f"[dashboard] push failed: {ex}")


REDO = {"swap_music": "new music", "new_clip": "same topic, new clip", "new_topic": "same clip, new topic",
        "text_edit": "your text edit"}


def _apply_dashboard_actions(cfg, q, by_id, st, fb) -> None:
    """Approve / skip / notes / redo / move / post-now clicked on the dashboard (via the relay)."""
    relay = (cfg.get("approvals") or {}).get("relay_url")
    if not relay:
        return
    import hashlib
    try:
        r = requests.get(relay.rstrip("/") + "/actions", params={"after": st.get("action_after", "")}, timeout=60,
                         headers={"Authorization": "Bearer " + hashlib.sha256(_token().encode()).hexdigest()})
        r.raise_for_status()
        acts = r.json()
    except Exception as ex:
        print(f"[dashboard] couldn't read actions: {ex}")
        return
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    for a in acts:
        st["action_after"] = a["key"]
        kind = a.get("kind")
        if kind in ("topic_keep", "topic_toss") and a.get("item_id"):  # topic review on the dashboard
            from .config import TOPIC_STATUS_PATH
            ts = load_json(TOPIC_STATUS_PATH, {})
            ts[a["item_id"]] = "active" if kind == "topic_keep" else "retired"
            save_json(TOPIC_STATUS_PATH, ts)
            print(f"[dashboard] topic {a['item_id']} -> {ts[a['item_id']]}")
            continue
        if kind == "clip_edit" and a.get("clip_id"):  # Clips tab: Nick's permanent settings for a clip
            clips = load_clips()
            cl = next((x for x in clips if x["id"] == a["clip_id"]), None)
            if cl:
                for k, v in (a.get("patch") or {}).items():
                    if k not in ("events_manual", "title_y", "start", "end"):
                        continue
                    if v is None:
                        cl.pop(k, None)  # back to the default / the bot's own detection
                    else:
                        cl[k] = v
                save_clips(clips)
                print(f"[dashboard] clip {a['clip_id']} settings saved: {a.get('patch')}")
            continue
        e = by_id.get(a.get("entry_id"))
        if not e or e.get("status") == "posted":
            continue
        print(f"[dashboard] {kind} on {e['slot_key']} ({e['item_id']})")
        if kind == "text_edit":  # Nick changed the title / fact lines on the dashboard
            hook = str(a.get("hook") or "").strip()
            beats = [str(b).strip() for b in (a.get("beats") or []) if str(b).strip()]
            if not hook:
                continue
            from .config import TOPIC_TEXT_PATH
            tt = load_json(TOPIC_TEXT_PATH, {})
            tt[e["item_id"]] = {"hook": hook, "beats": beats, "at": now.isoformat()}  # sticks for this topic from now on
            save_json(TOPIC_TEXT_PATH, tt)
            print(f"[dashboard] text for {e['item_id']} saved: {hook!r} + {len(beats)} line(s)")
            e["status"] = "skipped"
            e["decided_at"] = now.isoformat()
            e["redo"] = "text_edit"
            if not e.get("replaced_by"):
                e["needs_replacement"] = True
        elif kind in ("approve", "skip"):
            e["status"] = "approved" if kind == "approve" else "skipped"
            e["decided_at"] = now.isoformat()
        elif kind == "note" and (a.get("text") or "").strip():
            text = a["text"].strip()
            e.setdefault("feedback", []).append(text)
            fb.append({"at": now.isoformat(), "item_id": e["item_id"], "clip_id": e["clip_id"],
                       "slot": e["slot_key"], "text": text, "via": "dashboard"})
            e["status"] = "skipped"
            e["decided_at"] = now.isoformat()
            if not e.get("replaced_by") and datetime.fromisoformat(e["slot_iso"]) > now + timedelta(minutes=15):
                e["needs_replacement"] = True
        elif kind in REDO:
            e["status"] = "skipped"
            e["decided_at"] = now.isoformat()
            e["redo"] = kind
            if not e.get("replaced_by"):
                e["needs_replacement"] = True
        elif kind == "move" and a.get("slot_iso"):
            dt = datetime.fromisoformat(a["slot_iso"]).astimezone(tz)
            e["slot_iso"] = dt.astimezone(timezone.utc).isoformat()
            e["slot_label"] = dt.strftime("%a %b %d, %-I:%M %p")
            e["slot_key"] = f"{dt:%Y-%m-%d %H:%M} moved-{os.urandom(2).hex()}"
            e["status"] = "pending"  # any change = approve again
            e.pop("post_now", None)
        elif kind == "swap_type":  # trial <-> regular (Nick); the reel is re-captioned at post time
            e["post_type"] = "regular" if e.get("post_type") == "trial" else "trial"
            e["status"] = "pending"  # any change = approve again
            e["decided_at"] = now.isoformat()
            e.pop("post_now", None)
        elif kind == "post_now":
            if e.get("status") == "approved":
                e["post_now"] = True
        else:
            continue
        cid = chat_id(cfg)
        if cid and e.get("msg_id"):
            try:
                _tg("editMessageCaption", chat_id=cid, message_id=e["msg_id"], caption=_caption(e),
                    reply_markup=_buttons(e["id"]))
            except Exception:
                pass


def _caption(e: dict) -> str:
    status = {"pending": "⏳ waiting for you", "approved": "✅ approved — will post", "skipped": "❌ skipped",
              "posted": "📤 posted", "expired": "⌛ not approved in time",
              "missed": "⏭ not posted (another reel took this time slot)"}.get(e.get("status"), e.get("status"))
    if e.get("status") == "skipped" and (e.get("needs_replacement") or e.get("replaced_by")):
        why = REDO.get(e.get("redo"), "your notes")
        status += f" ({why}) — new version sent" if e.get("replaced_by") else f" ({why}) — new version coming"
    if e.get("replaces"):
        status += "\n🔁 replacement"
    kind = "TRIAL reel" if e["post_type"] == "trial" else "Regular reel"
    mu = f"\n🎵 {e['music']['title']} — {e['music']['creator']}" if e.get("music") else ""
    return (f"{e['slot_label']} · {kind}\n{e['item_id']} on {e['clip_id']}{mu}\nStatus: {status}\n\n"
            "Tap ✅ to post it or ❌ to skip. Reply to this video with any notes (text placement, timing).")


def _slot_list(cfg, day: datetime):
    from .cli import _slots
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    for t, kind in _slots(cfg):
        hh, mm = map(int, t.split(":"))
        dt = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
        yield dt, dt.strftime("%Y-%m-%d %H:%M"), kind


def make_queue(cfg, days_ahead: int = 1) -> int:
    """Render the reels for the next `days_ahead` days (only the slots not made yet) and send them
    to Telegram for approval. 0 = the rest of today. Nick approves several days at a time."""
    from .cli import _make
    try:
        sync_updates(cfg)  # pick up approvals and dashboard topic decisions first
    except Exception as ex:
        print(f"[telegram] sync failed: {ex}")
    cid = chat_id(cfg)
    if not cid:
        raise RuntimeError("Telegram chat not connected yet: open your bot in Telegram and press Start, then run again")
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    today = datetime.now(timezone.utc).astimezone(tz).date()
    offsets = range(1, days_ahead + 1) if days_ahead >= 1 else [0]
    days = [today + timedelta(days=k) for k in offsets]
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
    for day in days:
        slots = [s for s in _slot_list(cfg, datetime(day.year, day.month, day.day))
                 if s[1] not in have and s[0] > datetime.now(timezone.utc) + timedelta(minutes=10)]
        plan = _daily_mix(cfg, q, bank, day, len(slots), rng, tz)
        for (dt, key, kind), cls in zip(slots, plan):
            e = _queue_one(cfg, cid, dt, key, kind, bank, clips, hist, q, rng, out_dir, topic_class=cls)
            made += 1
            print(f"[queue] {key} {kind}: {e['item_id']} on {e['clip_id']} -> sent to Telegram")
    if made:
        span = f"{days[0]:%a %b %-d}" if len(days) == 1 else f"{days[0]:%a %b %-d} – {days[-1]:%a %b %-d}"
        _tg("sendMessage", chat_id=cid, text=f"👆 {made} new reel(s) for {span}. Approve the ones you like — "
                                              "only approved reels get posted. No answer = not posted.")
    return made


def _kind_of(bank, item_id) -> str:
    it = next((i for i in bank if i["id"] == item_id), {"id": item_id})
    return pk.topic_kind(it)


def _daily_mix(cfg, q, bank, day, n, rng, tz) -> list[str]:
    """Nick's rule: every day is a mix of proven winners and brand-new topics (half and half by
    default, any slot). Counts reels already queued for that day, then shuffles the rest."""
    mix = (cfg.get("picker") or {}).get("daily_mix") or {"winner": 3, "new": 3}
    have = {"winner": 0, "new": 0}
    for e in q:
        if e.get("status") in ("pending", "approved", "posted") and \
                datetime.fromisoformat(e["slot_iso"]).astimezone(tz).date() == day:
            have[_kind_of(bank, e["item_id"])] += 1
    want = [k for k in ("winner", "new") for _ in range(max(0, int(mix.get(k, 0)) - have[k]))]
    rng.shuffle(want)
    while len(want) < n:  # more slots than the mix covers: alternate
        want.append("new" if want.count("winner") > want.count("new") else "winner")
    return want[:n]


def _queue_one(cfg, cid, dt, key, kind, bank, clips, hist, q, rng, out_dir, extra=False, choice=None,
               topic_class=None):
    from .cli import _make
    choice = choice or pk.pick_next(bank, clips, hist, cfg, now=dt.astimezone(timezone.utc), rng=rng, post_type=kind,
                                    topic_class=topic_class)
    out, caption, meta = _make(cfg, choice, out_dir, key.replace(" ", "_").replace(":", ""), kind)
    e = {"id": os.urandom(4).hex(), "slot_key": key, "slot_iso": dt.astimezone(timezone.utc).isoformat(),
         "slot_label": dt.strftime("%a %b %d, %-I:%M %p"), "post_type": kind,
         "item_id": choice["item"]["id"], "category": choice["item"]["category"], "clip_id": choice["clip"]["id"],
         "cta": choice["cta"], "mode": choice["mode"], "seconds": meta["seconds"], "status": "pending"}
    if meta.get("music"):  # keep the same track for posting
        e["music"] = meta["music"]
    with open(out, "rb") as f:
        msg = _tg("sendVideo", files={"video": (out.name, f, "video/mp4")}, chat_id=cid, caption=_caption(e),
                  supports_streaming="true", reply_markup=_buttons(e["id"]))
    e["msg_id"] = msg["message_id"]
    e["file_id"] = (msg.get("video") or msg.get("document") or {}).get("file_id")  # for the dashboard player
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
        choice = _redo_choice(cfg, e, bank, clips, hist, dt, rng)
        new = _queue_one(cfg, cid, dt, e["slot_key"], e["post_type"], bank, clips, hist, q, rng, OUT_DIR / "queue",
                         extra=bool(e.get("extra")), choice=choice, topic_class=_kind_of(bank, e["item_id"]))
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


def _redo_choice(cfg, e, bank, clips, hist, dt, rng):
    """Dashboard redo buttons keep part of the old reel. A plain note (no redo) keeps the SAME topic,
    clip and music (Nick, 2026-10-13): the new version just picks up the current clip settings /
    fixes, instead of throwing his note away with a random new pick."""
    redo = e.get("redo")
    item = next((i for i in bank if i["id"] == e["item_id"]), None)
    clip = next((c for c in clips if c["id"] == e["clip_id"]), None)
    now = dt.astimezone(timezone.utc)
    if not redo or redo == "text_edit":  # a text edit = same reel, re-drawn with the new words
        if item and clip:
            return {"item": item, "clip": clip, "cta": int(e.get("cta") or 0),
                    "mode": "edited" if redo else "note", "music": e.get("music")}
        return None
    try:
        if redo == "swap_music" and item and clip:
            return {"item": item, "clip": clip, "cta": int(e.get("cta") or 0), "mode": e.get("mode", "redo"),
                    "avoid_music": {(e.get("music") or {}).get("id")}}
        if redo == "new_clip" and item:
            others = [c for c in clips if c["id"] != e["clip_id"]]
            ch = pk.pick_next([item], others, hist, cfg, now=now, rng=rng, post_type=e["post_type"])
            ch["item"] = item
            return ch
        if redo == "new_topic" and clip:
            ch = pk.pick_next(bank, [clip], hist, cfg, now=now, rng=rng, post_type=e["post_type"],
                              exclude_items={e["item_id"]}, topic_class=_kind_of(bank, e["item_id"]))
            ch["clip"] = clip
            return ch
    except Exception as ex:
        print(f"[redo] {redo} failed ({ex}); making a fresh pick instead")
    return None


def rerender_pending(cfg) -> int:
    """Re-render every queued reel that hasn't posted yet, keeping its topic, clip, music, time and
    approval, and swap the video in Telegram + on the dashboard. For renderer changes (fonts, emoji)."""
    from .cli import _make
    try:
        sync_updates(cfg)
    except Exception as ex:
        print(f"[telegram] sync failed: {ex}")
    cid = chat_id(cfg)
    q = load_queue()
    bank, clips = load_bank(), load_clips()
    now = datetime.now(timezone.utc)
    done = 0
    for e in q:
        if e.get("status") not in ("pending", "approved") or datetime.fromisoformat(e["slot_iso"]) <= now:
            continue
        item = next((i for i in bank if i["id"] == e["item_id"]), None)
        clip = next((k for k in clips if k["id"] == e["clip_id"]), None)
        if not item or not clip:
            print(f"[rerender] {e['slot_key']}: topic/clip no longer exists — skipped")
            continue
        choice = {"item": item, "clip": clip, "cta": int(e.get("cta") or 0), "mode": e.get("mode", "redo"),
                  "music": e.get("music")}
        try:
            out, _caption_txt, meta = _make(cfg, choice, OUT_DIR / "queue",
                                            e["slot_key"].replace(" ", "_").replace(":", ""), e["post_type"])
        except Exception as ex:
            print(f"[rerender] {e['slot_key']}: render failed: {ex}")
            continue
        e["seconds"] = meta["seconds"]
        if meta.get("music"):
            e["music"] = meta["music"]
        if cid:
            try:
                with open(out, "rb") as f:
                    msg = _tg("sendVideo", files={"video": (out.name, f, "video/mp4")}, chat_id=cid,
                              caption=_caption(e) + "\n🔁 re-rendered (new look, same reel)",
                              supports_streaming="true", reply_markup=_buttons(e["id"]))
                old = e.get("msg_id")
                e["msg_id"] = msg["message_id"]
                e["file_id"] = (msg.get("video") or msg.get("document") or {}).get("file_id")
                if old:
                    try:
                        _tg("deleteMessage", chat_id=cid, message_id=old)
                    except Exception:
                        pass
            except Exception as ex:
                print(f"[rerender] {e['slot_key']}: Telegram send failed: {ex}")
        done += 1
        save_queue(q)
        print(f"[rerender] {e['slot_key']}: {e['item_id']} on {e['clip_id']} ({e['status']}) re-rendered")
    if done and cid:
        _tg("sendMessage", chat_id=cid, text=f"🔁 {done} queued reel(s) re-rendered with the new look. "
                                           "Approvals were kept.")
    return done

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
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    plan = _daily_mix(cfg, q, bank, today, len(parts), rng, tz)
    for n, (part, cls) in enumerate(zip(parts, plan), 1):
        kind, _, hhmm = part.partition("@")
        hh, mm = map(int, hhmm.split(":"))
        dt = datetime(today.year, today.month, today.day, hh, mm, tzinfo=tz)
        key = f"{dt:%Y-%m-%d %H:%M} extra{n}-{os.urandom(2).hex()}"
        _queue_one(cfg, cid, dt, key, kind.strip() or "regular", bank, clips, hist, q, rng, OUT_DIR / "queue", extra=True,
                   topic_class=cls)
        made += 1
    if made:
        _tg("sendMessage", chat_id=cid, text=f"👆 {made} extra test reel(s) for tonight. Approve the ones you want posted.")
    return made


def due_entries(cfg, now=None, grace_min: int = 120) -> list[dict]:
    """The ONE approved reel to post now. GitHub often starts jobs late, so a single run can find
    several approved reels whose time has passed: post only the newest slot's reel (one per run,
    one per time slot) and mark the older ones as missed instead of posting them all at once."""
    try:
        sync_updates(cfg)
    except Exception as ex:
        print(f"[telegram] couldn't read approvals: {ex}")
    now = now or datetime.now(timezone.utc)
    posted = {p.get("slot") for p in load_posts()}
    q = load_queue()
    rush = [e for e in q if e.get("post_now") and e.get("status") == "approved" and e["slot_key"] not in posted]
    if rush:  # "Post now" on the dashboard: skip the clock
        print(f"[approvals] posting {rush[0]['slot_key']} now (dashboard)")
        return [rush[0]]
    due = []
    for e in q:
        t = datetime.fromisoformat(e["slot_iso"])
        if not (t <= now < t + timedelta(minutes=grace_min)):
            continue
        if e.get("status") == "approved" and e["slot_key"] not in posted:
            due.append(e)
        elif e.get("status") == "pending":
            print(f"[approvals] {e['slot_key']} wasn't approved — not posting")
    if not due:
        return []
    recent = [p for p in load_posts() if p.get("posted_at") and
              datetime.fromisoformat(p["posted_at"]) > now - timedelta(minutes=50)]
    if recent:  # something already went out for this time slot
        print(f"[approvals] a reel was posted {recent[-1]['posted_at']} — waiting for the next slot")
        return []
    # newest time slot wins; inside it, prefer a replacement, then the most recently approved
    due.sort(key=lambda e: (e["slot_iso"], bool(e.get("replaces")), e.get("decided_at") or ""))
    pick = due[-1]
    cid = chat_id(cfg)
    for e in due[:-1]:
        e["status"] = "missed"
        print(f"[approvals] {e['slot_key']} ({e['item_id']}) missed — only one reel per run/slot")
        if cid and e.get("msg_id"):
            try:
                _tg("editMessageCaption", chat_id=cid, message_id=e["msg_id"], caption=_caption(e))
            except Exception:
                pass
    save_queue(q)
    return [pick]


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


def nudge_share(cfg, permalink: str | None, item_id: str, post_type: str = "regular") -> bool:
    """Telegram reminder to share the reel to Facebook from the Instagram app.

    Reels published through the API never get Instagram's "share to Facebook" lane (the one that
    actually gets Facebook views); only a share from the app does. So after each post the bot sends
    a message with an "Open reel" button: tap it, then ⋯ → Share to Facebook."""
    cid = chat_id(cfg or {})
    if not cid:
        print("[nudge] no Telegram chat id yet — press Start in the bot")
        return False
    text = (f"📣 Share to Facebook: the {post_type} reel {item_id} just went up on Instagram.\n"
            f"Open it → tap ⋯ → *Share to Facebook*.")
    kw = {"reply_markup": {"inline_keyboard": [[{"text": "▶️ Open reel on Instagram", "url": permalink}]]}} \
        if permalink else {}
    try:
        _tg("sendMessage", chat_id=cid, text=text, parse_mode="Markdown", disable_web_page_preview=True, **kw)
        return True
    except Exception as ex:  # a failed reminder must never fail the post
        print(f"[nudge] Telegram reminder failed: {ex}")
        return False


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
