"""Approval service for phone-farm Marketplace replies.

Receives review items from every shard, shows them in a mobile friendly UI,
records approve/reject decisions, and hands approved replies to the phone
that owns each conversation. The phone re-reads the chat before sending, so
an approval never goes into a conversation that has moved on.

Run it on one machine; shards reach it over HTTP:

    .\\.venv\\Scripts\\python.exe review_service.py --host 0.0.0.0 --port 8770

Then open http://<host>:8770/?token=<REVIEW_SERVICE_SECRET>
"""
import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

DATABASE_PATH = Path(__file__).with_name("review.db")
SECRET = os.getenv("REVIEW_SERVICE_SECRET", "").strip()

# state machine:
#   pending -> approved -> sending -> sent | failed | stale
#   pending | stale | failed | approved -> rejected
#   pending | stale | failed | approved -> superseded   (newer item arrived)
#   approved -> stale    (approval expired before reaching its phone)
#   sending  -> failed   (the phone never reported back)
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
SENDING = "sending"
SENT = "sent"
STALE = "stale"
FAILED = "failed"
SUPERSEDED = "superseded"
# A reply the bot sent on its own, with no review. History only: it is never
# open, approvable, rejectable or delivered.
AUTO_SENT = "auto_sent"

OPEN_STATES = (PENDING, STALE, FAILED)
UNDELIVERED_STATES = OPEN_STATES + (APPROVED,)
SENT_STATES = (SENT, AUTO_SENT)

# An approval that has not reached its phone within this window goes back to
# the queue instead of being sent. The chat may have moved on, and an old
# test approval must never surprise a customer.
APPROVAL_EXPIRY_HOURS = float(os.getenv("APPROVAL_EXPIRY_HOURS") or 6)

# A claimed delivery whose phone never reported back. It may or may not have
# been sent, so it is surfaced to a human instead of being retried.
SENDING_TIMEOUT_MINUTES = float(os.getenv("SENDING_TIMEOUT_MINUTES") or 20)

app = FastAPI(title="Marketplace reply approvals")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@contextlib.contextmanager
def open_database():
    """Open the review database and always close it again."""
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                device_serial TEXT NOT NULL,
                conversation_key TEXT,
                conversation_label TEXT,
                listing_title TEXT,
                sender TEXT,
                latest_message TEXT,
                conversation TEXT,
                proposed_reply TEXT,
                approved_reply TEXT,
                confidence REAL,
                reason TEXT,
                risk_flags TEXT,
                source_status TEXT,
                state TEXT NOT NULL,
                delivered_at TEXT,
                detail TEXT
            )
            """
        )
        # Columns added after the first release of this table.
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(review_items)")
        }

        for column in ("approved_at", "claimed_at", "device_name"):
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE review_items ADD COLUMN {column} TEXT"
                )

        connection.execute(
            "CREATE INDEX IF NOT EXISTS review_items_state "
            "ON review_items (state, device_serial)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS review_items_conversation "
            "ON review_items (device_serial, conversation_key)"
        )
        connection.commit()
        yield connection
    finally:
        connection.close()


def timestamp_ago(seconds):
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.localtime(time.time() - seconds),
    )


def sweep(connection):
    """Expire old approvals and surface deliveries that never reported back."""
    expired = connection.execute(
        "UPDATE review_items SET state = ?, updated_at = ?, detail = ? "
        "WHERE state = ? AND COALESCE(approved_at, updated_at) < ?",
        (
            STALE,
            now(),
            f"Approval expired: not delivered within "
            f"{APPROVAL_EXPIRY_HOURS:g}h. Check the conversation and approve "
            "again if the reply still applies.",
            APPROVED,
            timestamp_ago(APPROVAL_EXPIRY_HOURS * 3600),
        ),
    ).rowcount
    abandoned = connection.execute(
        "UPDATE review_items SET state = ?, updated_at = ?, detail = ? "
        "WHERE state = ? AND COALESCE(claimed_at, updated_at) < ?",
        (
            FAILED,
            now(),
            "The phone started delivering this reply but never reported "
            "back. It may or may not have been sent: check the conversation "
            "on the phone before approving again.",
            SENDING,
            timestamp_ago(SENDING_TIMEOUT_MINUTES * 60),
        ),
    ).rowcount

    if expired or abandoned:
        connection.commit()


def require_secret(supplied):
    if not SECRET:
        raise HTTPException(
            status_code=500,
            detail="REVIEW_SERVICE_SECRET is not set on the service.",
        )
    if supplied != SECRET:
        raise HTTPException(status_code=401, detail="Bad or missing token.")


def item_to_dict(row):
    item = dict(row)
    try:
        item["risk_flags"] = json.loads(item["risk_flags"] or "[]")
    except json.JSONDecodeError:
        item["risk_flags"] = []
    return item


def supersede_older_items(connection, serial, conversation_key, keep_id, detail):
    """Close undelivered items for a conversation that has moved on.

    Items already sending are left alone; the phone's own newest-message check
    stops them from going out into a conversation that changed.
    """
    conversation_key = (conversation_key or "").strip()

    if not conversation_key:
        return 0

    return connection.execute(
        "UPDATE review_items SET state = ?, updated_at = ?, detail = ? "
        "WHERE device_serial = ? AND conversation_key = ? AND id != ? "
        "AND state IN (?, ?, ?, ?)",
        (SUPERSEDED, now(), detail, serial, conversation_key, keep_id)
        + UNDELIVERED_STATES,
    ).rowcount


# --------------------------------------------------------------------------
# Ingest - called by draft_reply.py on every shard
# --------------------------------------------------------------------------
@app.post("/review")
def receive_review(payload: dict = Body(...)):
    require_secret((payload.get("secret") or "").strip())

    fingerprint = (payload.get("event_id") or "").strip()
    if not fingerprint:
        raise HTTPException(status_code=400, detail="event_id is required.")

    serial = (payload.get("device_serial") or "").strip()
    if not serial:
        raise HTTPException(status_code=400, detail="device_serial is required.")

    timestamp = now()
    with open_database() as connection:
        existing = connection.execute(
            "SELECT id, state FROM review_items WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

        if existing:
            # The same conversation was queued again (a retry, or a shard
            # resend). Keep the decision already made; never resurrect it.
            return {"ok": True, "id": existing["id"],
                    "state": existing["state"], "duplicate": True}

        cursor = connection.execute(
            """
            INSERT INTO review_items (
                fingerprint, created_at, updated_at, device_serial,
                device_name,
                conversation_key, conversation_label, listing_title,
                sender, latest_message, conversation, proposed_reply,
                approved_reply, confidence, reason, risk_flags,
                source_status, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                payload.get("received_at") or timestamp,
                timestamp,
                serial,
                (payload.get("device_name") or "").strip(),
                (payload.get("conversation_key") or "").strip() or None,
                payload.get("conversation_label") or "",
                payload.get("listing_title") or "",
                payload.get("sender") or "",
                payload.get("latest_message") or "",
                payload.get("conversation") or "",
                payload.get("proposed_reply") or "",
                float(payload.get("confidence") or 0.0),
                payload.get("reason") or "",
                json.dumps(payload.get("risk_flags") or []),
                payload.get("status") or "",
                PENDING,
            ),
        )
        item_id = cursor.lastrowid
        # A new item means the customer has spoken again, so any earlier
        # undelivered item for this chat answers a question that moved on.
        superseded = supersede_older_items(
            connection,
            serial,
            payload.get("conversation_key"),
            item_id,
            f"Superseded by newer item #{item_id}.",
        )
        connection.commit()
        return {
            "ok": True,
            "id": item_id,
            "state": PENDING,
            "superseded": superseded,
        }


@app.post("/history")
def record_auto_sent(
    payload: dict = Body(...),
    x_review_token: str = Header(default=""),
):
    """Record a reply the bot sent without review, for the history view."""
    require_secret(x_review_token.strip())

    fingerprint = (payload.get("event_id") or "").strip()
    serial = (payload.get("device_serial") or "").strip()
    reply = (payload.get("reply") or "").strip()

    if not fingerprint or not serial or not reply:
        raise HTTPException(
            status_code=400,
            detail="event_id, device_serial and reply are required.",
        )

    reason = (payload.get("reason") or "").strip()
    intent = (payload.get("intent") or "").strip()

    if intent:
        reason = f"{reason} (intent: {intent})" if reason else f"intent: {intent}"

    timestamp = now()

    with open_database() as connection:
        existing = connection.execute(
            "SELECT id, state FROM review_items WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

        if existing:
            return {"ok": True, "id": existing["id"],
                    "state": existing["state"], "duplicate": True}

        cursor = connection.execute(
            """
            INSERT INTO review_items (
                fingerprint, created_at, updated_at, device_serial,
                device_name,
                conversation_key, conversation_label, listing_title,
                sender, latest_message, conversation, proposed_reply,
                approved_reply, confidence, reason, risk_flags,
                source_status, state, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, '[]', ?, ?, ?)
            """,
            (
                fingerprint,
                payload.get("received_at") or timestamp,
                timestamp,
                serial,
                (payload.get("device_name") or "").strip(),
                (payload.get("conversation_key") or "").strip() or None,
                payload.get("conversation_label") or "",
                payload.get("listing_title") or "",
                payload.get("sender") or "",
                payload.get("latest_message") or "",
                payload.get("conversation") or "",
                reply,
                float(payload.get("confidence") or 0.0),
                reason,
                AUTO_SENT,
                AUTO_SENT,
                timestamp,
            ),
        )
        item_id = cursor.lastrowid
        # The bot has answered this conversation, so an older card for it no
        # longer needs a decision.
        superseded = supersede_older_items(
            connection,
            serial,
            payload.get("conversation_key"),
            item_id,
            f"Answered automatically by the bot (history item #{item_id}).",
        )
        connection.commit()

    return {"ok": True, "id": item_id, "state": AUTO_SENT,
            "superseded": superseded}


# --------------------------------------------------------------------------
# UI data + decisions
# --------------------------------------------------------------------------
@app.get("/api/items")
def list_items(
    x_review_token: str = Header(default=""),
    state: str = Query(default="open"),
):
    require_secret(x_review_token.strip())

    with open_database() as connection:
        sweep(connection)

        if state == "open":
            rows = connection.execute(
                "SELECT * FROM review_items WHERE state IN (?, ?, ?) "
                "ORDER BY id DESC LIMIT 200",
                OPEN_STATES,
            ).fetchall()
        elif state == "history":
            rows = connection.execute(
                "SELECT * FROM review_items WHERE state IN (?, ?) "
                "ORDER BY id DESC LIMIT 200",
                SENT_STATES,
            ).fetchall()
        elif state == "all":
            rows = connection.execute(
                "SELECT * FROM review_items ORDER BY id DESC LIMIT 200"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM review_items WHERE state = ? "
                "ORDER BY id DESC LIMIT 200",
                (state,),
            ).fetchall()

        counts = {
            row["state"]: row["n"]
            for row in connection.execute(
                "SELECT state, COUNT(*) n FROM review_items GROUP BY state"
            )
        }

    return {"items": [item_to_dict(row) for row in rows], "counts": counts}


@app.post("/api/items/{item_id}/approve")
def approve_item(
    item_id: int,
    payload: dict = Body(default={}),
    x_review_token: str = Header(default=""),
):
    require_secret(x_review_token.strip())

    with open_database() as connection:
        row = connection.execute(
            "SELECT * FROM review_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such item.")
        if row["state"] not in OPEN_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"Item is {row['state']}, not open for approval.",
            )

        reply = payload.get("reply")
        reply = (reply if isinstance(reply, str) else "").strip()
        reply = reply or (row["proposed_reply"] or "").strip()
        if not reply:
            raise HTTPException(status_code=400, detail="Reply is empty.")

        timestamp = now()
        connection.execute(
            "UPDATE review_items SET state = ?, approved_reply = ?, "
            "updated_at = ?, approved_at = ?, detail = NULL WHERE id = ?",
            (APPROVED, reply, timestamp, timestamp, item_id),
        )
        connection.commit()

    return {"ok": True, "id": item_id, "state": APPROVED, "reply": reply}


@app.post("/api/items/{item_id}/reject")
def reject_item(item_id: int, x_review_token: str = Header(default="")):
    require_secret(x_review_token.strip())

    # An approval can still be cancelled until delivery starts. Items that are
    # sending or sent are locked, so a reject can never race a send.
    rejectable = OPEN_STATES + (APPROVED,)

    with open_database() as connection:
        updated = connection.execute(
            "UPDATE review_items SET state = ?, updated_at = ? "
            "WHERE id = ? AND state IN (?, ?, ?, ?)",
            (REJECTED, now(), item_id) + rejectable,
        ).rowcount
        connection.commit()

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Item is already sending, sent, or rejected.",
        )
    return {"ok": True, "id": item_id, "state": REJECTED}


# --------------------------------------------------------------------------
# Delivery - called by the phone that owns each conversation
# --------------------------------------------------------------------------
@app.get("/approved")
def approved_for_serial(
    serial: str = Query(...), x_review_token: str = Header(default="")
):
    require_secret(x_review_token.strip())
    with open_database() as connection:
        sweep(connection)
        rows = connection.execute(
            "SELECT id, fingerprint, device_serial, device_name, "
            "conversation_key, "
            "conversation_label, listing_title, sender, latest_message, "
            "approved_reply, approved_at "
            "FROM review_items WHERE state = ? AND device_serial = ? "
            "ORDER BY id",
            (APPROVED, serial),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/items/{item_id}/claim")
def claim_item(item_id: int, x_review_token: str = Header(default="")):
    """Move one approval to sending. Only a single caller can ever win."""
    require_secret(x_review_token.strip())

    with open_database() as connection:
        sweep(connection)
        timestamp = now()
        claimed = connection.execute(
            "UPDATE review_items SET state = ?, claimed_at = ?, updated_at = ? "
            "WHERE id = ? AND state = ?",
            (SENDING, timestamp, timestamp, item_id, APPROVED),
        ).rowcount
        connection.commit()
        row = connection.execute(
            "SELECT state FROM review_items WHERE id = ?", (item_id,)
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="No such item.")
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail=f"Item is {row['state']}; only approved items can be claimed.",
        )
    return {"ok": True, "id": item_id, "state": SENDING}


@app.post("/api/items/{item_id}/delivered")
def mark_delivered(
    item_id: int,
    payload: dict = Body(...),
    x_review_token: str = Header(default=""),
):
    require_secret(x_review_token.strip())

    status = (payload.get("status") or "").strip()
    if status not in {SENT, STALE, FAILED}:
        raise HTTPException(status_code=400, detail=f"Bad status: {status!r}")

    detail = payload.get("detail")
    detail = detail.strip() if isinstance(detail, str) and detail.strip() else None

    with open_database() as connection:
        row = connection.execute(
            "SELECT state FROM review_items WHERE id = ?", (item_id,)
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="No such item.")
        if row["state"] != SENDING:
            raise HTTPException(
                status_code=409,
                detail=f"Item is {row['state']}, not sending.",
            )

        timestamp = now()
        assignments = ["state = ?", "detail = ?", "updated_at = ?"]
        values = [status, detail, timestamp]

        if status == SENT:
            assignments.append("delivered_at = ?")
            values.append(timestamp)

        if status == STALE:
            # Show the reviewer the conversation as it is now, so a
            # re-approval is checked against the customer's newest message
            # rather than bouncing against the old one forever.
            for field in ("conversation", "latest_message", "sender"):
                value = payload.get(field)

                if isinstance(value, str) and value.strip():
                    assignments.append(f"{field} = ?")
                    values.append(value.strip())

        values.append(item_id)
        connection.execute(
            f"UPDATE review_items SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        connection.commit()

    return {"ok": True, "id": item_id, "state": status}


@app.get("/healthz")
def healthz():
    with open_database() as connection:
        total = connection.execute(
            "SELECT COUNT(*) n FROM review_items"
        ).fetchone()["n"]
    return JSONResponse({"ok": True, "items": total, "secret_set": bool(SECRET)})


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(PAGE)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reply approvals</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --ink:#14161a; --muted:#5c6470;
    --line:#e3e6ea; --accent:#1f6feb; --ok:#137333; --warn:#8a5a00;
    --bad:#b3261e; --chip:#eef1f5;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#111316; --card:#1a1d21; --ink:#e8eaed; --muted:#9aa3af;
      --line:#2a2f36; --accent:#5b9cff; --ok:#5bd08a; --warn:#e0b050;
      --bad:#ff6b60; --chip:#23272d;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  header{position:sticky;top:0;z-index:5;background:var(--bg);
    border-bottom:1px solid var(--line);padding:12px 16px;
    display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  h1{font-size:17px;margin:0;font-weight:650}
  .counts{color:var(--muted);font-size:13px;margin-left:auto}
  main{padding:12px;max-width:820px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);
    border-radius:12px;padding:14px;margin-bottom:12px}
  .row{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
  .label{font-weight:650}
  .meta{color:var(--muted);font-size:13px}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
  .chip{background:var(--chip);border-radius:999px;padding:2px 10px;
    font-size:12px;color:var(--muted)}
  .chip.warn{color:var(--warn)} .chip.bad{color:var(--bad)}
  .chip.ok{color:var(--ok)}
  .conv{white-space:pre-wrap;background:var(--chip);border-radius:8px;
    padding:10px;font-size:13px;max-height:210px;overflow:auto;margin:8px 0}
  textarea{width:100%;min-height:88px;padding:10px;border-radius:8px;
    border:1px solid var(--line);background:var(--bg);color:var(--ink);
    font:inherit;resize:vertical}
  .actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  button{font:inherit;border:0;border-radius:8px;padding:10px 16px;
    cursor:pointer;min-height:42px}
  .approve{background:var(--accent);color:#fff;font-weight:600}
  .reject{background:transparent;color:var(--bad);
    border:1px solid var(--line)}
  .empty{text-align:center;color:var(--muted);padding:48px 16px}
  .err{background:var(--bad);color:#fff;padding:10px 16px}
  .state{font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .state.stale{color:var(--warn)} .state.failed{color:var(--bad)}
  select{font:inherit;background:var(--card);color:var(--ink);
    border:1px solid var(--line);border-radius:8px;padding:8px}
</style>
</head>
<body>
<header>
  <h1>Reply approvals</h1>
  <select id="filter">
    <option value="open">Needs attention</option>
    <option value="history">History: all sent</option>
    <option value="auto_sent">Sent by AI (no review)</option>
    <option value="sent">Sent after your approval</option>
    <option value="approved">Approved, not sent yet</option>
    <option value="rejected">Rejected</option>
    <option value="superseded">Superseded</option>
    <option value="all">All</option>
  </select>
  <span class="counts" id="counts"></span>
</header>
<div id="err" class="err" hidden></div>
<main id="list"><div class="empty">Loading…</div></main>
<script>
const qs = new URLSearchParams(location.search);
if (qs.get("token")) {
  try { localStorage.setItem("reviewToken", qs.get("token")); } catch (e) {}
  history.replaceState({}, "", location.pathname);
}
let token = "";
try { token = localStorage.getItem("reviewToken") || ""; } catch (e) {}

const listEl = document.getElementById("list");
const errEl = document.getElementById("err");
const countsEl = document.getElementById("counts");
const filterEl = document.getElementById("filter");

function showError(message) {
  errEl.textContent = message;
  errEl.hidden = !message;
}

async function api(path, options) {
  const opts = options || {};
  opts.headers = Object.assign({"X-Review-Token": token,
    "Content-Type": "application/json"}, opts.headers || {});
  const response = await fetch(path, opts);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || (response.status + " " + response.statusText));
  }
  return response.json();
}

function escapeHtml(value) {
  return (value || "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// Text you have typed but not yet approved. It survives the automatic
// refresh, switching filters, and reloading the page.
let drafts = {};
try { drafts = JSON.parse(localStorage.getItem("reviewDrafts") || "{}") || {}; } catch (e) {}
function saveDrafts() {
  try { localStorage.setItem("reviewDrafts", JSON.stringify(drafts)); } catch (e) {}
}

try {
  const savedFilter = localStorage.getItem("reviewFilter");
  if (savedFilter && [...filterEl.options].some(o => o.value === savedFilter)) {
    filterEl.value = savedFilter;
  }
} catch (e) {}

const STATE_LABELS = {auto_sent: "sent by AI", sent: "sent", approved: "approved"};

function card(item) {
  const flags = (item.risk_flags || []).map(f =>
    `<span class="chip ${/failure|unsupported|not_verified|unreadable/.test(f) ? "bad" : "warn"}">${escapeHtml(f)}</span>`
  ).join("");
  const open = ["pending","stale","failed"].includes(item.state);
  const isHistory = ["sent","auto_sent"].includes(item.state);
  const confidence = (item.confidence == null || item.state === "sent") ? "" :
    `<span class="chip">confidence ${Number(item.confidence).toFixed(2)}</span>`;
  const who = item.state === "auto_sent"
    ? `<span class="chip ok">AI replied, no review needed</span>`
    : item.state === "sent" ? `<span class="chip ok">Approved by you</span>` : "";
  const when = isHistory && item.delivered_at ? item.delivered_at : (item.created_at || "");
  const sentText = item.approved_reply || item.proposed_reply;
  const hasDraft = Object.prototype.hasOwnProperty.call(drafts, String(item.id));
  const replyText = hasDraft ? drafts[item.id] : sentText;
  return `
  <div class="card" data-id="${item.id}">
    <div class="row">
      <span class="label">${escapeHtml(item.conversation_label || item.sender || "(no label)")}</span>
      <span class="state ${item.state}">${escapeHtml(STATE_LABELS[item.state] || item.state)}</span>
      <span class="meta" style="margin-left:auto" title="${escapeHtml(item.device_serial)}">${escapeHtml(item.device_name || item.device_serial)} · ${escapeHtml(when)}</span>
    </div>
    <div class="chips">${who}${confidence}${flags}</div>
    <div class="meta">Conversation:</div>
    <div class="conv">${escapeHtml(item.conversation || item.latest_message)}</div>
    ${item.reason ? `<div class="meta">Why: ${escapeHtml(item.reason)}</div>` : ""}
    ${item.detail && !isHistory ? `<div class="meta" style="color:var(--bad)">${escapeHtml(item.detail)}</div>` : ""}
    ${open ? `
      <textarea class="reply">${escapeHtml(replyText)}</textarea>
      ${hasDraft ? `<div class="meta">Your edit is saved here until you approve or reject.</div>` : ""}
      <div class="actions">
        <button class="approve">Approve &amp; queue</button>
        <button class="reject">Reject</button>
      </div>` : `
      <div class="meta">${isHistory ? "Reply sent:" : "Reply:"}</div>
      <div class="conv">${escapeHtml(sentText)}</div>
      ${item.state === "approved" ? `
      <div class="actions">
        <button class="reject">Cancel approval</button>
      </div>` : ""}`}
  </div>`;
}

function isEditing() {
  const active = document.activeElement;
  return !!(active && active.matches && active.matches("textarea.reply"));
}

async function load(automatic) {
  if (!token) {
    listEl.innerHTML = `<div class="empty">Open this page with
      <code>?token=YOUR_REVIEW_SERVICE_SECRET</code> once.</div>`;
    return;
  }
  // Never rebuild the list under someone who is typing.
  if (automatic && isEditing()) return;
  try {
    const data = await api("/api/items?state=" + encodeURIComponent(filterEl.value));
    if (automatic && isEditing()) return;
    showError("");
    countsEl.textContent = Object.entries(data.counts || {})
      .map(([k, v]) => `${STATE_LABELS[k] || k}: ${v}`).join("   ");
    if (filterEl.value === "open") {
      // Forget drafts for cards that no longer need a decision.
      const openIds = new Set(data.items.map(i => String(i.id)));
      for (const id of Object.keys(drafts)) {
        if (!openIds.has(id)) delete drafts[id];
      }
      saveDrafts();
    }
    listEl.innerHTML = data.items.length
      ? data.items.map(card).join("")
      : `<div class="empty">Nothing here.</div>`;
  } catch (error) {
    showError(error.message);
  }
}

listEl.addEventListener("input", (event) => {
  const box = event.target.closest("textarea.reply");
  if (!box) return;
  drafts[box.closest(".card").dataset.id] = box.value;
  saveDrafts();
});

listEl.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  const cardEl = button.closest(".card");
  const id = cardEl.dataset.id;
  button.disabled = true;
  try {
    if (button.classList.contains("approve")) {
      const reply = cardEl.querySelector(".reply").value;
      await api(`/api/items/${id}/approve`, {
        method: "POST", body: JSON.stringify({reply})});
    } else {
      await api(`/api/items/${id}/reject`, {method: "POST"});
    }
    delete drafts[id];
    saveDrafts();
    await load(false);
  } catch (error) {
    showError(error.message);
    button.disabled = false;
  }
});

filterEl.addEventListener("change", () => {
  try { localStorage.setItem("reviewFilter", filterEl.value); } catch (e) {}
  load(false);
});
load(false);
setInterval(() => load(true), 20000);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Reply approval service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--log-file",
        help=(
            "Append output here. Needed when started by Windows at logon, "
            "which gives the app no console to print to."
        ),
    )
    arguments = parser.parse_args()

    if arguments.log_file:
        handle = open(arguments.log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
        print(f"\n=== started {now()} on port {arguments.port} ===")

    if not SECRET:
        raise SystemExit(
            "REVIEW_SERVICE_SECRET is missing from .env. Set it to a long "
            "random string, and use the same value on every shard."
        )

    import uvicorn

    print(f"Review database: {DATABASE_PATH}")
    print(f"Open: http://{arguments.host}:{arguments.port}/?token=<secret>")
    uvicorn.run(app, host=arguments.host, port=arguments.port)


if __name__ == "__main__":
    main()
