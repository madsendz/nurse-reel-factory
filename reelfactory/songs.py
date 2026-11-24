"""Songs Nick has approved (2026-11-24).

The bot only attaches a song he kept. New trending songs from Instagram are sent to Telegram with
Keep / Toss buttons (he taps the listen link to hear one); each queued reel then shows the song
picked for it, and he can swap it or ban it there. data/songs.json:
{audio_id: {title, artist, link, status: review | keep | toss, added, msg_id}}
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config import ROOT, load_json, save_json

PATH = ROOT / "data" / "songs.json"


def load() -> dict:
    return load_json(PATH, {})


def buttons(aid: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Keep", "callback_data": f"k:{aid}"},
                                 {"text": "❌ Toss", "callback_data": f"t:{aid}"}]]}


def message_text(s: dict) -> str:
    state = {"keep": "✅ kept — the bot can use it", "toss": "❌ tossed — never used"}.get(s.get("status"), "⏳ waiting for you")
    listen = f"\n▶️ Listen: {s['link']}" if s.get("link") else "\n(search it in the Instagram app to hear it)"
    return f"🎵 Trending song\n{s.get('title')} — {s.get('artist')}{listen}\nStatus: {state}"


def as_audio(aid: str, s: dict) -> dict:
    return {"audio_id": aid, "type": "music", "title": s.get("title"), "artist": s.get("artist"), "link": s.get("link")}


def set_status(aid: str, status: str) -> dict | None:
    d = load()
    if aid not in d:
        return None
    d[aid]["status"] = status
    save_json(PATH, d)
    return d[aid]


def recent_ids(posts: list[dict], queue: list[dict], n: int = 6) -> set[str]:
    ids = [str(p["audio_id"]) for p in posts[-n:] if p.get("audio_id")]
    ids += [str(e["audio"]["audio_id"]) for e in queue if e.get("audio") and e.get("status") in ("pending", "approved")]
    return set(ids)


def pick(recent: set[str], rng, avoid: set[str] | None = None) -> dict | None:
    """A kept song, not the ones in `avoid`, and not a recently used one when there are enough to choose from."""
    kept = [(a, s) for a, s in load().items() if s.get("status") == "keep" and a not in (avoid or set())]
    if not kept:
        return None
    fresh = [x for x in kept if x[0] not in recent]
    return as_audio(*rng.choice(fresh or kept))


def for_post(entry: dict | None, posts: list[dict], rng) -> dict | None:
    """The song approved with the reel (unless he tossed it since), else any kept song, else None."""
    d = load()
    a = (entry or {}).get("audio")
    if a and (d.get(str(a.get("audio_id"))) or {}).get("status") == "keep":
        return a
    return pick(recent_ids(posts, []), rng)


def refresh(ig, cfg: dict, cid, tg) -> int:
    """Send new trending songs to Telegram for Keep / Toss (not while many are still waiting)."""
    from . import safety
    s = cfg.get("sound") or {}
    d = load()
    waiting = sum(1 for x in d.values() if x.get("status") == "review")
    room = s.get("review_max", 20) - waiting
    sent = 0
    if room <= 0 or not cid:
        return 0
    for it in ig.trending_audio("music"):
        aid = str(it.get("audio_id") or it.get("id") or "")
        if not aid or aid in d:
            continue
        rec = {"title": it.get("title"), "artist": it.get("display_artist") or it.get("ig_username"),
               "link": it.get("on_platform_audio_preview_link"), "status": "review",
               "added": datetime.now(timezone.utc).isoformat()}
        if safety.banned_hits(f"{rec['title']} {rec['artist']}", cfg):
            rec["status"] = "toss"   # a flag-list word in the song name: never offered
        elif sent < room:
            try:
                rec["msg_id"] = tg("sendMessage", chat_id=cid, text=message_text(rec), reply_markup=buttons(aid),
                                   disable_web_page_preview="true")["message_id"]
                sent += 1
            except Exception as e:
                print(f"[songs] couldn't send {rec['title']}: {e}")
                continue
        else:
            continue  # offered on a later run
        d[aid] = rec
    save_json(PATH, d)
    print(f"[songs] {sent} new song(s) sent for review; {sum(1 for x in d.values() if x.get('status') == 'keep')} kept so far")
    return sent
