"""Command line entry point.

  python -m reelfactory sync-clips          add new Drive clips to clips/clips.yaml
  python -m reelfactory auto-tag            find where Lexi points in new clips + make contact sheets
  python -m reelfactory refresh             pull IG insights + link clicks, rescore, write report
  python -m reelfactory post [--force]      post the next reel if a time slot is due
  python -m reelfactory preview --n 3       render N upcoming reels to out/ without posting
  python -m reelfactory report              print the performance report
  python -m reelfactory ideas               (optional) have Claude draft new topics for review
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import picker as pk
from .config import (OUT_DIR, ROOT, load_bank, load_clips, load_config, load_posts, save_bank, save_clips,
                     save_posts)
from .scoring import feature_estimates, leaderboard, parse_ts, score_posts

SLOT_GRACE_MIN = 120  # GitHub schedules can run late; a slot stays "due" this long


def _summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def _clip_file(clip: dict) -> Path:
    if clip.get("local_path"):
        return ROOT / clip["local_path"]
    from .drive import download
    return download(clip["drive_id"], clip.get("name", clip["id"]))


# ───────────────────────── commands ─────────────────────────
def cmd_sync_clips(cfg, args):
    from .drive import list_videos
    folder = cfg["drive"]["folder_id"]
    if not folder:
        sys.exit("Set drive.folder_id in config.yaml (or DRIVE_FOLDER_ID secret)")
    clips = load_clips()
    known = {c.get("drive_id") for c in clips}
    added = 0
    for f in list_videos(folder):
        if f["id"] in known:
            continue
        n = len(clips) + 1
        clips.append({"id": f"clip{n:02d}", "drive_id": f["id"], "name": f["name"], "status": "untagged",
                      "point": "", "zone": {"x": 0.08, "y": 0.14, "w": 0.84, "h": 0.40}, "start": 0, "end": None})
        added += 1
    save_clips(clips)
    print(f"Added {added} new clip(s); {len(clips)} total. New clips are 'untagged' until their zone is set.")


def cmd_auto_tag(cfg, args):
    from .autotag import contact_sheet, extract_frames, tag_clip
    clips = load_clips()
    todo = [c for c in clips if args.all or c.get("status") == "untagged"]
    sheet_dir = OUT_DIR / "sheets"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for c in todo:
        try:
            path = _clip_file(c)
            res = tag_clip(path, c)
            c["zone"], c["auto"] = res["zone"], res["auto"]
            try:
                from .autotag import track_clip
                tr = track_clip(path, c, sheet_out=sheet_dir / f"timeline_{c['id']}.jpg")
                c["duration"] = tr["duration"]
                c["auto"]["events"], c["auto"]["face_track"] = tr["events"], tr["face_track"]
                print(f"{c['id']}: {tr['duration']}s, pointing at " +
                      (", ".join(f"{e['t']}s {e['dir']}" for e in tr["events"]) or "none"))
            except Exception as e:
                print(f"{c['id']}: tracking failed {e}")
            if c.get("status") == "untagged":
                c["status"] = "active"
            frame = extract_frames(path, sheet_dir, n=1)[0].rename(sheet_dir / f"{c['id']}.jpg")
            entries.append((c["id"], frame, c["zone"]))
            print(f"{c['id']}: {res['auto']['method']} -> {c['zone']}")
            if not c.get("local_path") and not args.keep:
                path.unlink(missing_ok=True)  # don't fill the runner's disk
        except Exception as e:
            print(f"{c['id']}: FAILED {e}")
        save_clips(clips)
    for k in range(0, len(entries), 16):
        contact_sheet(entries[k:k + 16], sheet_dir / f"sheet_{k // 16 + 1:02d}.jpg")
    for _, f, _ in entries:
        f.unlink(missing_ok=True)
    print(f"Tagged {len(entries)} clip(s). Contact sheets in {sheet_dir}")


def cmd_refresh(cfg, args):
    from .clicks import attribute, update_click_log
    posts = load_posts()
    now = datetime.now(timezone.utc)
    try:
        from .instagram import Instagram
        ig = Instagram(cfg)
        try:  # connection check: shows up in the run log
            me = ig.get(ig.user_id, fields="username,followers_count,media_count")
            quota = ig.publishing_quota()
            print(f"[instagram] connected as @{me.get('username')} ({me.get('followers_count')} followers); "
                  f"publishing quota: {quota.get('data', [{}])[0]}")
        except Exception as e:
            print(f"[instagram] CONNECTION CHECK FAILED: {e}")
        try:
            from . import facebook
            if facebook.enabled(cfg):
                print(f"[facebook] cross-posting to Page {facebook.check(cfg)}")
        except Exception as e:
            print(f"[facebook] CONNECTION CHECK FAILED: {e}")
        for p in posts:
            if not p.get("ig_media_id"):
                continue
            age_d = (now - parse_ts(p["posted_at"])).days
            if age_d > 45 and p.get("metrics"):
                continue  # old reels are settled
            try:
                p["metrics"] = ig.reel_insights(p["ig_media_id"])
                p["metrics_updated_at"] = now.isoformat()
            except Exception as e:  # deleted reel etc.
                print(f"[insights] {p['ig_media_id']}: {e}")
    except Exception as e:
        print(f"[insights] skipped: {e}")
    hourly = update_click_log(cfg)
    attribute(posts, hourly, cfg)
    save_posts(posts)
    md = leaderboard(score_posts(posts, cfg), feature_estimates(score_posts(posts, cfg)))
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "latest.md").write_text(md, encoding="utf-8")
    _summary(md)
    print(md)


def _slots(cfg):
    for t in cfg["post_times"]:
        if isinstance(t, str):
            yield t, "regular"
        else:
            yield t["time"], t.get("type", "regular")


def _due_slot(cfg, posts, now_utc):
    """Return (slot_key, post_type) for the most recent due slot not yet posted, else None."""
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    now = now_utc.astimezone(tz)
    done = {p.get("slot") for p in posts}
    due = []
    for t, kind in _slots(cfg):
        hh, mm = map(int, t.split(":"))
        slot_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        key = slot_dt.strftime("%Y-%m-%d %H:%M")
        if slot_dt <= now < slot_dt + timedelta(minutes=SLOT_GRACE_MIN) and key not in done:
            due.append((slot_dt, key, kind))
    if not due:
        return None
    _, key, kind = max(due)
    return key, kind


def _make(cfg, choice, out_dir: Path, tag: str, post_type: str = "regular"):
    from .render import render_reel
    item, clip = choice["item"], choice["clip"]
    out = out_dir / f"{tag}_{item['id']}_{clip['id']}.mp4"
    meta = render_reel(item, clip, _clip_file(clip), out, cfg)
    caption = pk.build_caption(item, choice["cta"], cfg, post_type)
    from . import music
    if music.baked(cfg):  # royalty-free track mixed into the video (same track at post time as in Telegram)
        track = choice.get("music")
        if not track:
            used = music.recent_ids(load_posts() + _queued(), 15) | {m for m in choice.get("avoid_music", set()) if m}
            track = music.pick_track(cfg, avoid=used)
        if track and music.add_music(out, track, float(meta["seconds"]), cfg):
            meta["music"] = track
            c = music.credit(track)
            if c:
                caption = caption + "\n\n" + c
    return out, caption, meta


def _queued() -> list[dict]:
    try:
        from .approve import load_queue
        return [e for e in load_queue() if e.get("status") in ("pending", "approved")]
    except Exception:
        return []


def cmd_post(cfg, args):
    if cfg.get("paused") and not args.force:
        print("Paused in config.yaml — not posting.")
        return
    posts = load_posts()
    now = datetime.now(timezone.utc)
    from . import approve
    if approve.enabled(cfg) and not args.force:
        entries = approve.due_entries(cfg, now)  # approved in Telegram and due now (regular slots + extras)
        if not entries:
            print("Nothing approved is due right now.")
            return
        for e in entries:
            _post_one(cfg, args, posts, now, e["slot_key"], e.get("post_type", "regular"), e)
        return
    due = _due_slot(cfg, posts, now)
    if not due:
        if not args.force:
            print("No posting slot due right now.")
            return
        due = ("manual " + now.strftime("%Y-%m-%d %H:%M"), "trial" if args.trial else "regular")
    slot, post_type = due
    if args.trial:
        post_type = "trial"
    _post_one(cfg, args, posts, now, slot, post_type, None)


def _post_one(cfg, args, posts, now, slot, post_type, entry):
    if entry:  # post exactly the reel that was approved in Telegram
        item = next((i for i in load_bank() if i["id"] == entry["item_id"]), None)
        clip = next((c for c in load_clips() if c["id"] == entry["clip_id"]), None)
        if not item or not clip:
            print(f"[approvals] approved topic/clip no longer exists ({entry['item_id']}, {entry['clip_id']})")
            return
        choice = {"item": item, "clip": clip, "cta": int(entry.get("cta") or 0), "mode": entry.get("mode", "approved"),
                  "music": entry.get("music")}
    else:
        choice = pk.pick_next(load_bank(), load_clips(), posts, cfg, now, post_type=post_type)
    out, caption, meta = _make(cfg, choice, OUT_DIR, "post_" + slot.replace(" ", "_").replace(":", ""), post_type)
    print(f"[{post_type}] Picked {choice['item']['id']} on {choice['clip']['id']} ({choice['mode']}); rendered {meta['seconds']}s")
    record = {"id": uuid.uuid4().hex[:10], "slot": slot, "item_id": choice["item"]["id"],
              "post_type": post_type, "category": choice["item"]["category"], "style": choice["item"].get("style"),
              "clip_id": choice["clip"]["id"], "cta": str(choice["cta"]), "mode": choice["mode"],
              "seconds": meta["seconds"], "caption": caption}
    if meta.get("music"):
        mu = meta["music"]
        record.update({"music_id": mu["id"], "music_title": mu["title"], "music_creator": mu["creator"],
                       "music_license": mu["license"]})
    if args.dry_run:
        print("Dry run — not publishing.\n\n" + caption)
        return
    from .instagram import Instagram, pick_audio
    ig = Instagram(cfg)
    strategy = cfg.get("trial", {}).get("graduation_strategy", "MANUAL") if post_type == "trial" else None
    from . import music
    audio = None if music.baked(cfg) else pick_audio(ig, posts, cfg, random.Random())
    if audio:
        record.update({"audio_id": audio["audio_id"], "audio_title": audio.get("title"),
                       "audio_artist": audio.get("artist")})
        print(f"[sound] {audio.get('title')} - {audio.get('artist')}")
    # Nick: the cover photo shows the title only (before the first fact box pops up), never the last fully revealed frame
    stages = meta["stages"]
    cover_s = min(stages[1] * 0.5, 1.5) if len(stages) > 1 else 0.6
    thumb = int(max(cover_s, 0.2) * 1000)
    video_url = _host_video(cfg, out) if strategy else None
    if strategy and not video_url:
        raise SystemExit("[trial] couldn't host the video for a trial upload — not posting it as a regular reel")
    try:
        res = ig.publish_reel(out, caption, thumb_offset_ms=thumb, trial_strategy=strategy, audio=audio,
                              video_url=video_url)
    except Exception as e:
        if not audio:
            raise
        print(f"[sound] posting with the sound failed ({e}); posting with the clip's own audio instead")
        for k in ("audio_id", "audio_title", "audio_artist"):
            record.pop(k, None)
        res = ig.publish_reel(out, caption, thumb_offset_ms=thumb, trial_strategy=strategy, video_url=video_url)
    record.update(res)
    record["posted_at"] = (res.get("timestamp") or now.isoformat()).replace("+0000", "+00:00")
    from . import facebook
    if facebook.enabled(cfg) and (post_type != "trial" or (cfg.get("facebook") or {}).get("trials", True)):
        try:  # same reel + caption on the Facebook Page (Instagram API posts never share to Facebook on their own)
            record.update(facebook.publish_reel(out, caption, cfg))
            print(f"[facebook] cross-posted → {record.get('fb_permalink')}")
        except Exception as ex:  # never let Facebook block the Instagram post
            print(f"[facebook] cross-post FAILED: {ex}")
    posts.append(record)
    save_posts(posts)
    if entry:
        from . import approve
        approve.mark(slot, "posted", record.get("permalink"), cfg)
    msg = f"Posted {post_type} reel **{record['item_id']}** on {record['clip_id']} ({record['mode']}) → {record.get('permalink')}"
    if record.get("fb_permalink"):
        msg += f"  · Facebook: {record['fb_permalink']}"
    elif (cfg.get("facebook") or {}).get("share_nudge", True):
        # Nick: API posts never reach Facebook's Reels feed; a share from the IG app does -> remind him in Telegram
        from . import approve
        if approve.nudge_share(cfg, record.get("permalink"), record["item_id"], post_type):
            record["fb_nudged_at"] = now.isoformat()
            save_posts(posts)
            msg += "  · Telegram: share-to-Facebook reminder sent"
    print(msg)
    _summary(msg)


def _host_video(cfg, path) -> str | None:
    """Put the reel on the relay for a day so Instagram can fetch it by URL (trial reels need this)."""
    relay = (cfg.get("approvals") or {}).get("relay_url")
    if not relay:
        return None
    import hashlib
    import requests
    from .approve import _token
    try:
        r = requests.post(relay.rstrip("/") + "/media", data=open(path, "rb").read(), timeout=120,
                          headers={"Authorization": "Bearer " + hashlib.sha256(_token().encode()).hexdigest(),
                                   "content-type": "video/mp4"})
        r.raise_for_status()
        u = r.json()["url"]
        print("[trial] video hosted for upload")
        return u
    except Exception as ex:
        print(f"[trial] hosting failed: {ex}")
        return None


def cmd_preview(cfg, args):
    posts = load_posts()
    bank, clips = load_bank(), load_clips()
    if args.clip:
        clips = [c for c in clips if c["id"] == args.clip]
        for c in clips:
            c["status"] = "active"
    if args.item:
        bank = [i for i in bank if i["id"] == args.item]
        for i in bank:
            i["status"] = "active"
    out_dir = OUT_DIR / "preview"
    rng = random.Random(args.seed)
    ig = None
    if os.environ.get("IG_ACCESS_TOKEN"):
        try:
            from .instagram import Instagram
            ig = Instagram(cfg)
        except Exception as e:
            print(f"[sound] preview can't reach Instagram: {e}")
    used, lines = set(), ["# Upcoming reels (preview)", ""]
    fake_posts = list(posts)
    for k in range(args.n):
        choice = pk.pick_next(bank, clips, fake_posts, cfg, rng=rng, exclude_items=used)
        used.add(choice["item"]["id"])
        out, caption, meta = _make(cfg, choice, out_dir, f"{k + 1:02d}")
        fake_posts.append({"item_id": choice["item"]["id"], "category": choice["item"]["category"],
                           "clip_id": choice["clip"]["id"], "posted_at": datetime.now(timezone.utc).isoformat()})
        sound = ""
        if ig:
            from .instagram import pick_audio
            a = pick_audio(ig, fake_posts, cfg, rng)
            sound = f" · sound: {a.get('title')} - {a.get('artist')}" if a else " · sound: (none found)"
            if a:
                fake_posts[-1]["audio_id"] = a["audio_id"]
        lines += [f"## {k + 1}. {choice['item']['id']} · {choice['clip']['id']} · {choice['mode']} · {meta['seconds']}s{sound}",
                  "", "```", caption, "```", ""]
        print(f"rendered {out.name}")
    (out_dir / "preview.md").write_text("\n".join(lines), encoding="utf-8")
    _summary("\n".join(lines))


def cmd_queue(cfg, args):
    from . import approve
    if str(args.rerender).strip().lower() in ("yes", "true", "1", "y"):
        n = approve.rerender_pending(cfg)
        _summary(f"Re-rendered {n} queued reel(s) with the current look.")
        return
    if args.extra.strip():
        n = approve.add_extras(cfg, args.extra)
    else:
        days = str(args.days_ahead).strip()
        n = approve.make_queue(cfg, days_ahead=int(days) if days else int((cfg.get("queue") or {}).get("days_ahead", 1)))
    _summary(f"Sent {n} reel(s) to Telegram for approval.")


def cmd_approvals(cfg, args):
    from . import approve
    approve.sync_updates(cfg)
    for e in approve.load_queue()[-12:]:
        print(e["slot_key"], e["post_type"], e["item_id"], e["clip_id"], e.get("status"), e.get("feedback", ""))


def cmd_replies(cfg, args):
    from . import approve
    n = approve.make_replacements(cfg)
    _summary(f"Sent {n} replacement reel(s) to Telegram.")


def cmd_clip_previews(cfg, args):
    from .previews import push_previews
    n = push_previews(cfg, force=args.force, only=args.clip)
    print(f"[previews] {n} preview(s) uploaded to the relay")


def cmd_report(cfg, args):
    posts = load_posts()
    s = score_posts(posts, cfg)
    print(leaderboard(s, feature_estimates(s)))


def cmd_ideas(cfg, args):
    from .ideas import generate
    if not cfg["ideas"].get("enabled") and not args.force:
        print("ideas.enabled is false in config.yaml")
        return
    bank = load_bank()
    s = score_posts(load_posts(), cfg)
    new = generate(bank, feature_estimates(s), cfg, n=getattr(args, "n", None))
    save_bank(bank + new)
    try:  # Topics tab shows them right away
        from . import dashboard
        dashboard.push(cfg)
    except Exception as ex:
        print(f"[dashboard] push failed: {ex}")
    md = "## New topic ideas (status: review)\n\n" + "\n".join(f"- **{i['id']}**: {i['hook']}" for i in new)
    _summary(md)
    print(md)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reelfactory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-clips")
    p = sub.add_parser("auto-tag")
    p.add_argument("--all", action="store_true", help="re-tag every clip, not just untagged ones")
    p.add_argument("--keep", action="store_true", help="keep downloaded clips")
    sub.add_parser("refresh")
    p = sub.add_parser("post")
    p.add_argument("--force", action="store_true", help="post now even if no slot is due / paused")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--trial", action="store_true", help="post as a trial reel")
    p = sub.add_parser("preview")
    p.add_argument("--n", type=int, default=3)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--clip", help="only use this clip id")
    p.add_argument("--item", help="only use this topic id")
    sub.add_parser("report")
    p = sub.add_parser("clip-previews")
    p.add_argument("--force", action="store_true", help="remake every preview, not just the missing ones")
    p.add_argument("--clip", help="only this clip id")
    p = sub.add_parser("queue")
    p.add_argument("--days-ahead", default="", help="N = fill the next N days, 0 = rest of today; empty = config queue.days_ahead")
    p.add_argument("--extra", default="", help='extra reels today, e.g. "regular@21:30,trial@22:30"')
    p.add_argument("--rerender", default="", help="yes = redraw every queued reel with the current look (same topic/clip/music)")
    sub.add_parser("approvals")
    sub.add_parser("replies")
    p = sub.add_parser("ideas")
    p.add_argument("--force", action="store_true")
    p.add_argument("--n", type=int, default=None, help="how many ideas (default: ideas.per_run in config.yaml)")
    args = ap.parse_args(argv)
    cfg = load_config()
    {"sync-clips": cmd_sync_clips, "auto-tag": cmd_auto_tag, "refresh": cmd_refresh, "post": cmd_post, "preview": cmd_preview,
     "report": cmd_report, "ideas": cmd_ideas, "queue": cmd_queue, "approvals": cmd_approvals, "clip-previews": cmd_clip_previews,
     "replies": cmd_replies}[args.cmd](cfg, args)
    if args.cmd == "post":
        # GitHub runs one job at a time and cancels a queued replies job when a post job is queued behind it,
        # so a "new clip / new topic / notes" click could leave a skipped reel with no new version. Make them here.
        try:
            from . import approve
            if approve.enabled(cfg) and approve.pending_replacements():
                print(f"[replace] made {approve.make_replacements(cfg)} replacement(s) after posting")
        except Exception as ex:
            print(f"[replace] failed: {ex}")
    if args.cmd in ("post", "queue", "replies", "refresh"):
        try:  # keep the dashboard current
            from . import dashboard
            dashboard.push(cfg)
        except Exception as ex:
            print(f"[dashboard] push failed: {ex}")
