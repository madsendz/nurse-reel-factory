# Nurse Reel Factory

Picks, edits and posts Nurse Lexi educational reels on autopilot, and learns which ones
send the most traffic to nurselexi.com/n.

```
 Google Drive clips ──► pick topic + clip ──► render reel ──► post to Instagram
        ▲                      ▲                                   │
        │                      └──── score: /n clicks, views, ◄────┘
   new clips                         shares, saves (daily)
```

| What | Where |
|---|---|
| Settings (post times, pause, look, CTAs) | `config.yaml` |
| Topics / facts (add your own) | `content/bank.yaml` |
| Clips + where text goes on each | `clips/clips.yaml` |
| Post history + stats | `data/posts.json` |
| What's working | `reports/latest.md` (also shown on each Actions run) |

**Schedules (GitHub Actions):** regular reels 9:30 AM, 1 PM, 7:30 PM ET; trial reels 11 AM, 4 PM, 9:30 PM ET · stats + report daily ~6 AM ET ·
new Drive clips synced + auto-tagged Mondays.

**Manual buttons (Actions tab → pick a workflow → Run workflow):**
- *Preview reels*: render the next few reels without posting, then download them from the run
- *Post reel*: tick "force" to post one right now, or "dry run" to just render
- *Sync + tag clips*: pull in new clips you've added and auto-place the text

**Pause everything:** set `paused: true` in `config.yaml`.

See `SETUP.md` for the one-time setup.
