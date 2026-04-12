"""
Instagram Agent Dashboard — Enhanced Flask Backend
====================================================
FEATURES ADDED IN THIS VERSION
--------------------------------
  Feature 1 — Multi-Account Support (up to 5 accounts)
    · New `accounts` table in DB
    · posts.account_id foreign key — each post linked to one account
    · Upload accepts account_ids (comma-separated) for multi-account posting
    · /api/post-now fetches correct credentials per-post from accounts table
    · New CRUD routes: /api/accounts

  Feature 2 — Telegram Alerts
    · send_telegram(message) — fires on success or failure
    · Includes account name, caption, time / error in message

  Feature 3 — Auto Slot Assignment
    · assign_next_slot() — finds next free 08:00 / 17:00 UTC slot
    · Upload no longer needs a manual slot number
    · Never overwrites existing scheduled posts

  Feature 4 — Auto-Delete from Cloudinary After Success
    · posts.public_id column stores Cloudinary public_id at upload time
    · delete_from_cloudinary(public_id) called after confirmed "posted"
    · Only runs when status == "posted"; skipped on failure

UNCHANGED
---------
  · All existing route signatures (/api/posts, /api/stats, /api/config, etc.)
  · Frontend UI (templates/index.html)
  · GitHub Actions trigger flow (GET /api/post-now)
  · Event-driven architecture (no background threads)

REQUIRED ENVIRONMENT VARIABLES
--------------------------------
  CLOUDINARY_CLOUD_NAME  — Cloudinary cloud name
  CLOUDINARY_API_KEY     — Cloudinary API key
  CLOUDINARY_API_SECRET  — Cloudinary API secret
  TELEGRAM_BOT_TOKEN     — Bot token from @BotFather
  TELEGRAM_CHAT_ID       — Chat / channel ID for alerts
  DB_PATH                — (optional) SQLite path, default: queue.db
  POST_TIME_1            — (optional) first slot HH:MM UTC, default: 08:00
  POST_TIME_2            — (optional) second slot HH:MM UTC, default: 17:00
  ACCESS_TOKEN           — (optional) fallback IG token if no accounts in DB
  ACCOUNT_ID             — (optional) fallback IG account ID
"""

import os
import time
import sqlite3
import logging
import requests
import cloudinary
import cloudinary.uploader
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# APP SETUP
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

UPLOAD_FOLDER = Path("/tmp/ig_uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Cloudinary ─────────────────────────────────────────────────────────────────
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True,
)

# ── Constants ──────────────────────────────────────────────────────────────────
GRAPH_API_VERSION   = "v19.0"
BASE_URL            = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SLOT_WINDOW_MINUTES = 10   # ±minutes around a slot still considered "due"
MAX_ACCOUNTS        = 5


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = os.environ.get("DB_PATH", "queue.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create / migrate all tables. Idempotent — safe to call on every boot."""
    with get_db() as db:

        # ── accounts (Feature 1) ───────────────────────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL UNIQUE,
                access_token  TEXT    NOT NULL,
                account_id    TEXT    NOT NULL,
                created_at    TEXT    DEFAULT (datetime('now'))
            )""")

        # ── posts ──────────────────────────────────────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                video_url     TEXT,
                public_id     TEXT,
                caption       TEXT,
                account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                scheduled_at  TEXT,
                status        TEXT    DEFAULT 'pending',
                media_id      TEXT,
                error         TEXT,
                posted_at     TEXT,
                created_at    TEXT    DEFAULT (datetime('now'))
            )""")

        # ── config (dashboard-editable settings) ──────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""")

        # ── Safe migrations for pre-existing DBs ──────────────────────────────
        existing_cols = {
            row[1] for row in db.execute("PRAGMA table_info(posts)").fetchall()
        }
        migrations = {
            "video_url":  "TEXT",
            "public_id":  "TEXT",
            "account_id": "INTEGER",
            "posted_at":  "TEXT",
        }
        for col, col_type in migrations.items():
            if col not in existing_cols:
                db.execute(f"ALTER TABLE posts ADD COLUMN {col} {col_type}")
                log.info(f"DB migration: added posts.{col}")

        db.commit()
    log.info("DB initialised.")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_config(key, default=None):
    with get_db() as db:
        row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key, value):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, value))
        db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def utcnow() -> datetime:
    """Current UTC time as a naive datetime (consistent with DB ISO strings)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_post_times() -> list:
    """Return [time1, time2] as 'HH:MM' strings read from DB then env then defaults."""
    t1 = get_config("post_time_1") or os.environ.get("POST_TIME_1", "08:00")
    t2 = get_config("post_time_2") or os.environ.get("POST_TIME_2", "17:00")
    return [t1, t2]


def is_posting_time(now: datetime, post_times: list) -> bool:
    """True if `now` falls within SLOT_WINDOW_MINUTES of any configured slot."""
    for slot_str in post_times:
        h, m = map(int, slot_str.split(":"))
        slot_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((now - slot_dt).total_seconds()) <= SLOT_WINDOW_MINUTES * 60:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 3 — AUTO SLOT ASSIGNMENT
# ══════════════════════════════════════════════════════════════════════════════

def assign_next_slot() -> str:
    """
    Find the next free posting slot (08:00 or 17:00 UTC) and return it as an
    ISO datetime string.

    Rules:
      · Never overwrites an existing pending/posting post's slot
      · Starts from tomorrow to avoid same-day ambiguity
      · Pattern: 08:00 → 17:00 → next day 08:00 → 17:00 → …
    """
    times = get_post_times()

    with get_db() as db:
        taken_rows = db.execute(
            "SELECT scheduled_at FROM posts "
            "WHERE status IN ('pending', 'posting') AND scheduled_at IS NOT NULL"
        ).fetchall()
    taken = {row["scheduled_at"] for row in taken_rows}

    # Start from tomorrow 00:00 UTC
    day = (utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Search up to 90 days = 180 slots (fail-safe upper bound)
    for _ in range(180):
        for slot_str in times:
            h, m = map(int, slot_str.split(":"))
            candidate = day.replace(hour=h, minute=m, second=0, microsecond=0).isoformat()
            if candidate not in taken:
                log.info(f"assign_next_slot → {candidate}")
                return candidate
        day += timedelta(days=1)

    raise RuntimeError("No free slot found in the next 90 days — queue is full.")


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 1 — ACCOUNT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_account(account_db_id: int) -> Optional[dict]:
    """Fetch one account row by its integer PK. Returns None if not found."""
    with get_db() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_db_id,)).fetchone()
    return dict(row) if row else None


def get_all_accounts() -> list:
    with get_db() as db:
        rows = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 2 — TELEGRAM ALERTS
# ══════════════════════════════════════════════════════════════════════════════

def send_telegram(message: str) -> None:
    """
    Send `message` to the configured Telegram chat.
    Errors are logged but never re-raised — alerts must never break posting.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing).")
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Telegram alert sent.")
    except Exception as e:
        log.warning(f"Telegram alert failed (non-fatal): {e}")


def _alert_success(account_name: str, caption: str, media_id: str) -> None:
    send_telegram(
        f"✅ <b>Posted successfully</b>\n"
        f"Account: <b>{account_name}</b>\n"
        f"Caption: {caption[:120]}{'…' if len(caption) > 120 else ''}\n"
        f"Media ID: {media_id}\n"
        f"Time: {utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    )


def _alert_failure(account_name: str, caption: str, error: str) -> None:
    send_telegram(
        f"❌ <b>Post failed</b>\n"
        f"Account: <b>{account_name}</b>\n"
        f"Caption: {caption[:120]}{'…' if len(caption) > 120 else ''}\n"
        f"Error: {error}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 4 — CLOUDINARY AUTO-DELETE
# ══════════════════════════════════════════════════════════════════════════════

def delete_from_cloudinary(public_id: str) -> None:
    """
    Delete a video from Cloudinary by its public_id.
    Called ONLY after a confirmed "posted" status — never on failure.
    Errors are logged but never re-raised.
    """
    if not public_id:
        log.warning("delete_from_cloudinary: empty public_id — skipping.")
        return
    try:
        result = cloudinary.uploader.destroy(public_id, resource_type="video")
        if result.get("result") == "ok":
            log.info(f"🗑  Cloudinary deleted: {public_id}")
        else:
            log.warning(f"Cloudinary delete unexpected result for {public_id}: {result}")
    except Exception as e:
        log.warning(f"Cloudinary delete failed (non-fatal) for {public_id}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# DB POST HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_pending_posts(now: datetime) -> list:
    """
    Return all posts with status='pending' whose scheduled_at <= now.
    Joins the accounts table so credential lookup is a single query.
    """
    with get_db() as db:
        rows = db.execute("""
            SELECT p.*,
                   a.name         AS account_name,
                   a.access_token AS account_token,
                   a.account_id   AS ig_account_id
              FROM posts p
         LEFT JOIN accounts a ON a.id = p.account_id
             WHERE p.status = 'pending'
               AND p.scheduled_at <= ?
          ORDER BY p.scheduled_at ASC
        """, (now.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def update_post_status(
    post_id:  int,
    status:   str,
    media_id: str = None,
    error:    str = None,
) -> None:
    """Atomically update a post's status and related audit fields."""
    posted_at = utcnow().isoformat() if status == "posted" else None
    with get_db() as db:
        db.execute(
            """UPDATE posts
                  SET status    = ?,
                      media_id  = COALESCE(?, media_id),
                      error     = COALESCE(?, error),
                      posted_at = COALESCE(?, posted_at)
                WHERE id = ?""",
            (status, media_id, error, posted_at, post_id),
        )
        db.commit()
    log.info(f"Post {post_id} → {status}" + (f" | media_id={media_id}" if media_id else ""))


# ══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM API HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_credentials(post: dict) -> tuple:
    """
    Return (access_token, ig_account_id) for a post.
    Priority: linked account row → env var fallback (backward compat).
    """
    token   = post.get("account_token") or os.environ.get("ACCESS_TOKEN")
    acct_id = post.get("ig_account_id") or os.environ.get("ACCOUNT_ID")
    if not token or not acct_id:
        raise ValueError(
            f"No Instagram credentials for post id={post['id']}. "
            "Add an account via /api/accounts or set ACCESS_TOKEN / ACCOUNT_ID env vars."
        )
    return token, acct_id


def _create_reel_container(token: str, acct_id: str, video_url: str, caption: str) -> str:
    """Step 1 — Submit video URL to Instagram, returns container ID."""
    if not video_url:
        raise ValueError("video_url is empty — cannot create Instagram container.")
    resp = requests.post(
        f"{BASE_URL}/{acct_id}/media",
        data={
            "media_type":    "REELS",
            "video_url":     video_url,
            "caption":       caption or "",
            "share_to_feed": "true",
            "access_token":  token,
        },
        timeout=60,
    )
    resp.raise_for_status()
    cid = resp.json()["id"]
    log.info(f"  Container created: {cid}")
    return cid


def _wait_for_container(token: str, container_id: str, max_wait: int = 300) -> None:
    """Step 2 — Poll until container is FINISHED. Raises on failure or timeout."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = requests.get(
            f"{BASE_URL}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=20,
        )
        r.raise_for_status()
        sc = r.json().get("status_code", "")
        log.info(f"  Container status: {sc}")
        if sc == "FINISHED":
            return
        if sc in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram container failed with status: {sc}")
        time.sleep(10)
    raise TimeoutError("Instagram container processing timed out after 5 minutes.")


def _publish_container(token: str, acct_id: str, container_id: str) -> str:
    """Step 3 — Publish ready container, returns Instagram media_id."""
    r = requests.post(
        f"{BASE_URL}/{acct_id}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def post_to_instagram(post: dict) -> str:
    """
    Full 3-step Instagram Reels publishing pipeline.
    Automatically resolves credentials from the linked account (Feature 1).
    Returns the Instagram media_id on success; raises on any failure.
    """
    token, acct_id = _resolve_credentials(post)
    video_url = post.get("video_url") or ""
    caption   = post.get("caption") or ""
    label     = post.get("account_name") or acct_id

    log.info(f"▶ Posting post_id={post['id']} to account={label}")
    cid      = _create_reel_container(token, acct_id, video_url, caption)
    _wait_for_container(token, cid)
    media_id = _publish_container(token, acct_id, cid)
    return media_id


# ══════════════════════════════════════════════════════════════════════════════
# /api/post-now — MAIN TRIGGER (called by GitHub Actions)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/post-now")
def api_post_now():
    """
    GitHub Actions calls this at 08:00 and 17:00 UTC daily.

    For each pending post that is due:
      1. Lock row (status → 'posting') to prevent double-posting
      2. Post to Instagram using the correct account credentials
      3. On success: mark 'posted', send Telegram alert, delete Cloudinary video
      4. On failure: mark 'failed', send Telegram alert

    Query params:
      ?force=true  — bypass the ±10min time-window guard (useful for testing)
    """
    now        = utcnow()
    force      = request.args.get("force", "").lower() == "true"
    post_times = get_post_times()

    # Guard — only run near a configured slot
    if not force and not is_posting_time(now, post_times):
        msg = (
            f"Not a posting window (now={now.strftime('%H:%M')} UTC, "
            f"slots={post_times}). Use ?force=true to override."
        )
        log.info(f"[post-now] Skipped — {msg}")
        return jsonify({"status": "skipped", "message": msg, "processed_posts": []}), 200

    pending = get_pending_posts(now)

    if not pending:
        log.info("[post-now] Queue empty — nothing to post.")
        return jsonify({"status": "ok", "message": "No pending posts due.", "processed_posts": []}), 200

    results = []

    for post in pending:
        pid          = post["id"]
        account_name = post.get("account_name") or "unknown"
        caption      = post.get("caption") or ""
        public_id    = post.get("public_id")

        # Lock the row — prevents race condition if trigger fires twice
        update_post_status(pid, "posting")

        try:
            media_id = post_to_instagram(post)

            # ── Success ────────────────────────────────────────────────────────
            update_post_status(pid, "posted", media_id=media_id)
            log.info(f"✅ post_id={pid} | account={account_name} | media_id={media_id}")

            _alert_success(account_name, caption, media_id)          # Feature 2
            if public_id:
                delete_from_cloudinary(public_id)                     # Feature 4

            results.append({
                "post_id":  pid,
                "account":  account_name,
                "status":   "posted",
                "media_id": media_id,
            })

        except Exception as e:
            err = str(e)
            update_post_status(pid, "failed", error=err)
            log.error(f"❌ post_id={pid} | account={account_name} | {err}")

            _alert_failure(account_name, caption, err)                # Feature 2

            results.append({
                "post_id": pid,
                "account": account_name,
                "status":  "failed",
                "error":   err,
            })

    any_failed = any(r["status"] == "failed" for r in results)
    return jsonify({
        "status":          "error" if any_failed else "success",
        "message":         f"Processed {len(results)} post(s).",
        "processed_posts": results,
    }), 207 if any_failed else 200


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE 1 — ACCOUNT MANAGEMENT ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/accounts", methods=["GET"])
def api_accounts_get():
    """List all accounts. Tokens are masked — never returned in full."""
    accounts = get_all_accounts()
    return jsonify([
        {
            "id":            a["id"],
            "name":          a["name"],
            "account_id":    a["account_id"],
            "token_preview": (a["access_token"][:12] + "…") if a.get("access_token") else "",
            "created_at":    a["created_at"],
        }
        for a in accounts
    ])


@app.route("/api/accounts", methods=["POST"])
def api_accounts_create():
    """Add a new account. Max 5 accounts enforced."""
    data         = request.json or {}
    name         = (data.get("name") or "").strip()
    access_token = (data.get("access_token") or "").strip()
    account_id   = (data.get("account_id") or "").strip()

    if not name or not access_token or not account_id:
        return jsonify({"ok": False, "error": "name, access_token, and account_id are all required."}), 400

    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] >= MAX_ACCOUNTS:
            return jsonify({"ok": False, "error": f"Maximum {MAX_ACCOUNTS} accounts reached."}), 400
        try:
            cur = db.execute(
                "INSERT INTO accounts (name, access_token, account_id) VALUES (?,?,?)",
                (name, access_token, account_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify({"ok": False, "error": f"Account name '{name}' already exists."}), 409

    log.info(f"Account created: id={cur.lastrowid} name={name}")
    return jsonify({"ok": True, "id": cur.lastrowid}), 201


@app.route("/api/accounts/<int:acct_id>", methods=["PUT"])
def api_accounts_update(acct_id):
    """Update name / access_token / account_id for an existing account."""
    data = request.json or {}
    fields, values = [], []
    for col in ("name", "access_token", "account_id"):
        if col in data and data[col]:
            fields.append(f"{col}=?")
            values.append(data[col].strip())
    if not fields:
        return jsonify({"ok": False, "error": "No valid fields to update."}), 400
    values.append(acct_id)
    with get_db() as db:
        db.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id=?", values)
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:acct_id>", methods=["DELETE"])
def api_accounts_delete(acct_id):
    """Delete an account. Related posts will have account_id set to NULL."""
    with get_db() as db:
        db.execute("DELETE FROM accounts WHERE id=?", (acct_id,))
        db.commit()
    log.info(f"Account deleted: id={acct_id}")
    return jsonify({"ok": True})


@app.route("/api/accounts/<int:acct_id>/test", methods=["POST"])
def api_accounts_test(acct_id):
    """Ping the Graph API to verify this account's credentials are valid."""
    account = get_account(acct_id)
    if not account:
        return jsonify({"ok": False, "error": "Account not found."}), 404
    try:
        r = requests.get(
            f"{BASE_URL}/{account['account_id']}",
            params={"fields": "name,username", "access_token": account["access_token"]},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        return jsonify({"ok": True, "name": d.get("name"), "username": d.get("username")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════════════════════
# UPLOAD — ENHANCED (Features 1, 3, 4)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """
    Upload a single video and auto-schedule it.

    Form fields:
      video        — video file (required)
      caption      — post caption
      account_ids  — comma-separated account DB ids, e.g. "1" or "1,3"
                     Omit to use the first configured account.
                     Multiple ids → one independently scheduled post per account.

    The video is uploaded to Cloudinary once and the URL is shared across
    all generated post rows (no duplicate uploads).
    """
    caption         = request.form.get("caption", "")
    account_ids_raw = request.form.get("account_ids", "").strip()
    video           = request.files.get("video")

    if not video:
        return jsonify({"ok": False, "error": "No video file attached."}), 400

    # ── Resolve account list ───────────────────────────────────────────────────
    if account_ids_raw:
        try:
            account_ids = [int(x.strip()) for x in account_ids_raw.split(",") if x.strip()]
        except ValueError:
            return jsonify({"ok": False, "error": "account_ids must be comma-separated integers."}), 400
    else:
        accounts = get_all_accounts()
        account_ids = [accounts[0]["id"]] if accounts else [None]

    # ── Save temp file & upload to Cloudinary (Feature 4 prep) ────────────────
    fname = secure_filename(video.filename)
    tmp   = UPLOAD_FOLDER / fname
    video.save(str(tmp))

    try:
        result    = cloudinary.uploader.upload(str(tmp), resource_type="video", folder="ig_dashboard")
        video_url = result["secure_url"]
        public_id = result["public_id"]
        log.info(f"Cloudinary upload OK: {public_id}")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": f"Cloudinary upload failed: {e}"}), 500
    finally:
        tmp.unlink(missing_ok=True)  # always remove the temp file

    # ── Create one post row per account (Feature 1 + Feature 3) ──────────────
    created = []
    with get_db() as db:
        for aid in account_ids:
            # Each account post gets its own independently computed next free slot
            scheduled_at = assign_next_slot()
            cur = db.execute(
                """INSERT INTO posts (video_url, public_id, caption, account_id, scheduled_at, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (video_url, public_id, caption, aid, scheduled_at),
            )
            db.commit()
            log.info(f"Post queued: id={cur.lastrowid} account_id={aid} slot={scheduled_at}")
            created.append({
                "post_id":      cur.lastrowid,
                "account_id":   aid,
                "scheduled_at": scheduled_at,
            })

    return jsonify({"ok": True, "posts_created": created}), 201


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING ROUTES (UNCHANGED SIGNATURES)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/posts")
def api_posts():
    """All posts joined with account name for the dashboard table."""
    with get_db() as db:
        rows = db.execute("""
            SELECT p.*, a.name AS account_name
              FROM posts p
         LEFT JOIN accounts a ON a.id = p.account_id
          ORDER BY p.scheduled_at ASC, p.id ASC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    with get_db() as db:
        row = db.execute("""
            SELECT COUNT(*)               AS total,
                   SUM(status='pending')  AS pending,
                   SUM(status='posted')   AS posted,
                   SUM(status='failed')   AS failed
              FROM posts
        """).fetchone()
    stats = dict(row)
    pending = stats.get("pending") or 0
    stats["days_remaining"] = pending // 2 + (1 if pending % 2 else 0)
    return jsonify(stats)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify({
        "post_time_1": get_config("post_time_1", "08:00"),
        "post_time_2": get_config("post_time_2", "17:00"),
    })


@app.route("/api/config", methods=["POST"])
def api_config_set():
    data = request.json or {}
    for key in ["post_time_1", "post_time_2"]:
        if key in data:
            set_config(key, data[key])
    return jsonify({"ok": True})


@app.route("/api/test-connection", methods=["POST"])
def api_test():
    """Quick connectivity test using the first configured account."""
    accounts = get_all_accounts()
    token   = accounts[0]["access_token"] if accounts else os.environ.get("ACCESS_TOKEN")
    acct_id = accounts[0]["account_id"]   if accounts else os.environ.get("ACCOUNT_ID")
    if not token or not acct_id:
        return jsonify({"ok": False, "error": "No credentials configured."})
    try:
        r = requests.get(
            f"{BASE_URL}/{acct_id}",
            params={"fields": "name,username", "access_token": token},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        return jsonify({"ok": True, "name": d.get("name"), "username": d.get("username")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/caption/<int:post_id>", methods=["POST"])
def api_caption(post_id):
    caption = (request.json or {}).get("caption", "")
    with get_db() as db:
        db.execute("UPDATE posts SET caption=? WHERE id=?", (caption, post_id))
        db.commit()
    return jsonify({"ok": True})


@app.route("/api/delete/<int:post_id>", methods=["DELETE"])
def api_delete(post_id):
    """Remove a queued post. Cleans up Cloudinary if not yet posted."""
    with get_db() as db:
        row = db.execute(
            "SELECT public_id, status FROM posts WHERE id=?", (post_id,)
        ).fetchone()
        # Only delete from Cloudinary if the video was never posted
        if row and row["status"] != "posted" and row["public_id"]:
            delete_from_cloudinary(row["public_id"])
        db.execute("DELETE FROM posts WHERE id=?", (post_id,))
        db.commit()
    log.info(f"Post deleted: id={post_id}")
    return jsonify({"ok": True})


@app.route("/api/retry/<int:post_id>", methods=["POST"])
def api_retry(post_id):
    """Re-queue a failed post at the next available slot."""
    scheduled_at = assign_next_slot()
    with get_db() as db:
        db.execute(
            "UPDATE posts SET status='pending', error=NULL, scheduled_at=? "
            "WHERE id=? AND video_url IS NOT NULL",
            (scheduled_at, post_id),
        )
        db.commit()
    return jsonify({"ok": True, "scheduled_at": scheduled_at})


@app.route("/api/daily-log")
def api_daily_log():
    with get_db() as db:
        rows = db.execute("""
            SELECT DATE(scheduled_at)    AS day,
                   SUM(status='posted')  AS posted,
                   SUM(status='pending') AS pending,
                   SUM(status='failed')  AS failed,
                   COUNT(*)              AS total
              FROM posts WHERE scheduled_at IS NOT NULL
          GROUP BY day ORDER BY day
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": utcnow().isoformat()})


# ══════════════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════════════

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 Instagram Dashboard (Enhanced) → http://localhost:{port}\n")
    app.run(debug=False, host="0.0.0.0", port=port)
