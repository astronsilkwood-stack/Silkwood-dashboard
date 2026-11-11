"""Owner help chat. The portal stores messages and relays them. It does not call a model."""
import json
import os
import urllib.request

import portal

MAX_MESSAGE = 2000


def _clean_page(value):
    page = str(value or "")[:200]
    if not page.startswith("/portal"):
        page = "/portal"
    return page.split("?", 1)[0]


def _clean_context(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in list(value.items())[:20]:
        if not isinstance(key, str) or not isinstance(item, str):
            continue
        out[key[:40]] = item[:120]
    return out


def list_messages(c):
    found = []
    for row in portal.rows(c.execute("SELECT * FROM portal_chat ORDER BY id")):
        context = {}
        try:
            context = json.loads(row["context"] or "{}")
        except json.JSONDecodeError:
            context = {}
        found.append({
            "id": row["id"],
            "message": row["message"],
            "page": row["page"] or "",
            "context": context if isinstance(context, dict) else {},
            "reply": row["reply"],
            "createdAt": row["created_at"],
            "repliedAt": row["replied_at"],
        })
    return found


def deliver(payload):
    """POST the message to the owner's assistant. Returns offline, sent, or saved.

    The webhook key is never logged.
    """
    url = os.environ.get("PORTAL_CHAT_WEBHOOK_URL", "").strip()
    if not url:
        return "offline"
    key = os.environ.get("PORTAL_CHAT_WEBHOOK_KEY", "").strip()
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "User-Agent": "silkwood-portal-chat/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            response.read(2048)
        return "sent"
    except Exception:
        portal.log("chat webhook failed")
        return "saved"


def add_message(c, user, message, page, context):
    text = (message or "").strip()
    if not text or len(text) > MAX_MESSAGE:
        raise ValueError("Enter a message of 2000 characters or less.")
    portal.reject_sensitive(text, "chat")
    ctx = _clean_context(context)
    for key, value in portal.chat_figures(c).items():
        ctx[key] = str(value)[:120]
    portal.reject_sensitive(ctx, "chat")
    created = portal._srv().now().isoformat()
    row = portal.one(c.execute(
        """INSERT INTO portal_chat(message,page,context,reply,created_at,replied_at,created_by)
           VALUES(?,?,?,NULL,?,NULL,?) RETURNING id""",
        (text, _clean_page(page), json.dumps(ctx, ensure_ascii=False), created, user["username"]),
    ))
    payload = {"id": row["id"], "message": text, "page": _clean_page(page), "context": ctx, "createdAt": created}
    status = deliver(payload)
    return row["id"], status


def set_reply(c, message_id, reply):
    text = (reply or "").strip()
    if not text or len(text) > 8000:
        raise ValueError("Reply is required and must be 8000 characters or less.")
    portal.reject_sensitive(text, "chat")
    row = portal.one(c.execute("SELECT id FROM portal_chat WHERE id=?", (message_id,)))
    if not row:
        raise LookupError("That message was not found.")
    c.execute(
        "UPDATE portal_chat SET reply=?, replied_at=? WHERE id=?",
        (text, portal.stamp(), message_id),
    )


def clear_messages(c):
    c.execute("DELETE FROM portal_chat")
