"""Gmail tools for Sena Agents.

Provides gmail_send, gmail_reply, and gmail_search tools.
Uses Frappe's Email Account + SMTP for sending.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import frappe

logger = logging.getLogger(__name__)


# ── Credential helpers ────────────────────────────────────────────────────────

def _resolve_current_user() -> str:
    user = getattr(frappe.flags, "current_spawned_by_user", None)
    if user:
        return user
    instance_id = getattr(frappe.flags, "current_instance_id", None)
    if instance_id:
        spawned_by = frappe.db.get_value("Runtime Instance", instance_id, "spawned_by_user")
        if spawned_by:
            return spawned_by
    return frappe.session.user


def _get_sender_email() -> str:
    """Get the outgoing email address from the user's Gmail Email Account."""
    user = _resolve_current_user()

    # Try user's own OAuth account first
    accounts = frappe.get_all(
        "Email Account",
        filters={"connected_user": user, "service": "GMail", "enable_outgoing": 1},
        fields=["email_id"],
        limit=1,
    )
    if accounts:
        return accounts[0].email_id

    # Fallback to any Gmail outgoing account
    accounts = frappe.get_all(
        "Email Account",
        filters={"service": "GMail", "enable_outgoing": 1},
        fields=["email_id"],
        limit=1,
    )
    if accounts:
        return accounts[0].email_id

    # Last resort: any outgoing account
    accounts = frappe.get_all(
        "Email Account",
        filters={"enable_outgoing": 1},
        fields=["email_id"],
        limit=1,
    )
    return accounts[0].email_id if accounts else ""


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _handle_gmail_send(to: str, subject: str, body: str, cc: str = "", bcc: str = "", **_: Any) -> dict[str, Any]:
    """Send a new email via Gmail."""
    sender = _get_sender_email()
    if not sender:
        return {"status": "error", "error": "No outgoing email account configured."}

    try:
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        cc_list = [r.strip() for r in cc.split(",") if r.strip()] if cc else []
        bcc_list = [r.strip() for r in bcc.split(",") if r.strip()] if bcc else []

        frappe.sendmail(
            recipients=recipients,
            sender=sender,
            subject=subject,
            message=body,
            cc=cc_list,
            bcc=bcc_list,
            now=True,
        )

        logger.info("gmail_send: sent to=%s subject=%s", to, subject[:50])
        return {"status": "ok", "to": to, "subject": subject, "sender": sender}
    except Exception as exc:
        logger.error("gmail_send failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def _handle_gmail_reply(to: str, subject: str, body: str, cc: str = "", **_: Any) -> dict[str, Any]:
    """Reply to an email thread."""
    sender = _get_sender_email()
    if not sender:
        return {"status": "error", "error": "No outgoing email account configured."}

    # Ensure subject has Re: prefix
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    try:
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        cc_list = [r.strip() for r in cc.split(",") if r.strip()] if cc else []

        frappe.sendmail(
            recipients=recipients,
            sender=sender,
            subject=subject,
            message=body,
            cc=cc_list,
            now=True,
        )

        logger.info("gmail_reply: sent to=%s subject=%s", to, subject[:50])
        return {"status": "ok", "to": to, "subject": subject, "sender": sender}
    except Exception as exc:
        logger.error("gmail_reply failed: %s", exc)
        return {"status": "error", "error": str(exc)}


def _handle_gmail_search(query: str, limit: int = 10, **_: Any) -> dict[str, Any]:
    """Search emails in the inbox."""
    try:
        limit = min(int(limit), 50)

        # Search Communications (Frappe's email storage)
        filters = {
            "communication_medium": "Email",
            "communication_type": "Communication",
        }

        # Parse simple query — search in subject and sender
        or_filters = [
            {"subject": ["like", f"%{query}%"]},
            {"sender": ["like", f"%{query}%"]},
            {"content": ["like", f"%{query}%"]},
        ]

        results = frappe.get_all(
            "Communication",
            filters=filters,
            or_filters=or_filters,
            fields=["name", "subject", "sender", "sender_full_name", "creation",
                     "sent_or_received", "seen", "has_attachment"],
            order_by="creation desc",
            limit=limit,
        )

        emails = []
        for r in results:
            emails.append({
                "id": r.name,
                "subject": r.subject or "(no subject)",
                "from": r.sender_full_name or r.sender,
                "from_email": r.sender,
                "date": str(r.creation),
                "direction": r.sent_or_received,
                "read": bool(r.seen),
                "has_attachment": bool(r.has_attachment),
            })

        return {"status": "ok", "query": query, "count": len(emails), "emails": emails}
    except Exception as exc:
        logger.error("gmail_search failed: %s", exc)
        return {"status": "error", "error": str(exc)}


# ── Tool registration ─────────────────────────────────────────────────────────

def _register() -> None:
    try:
        from sena_agents_backend.sena_agents_backend.tools.registry import register_tool
    except ImportError:
        logger.warning("gmail_tools: sena_agents_backend not available — skipping")
        return

    register_tool(
        name="gmail_send",
        description="Send a new email via Gmail.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email (comma-separated for multiple)."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Email body (plain text or HTML)."},
                "cc": {"type": "string", "description": "CC recipients (comma-separated). Optional."},
                "bcc": {"type": "string", "description": "BCC recipients (comma-separated). Optional."},
            },
            "required": ["to", "subject", "body"],
        },
        handler=_handle_gmail_send,
        category="Integration",
        tags="gmail, email, send",
        is_system=False,
    )

    register_tool(
        name="gmail_reply",
        description="Reply to an email thread via Gmail.",
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email to reply to."},
                "subject": {"type": "string", "description": "Email subject (Re: prefix auto-added)."},
                "body": {"type": "string", "description": "Reply body text."},
                "cc": {"type": "string", "description": "CC recipients (comma-separated). Optional."},
            },
            "required": ["to", "subject", "body"],
        },
        handler=_handle_gmail_reply,
        category="Integration",
        tags="gmail, email, reply",
        is_system=False,
    )

    register_tool(
        name="gmail_search",
        description="Search emails in the inbox by keyword (searches subject, sender, and body).",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (keyword to find in subject/sender/body)."},
                "limit": {"type": "integer", "description": "Max results to return (default 10, max 50)."},
            },
            "required": ["query"],
        },
        handler=_handle_gmail_search,
        category="Integration",
        tags="gmail, email, search",
        is_system=False,
    )

    logger.info("gmail_tools: registered gmail_send, gmail_reply, gmail_search")


_register()
