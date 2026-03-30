"""Poll for new emails using Gmail API (not IMAP).

Uses Gmail REST API to fetch only recent unread emails.
No dependency on Frappe's Email Account IMAP pull.
Runs via scheduler "all" event.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

import frappe

logger = logging.getLogger(__name__)

_LAST_POLL_KEY = "sena_gmail:last_poll_time"
_LAST_HISTORY_KEY = "sena_gmail:last_history_id"
_POLL_INTERVAL = 15  # seconds between polls
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


def poll_new_emails() -> None:
    """Poll Gmail API for new unread emails and route to agents."""
    cache = frappe.cache()

    # Rate limit
    last_poll = cache.get(_LAST_POLL_KEY)
    if isinstance(last_poll, bytes):
        last_poll = float(last_poll.decode())
    else:
        last_poll = 0

    now = time.time()
    if now - last_poll < _POLL_INTERVAL:
        return

    cache.set(_LAST_POLL_KEY, str(now))

    # Get Gmail OAuth token
    token = _get_gmail_token()
    if not token:
        return

    email_id = _get_gmail_email()
    if not email_id:
        return

    # Fetch recent unread messages from Gmail API
    try:
        messages = _fetch_unread_messages(token)
    except Exception:
        logger.exception("Gmail poll: failed to fetch messages")
        return

    if not messages:
        return

    from sena_gmail.api.gmail import _instance_id, _get_or_create_instance

    # Check which messages we've already processed
    processed_key = "sena_gmail:processed_ids"
    processed_raw = cache.get(processed_key)
    if isinstance(processed_raw, bytes):
        processed_ids = set(json.loads(processed_raw.decode()))
    else:
        processed_ids = set()

    new_count = 0
    for msg_meta in messages:
        msg_id = msg_meta.get("id")
        if msg_id in processed_ids:
            continue

        try:
            msg_detail = _fetch_message_detail(token, msg_id)
            if not msg_detail:
                continue

            sender = msg_detail.get("from", "")
            subject = msg_detail.get("subject", "")
            body = msg_detail.get("body", "")
            sender_name = msg_detail.get("from_name", sender)

            # Skip emails from ourselves
            if sender.lower() == email_id.lower():
                processed_ids.add(msg_id)
                continue

            # Create Runtime Instance and message
            from sena_gmail.api.gmail import process_inbound_email_direct
            process_inbound_email_direct(sender, sender_name, subject, body)
            processed_ids.add(msg_id)
            new_count += 1

        except Exception:
            logger.exception("Gmail poll: failed to process message %s", msg_id)
            processed_ids.add(msg_id)  # Don't retry failed messages

    # Save processed IDs (keep last 200 to prevent unbounded growth)
    trimmed = list(processed_ids)[-200:]
    cache.set(processed_key, json.dumps(trimmed))

    if new_count:
        logger.info("Gmail poll: processed %s new emails", new_count)


def _get_gmail_token() -> str:
    """Get OAuth access token from Connected App."""
    try:
        connected_app_name = ""
        creds = frappe.get_all(
            "User Tool Credential",
            filters={"composite": "gmail", "enabled": 1},
            fields=["config_json"],
            limit=1,
        )
        if creds:
            config = json.loads(creds[0].config_json or "{}")
            connected_app_name = config.get("connected_app", "")

        if not connected_app_name:
            # Find any Google connected app
            apps = frappe.get_all("Connected App", filters={"provider_name": "Google"}, fields=["name"], limit=1)
            if not apps:
                return ""
            connected_app_name = apps[0].name

        connected_app = frappe.get_doc("Connected App", connected_app_name)
        user = config.get("email_id") or frappe.session.user

        token_cache = connected_app.get_active_token(user)
        if token_cache and token_cache.get_password("access_token"):
            return token_cache.get_password("access_token")
    except Exception:
        logger.debug("Gmail poll: could not get token")
    return ""


def _get_gmail_email() -> str:
    """Get the connected Gmail email address."""
    creds = frappe.get_all(
        "User Tool Credential",
        filters={"composite": "gmail", "enabled": 1},
        fields=["config_json"],
        limit=1,
    )
    if creds:
        config = json.loads(creds[0].config_json or "{}")
        return config.get("email_id", "")
    return ""


def _fetch_unread_messages(token: str) -> list:
    """Fetch recent unread messages from Gmail API (last 1 hour only)."""
    import urllib.parse
    # newer_than:1h limits to emails received in the last hour
    q = urllib.parse.quote("is:unread in:inbox newer_than:1h")
    url = f"{_GMAIL_API}/users/me/messages?q={q}&maxResults=10"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return data.get("messages", [])


def _fetch_message_detail(token: str, msg_id: str) -> dict | None:
    """Fetch full message details from Gmail API."""
    url = f"{_GMAIL_API}/users/me/messages/{msg_id}?format=full"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}
    sender_raw = headers.get("from", "")

    # Parse "Name <email>" format
    import re
    match = re.match(r'^(.+?)\s*<(.+?)>$', sender_raw)
    if match:
        from_name = match.group(1).strip().strip('"')
        from_email = match.group(2).strip()
    else:
        from_name = sender_raw
        from_email = sender_raw

    # Get body text
    body = _extract_body(data.get("payload", {}))

    return {
        "from": from_email,
        "from_name": from_name,
        "subject": headers.get("subject", ""),
        "body": body,
    }


def _extract_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    import base64

    # Check for plain text part
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    # Check parts
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        # Nested multipart
        if part.get("parts"):
            result = _extract_body(part)
            if result:
                return result

    # Fallback: snippet
    return ""
