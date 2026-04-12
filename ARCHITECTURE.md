# Instagram Dashboard — Refactored Architecture

## What Changed

| | Before | After |
|---|---|---|
| Scheduling | `while True` loop in a daemon thread | GitHub Actions cron |
| Always-on | Yes — thread running 24/7 | No — app is idle between requests |
| Dependencies | `schedule`, `threading` | Removed |
| Trigger | Time-based internal loop | HTTP `GET /api/post-now` |
| Cost | Requires always-on dyno | Runs on free/sleep-able tier |

---

## New Flow (Step by Step)

```
GitHub Actions (cron 08:00 / 17:00 UTC)
         │
         │  GET https://your-app.com/api/post-now
         ▼
    ┌─────────────────────────────────┐
    │         Flask App               │
    │                                 │
    │  1. Check: is it a slot time?   │
    │  2. Query DB for pending posts  │
    │  3. For each due post:          │
    │     a. Mark as "posting"        │
    │     b. Call Instagram API       │
    │     c. Mark "posted" or "failed"│
    │  4. Return JSON report          │
    └─────────────────────────────────┘
         │
         ▼
    Dashboard UI (unchanged)
    Shows updated statuses in real time
```

---

## Setup Instructions

### 1. Deploy the Flask App

Push to GitHub, connect to Render/Railway as before.

**Render free tier will spin down after 15 min of inactivity** — that's fine now,
because GitHub Actions will wake it up via HTTP at posting time.

### 2. Add GitHub Secret

In your GitHub repo: **Settings → Secrets and variables → Actions → New secret**

| Name | Value |
|------|-------|
| `APP_URL` | `https://your-app-name.onrender.com` |

### 3. Push the Workflow File

The `.github/workflows/post.yml` file must be committed to your repo's **default branch**.
GitHub Actions reads it automatically.

### 4. Test Manually

Trigger from GitHub UI:
- Go to **Actions → Instagram Auto-Post → Run workflow**
- Set `force=true` to bypass the time window check

Or hit the endpoint directly:
```bash
curl "https://your-app.onrender.com/api/post-now?force=true"
```

---

## API Reference

### `GET /api/post-now`

Called by GitHub Actions. Posts any pending reels that are due.

**Query params:**
- `?force=true` — bypass the posting-window check (for testing)

**Response:**
```json
{
  "status": "success",
  "message": "Processed 1 post(s).",
  "processed_posts": [
    {
      "slot": 3,
      "post_id": 7,
      "status": "posted",
      "media_id": "17854360229135492"
    }
  ]
}
```

**HTTP status codes:**
- `200` — all posts succeeded (or nothing was due)
- `207` — partial: some posts failed (check `processed_posts` for details)

---

## Duplicate-Post Protection

The endpoint sets a post's status to `"posting"` **before** calling the Instagram API.
If a second trigger fires while the first is still running, it will see `status='posting'`
and skip that row — so you'll never double-post the same reel.

---

## Configuring Post Times

Post times can be changed from the dashboard (Settings tab). They're stored in the DB,
not hardcoded. The GitHub Actions cron (`0 8 * * *` and `0 17 * * *`) should match
whatever times you configure.

To change cron times, edit `.github/workflows/post.yml` and update the two `cron:` lines.
