"""Gmail webhook/polling receiver for Sena Agents.

Unlike WhatsApp/Telegram which use webhooks, Gmail uses polling (IMAP) via
Frappe's built-in Email Account system. New emails appear as Communication
docs. This module routes them to agents.

Inbound email flow:
  1. Frappe scheduler pulls emails via IMAP (Email Account with enable_incoming=1)
  2. New Communication docs are created automatically by Frappe
  3. Our poll service detects new Communications and routes to agents
  4. Agent processes and replies via gmail_reply tool

Operator direct send:
  send_direct_message() — send from channels-ui without agent
"""
from __future__ import annotations

import json
import logging
from typing import Any

import frappe

from sena_gmail.services.gmail_routing_service import resolve_gmail_agent

logger = logging.getLogger(__name__)

# Redis key prefixes
_PENDING_KEY = "em_pending:{email_key}"
_LOCK_KEY = "em_lock:{email_key}"
_INJECT_KEY = "em_inject:{instance_id}"
_LOCK_TTL = 600
_INJECT_TTL = 3600


def _site_key(key: str) -> str:
    try:
        from sena_agents_backend.sena_agents_backend.utils.redis_keys import site_key
        return site_key(key)
    except ImportError:
        return f"{frappe.local.site}:{key}"


def _instance_id(sender_email: str) -> str:
    """Create instance ID from sender email. Sanitize for use as doc name."""
    safe = sender_email.lower().strip().replace("@", "_at_").replace(".", "_")
    return f"std:em:{safe}"


# ── Called by poll service when new email arrives ────────────────────────────

def process_inbound_email(communication_name: str) -> None:
    """Process an inbound email Communication and route to an agent."""
    comm = frappe.get_doc("Communication", communication_name)

    sender = comm.sender or ""
    subject = comm.subject or ""
    text = comm.text_content or comm.content or ""
    # Truncate very long emails for the agent
    if len(text) > 3000:
        text = text[:3000] + "\n\n[... email truncated ...]"

    sender_name = comm.sender_full_name or sender

    logger.info("Email inbound: from=%s subject=%s", sender, subject[:50])

    # Route to agent
    target_agent = resolve_gmail_agent(sender, subject, text)

    if not target_agent:
        _store_unrouted(sender, subject, text, sender_name, communication_name)
        frappe.publish_realtime("em_new_message", {"sender": sender, "routed": False})
        return

    # Build message envelope
    envelope = json.dumps({
        "text": f"Subject: {subject}\n\n{text}",
        "sender": sender,
        "from_name": sender_name,
        "subject": subject,
        "communication_name": communication_name,
    })

    email_key = sender.lower().strip()
    cache = frappe.cache()
    lock_key = _site_key(_LOCK_KEY.format(email_key=email_key))
    pending_key = _site_key(_PENDING_KEY.format(email_key=email_key))
    instance_id = _instance_id(sender)
    inject_key = _site_key(_INJECT_KEY.format(instance_id=instance_id))

    acquired = cache.set(lock_key, "1", nx=True, ex=_LOCK_TTL)

    if not acquired:
        cache.rpush(inject_key, envelope)
        cache.expire(inject_key, _INJECT_TTL)
        return

    cache.rpush(pending_key, envelope)
    cache.expire(pending_key, 3600)

    frappe.enqueue(
        "sena_gmail.api.gmail._run_email_wakeup",
        agent_name=target_agent,
        email_key=email_key,
        sender=sender,
        queue="long",
        enqueue_after_commit=False,
    )

    frappe.publish_realtime("em_new_message", {"sender": sender, "agent": target_agent})
    logger.info("Email inbound: enqueued wakeup for sender=%s → agent=%s", sender, target_agent)


# ── Operator direct send ─────────────────────────────────────────────────────

@frappe.whitelist()
def send_direct_message(instance_id: str, message: str, subject: str = "") -> dict[str, Any]:
    """Send an email directly from the operator (no agent)."""
    if not instance_id.startswith("std:em:"):
        frappe.throw("Not an Email instance", frappe.ValidationError)

    # Reverse the instance_id to get the email
    email_part = instance_id[len("std:em:"):]
    recipient = email_part.replace("_at_", "@").replace("_", ".")

    # Find user's Email Account
    user = frappe.session.user
    accounts = frappe.get_all(
        "Email Account",
        filters={"connected_user": user, "enable_outgoing": 1},
        fields=["name", "email_id"],
        limit=1,
    )
    if not accounts:
        # Fallback to any outgoing email account
        accounts = frappe.get_all(
            "Email Account",
            filters={"enable_outgoing": 1},
            fields=["name", "email_id"],
            limit=1,
        )
    if not accounts:
        frappe.throw("No outgoing email account configured.", frappe.ValidationError)

    sender = accounts[0].email_id

    # Send via Frappe's email system
    frappe.sendmail(
        recipients=[recipient],
        sender=sender,
        subject=subject or "Re: Conversation",
        message=message,
        now=True,
    )

    # Persist as operator message
    from sena_agents_backend.sena_agents_backend.core.memory import MemoryStore
    agent_name = frappe.db.get_value("Runtime Instance", instance_id, "agent_name") or ""
    memory = MemoryStore(agent_name, instance_id)
    memory.append_message({
        "role": "assistant",
        "content": f"Subject: {subject}\n\n{message}" if subject else message,
        "sender_name": "Operator",
    })
    frappe.db.commit()

    return {"status": "ok", "recipient": recipient}


# ── Background wakeup job ─────────────────────────────────────────────────────

def _run_email_wakeup(agent_name: str, email_key: str, sender: str) -> None:
    """Drain pending emails and run the agent."""
    from sena_agents_backend.sena_agents_backend.services.chat_service import ChatService

    cache = frappe.cache()
    lock_key = _site_key(_LOCK_KEY.format(email_key=email_key))
    pending_key = _site_key(_PENDING_KEY.format(email_key=email_key))
    instance_id = _instance_id(sender)
    inject_key = _site_key(_INJECT_KEY.format(instance_id=instance_id))

    _get_or_create_instance(agent_name, sender)

    try:
        frappe.db.set_value("Runtime Instance", instance_id, "status", "running")
        frappe.db.commit()

        messages = _drain(cache, pending_key)
        if messages:
            _run_agent(ChatService, agent_name, instance_id, sender, messages, pass_num=0)

        leftover = _drain(cache, inject_key)
        if leftover:
            _run_agent(ChatService, agent_name, instance_id, sender, leftover, pass_num=1)

    finally:
        try:
            current_status = frappe.db.get_value("Runtime Instance", instance_id, "status")
            if current_status != "waiting_for_owner":
                frappe.db.set_value("Runtime Instance", instance_id, "status", "active")
            frappe.db.commit()
        except Exception:
            pass
        cache.delete(lock_key)


def _run_agent(ChatService, agent_name: str, instance_id: str, sender: str,
               messages: list[dict], pass_num: int = 0) -> None:
    last = messages[-1]
    from_name = last.get("from_name") or sender
    subject = last.get("subject") or ""

    frappe.flags.channel_sender_name = from_name

    combined_text = (
        messages[0]["text"] if len(messages) == 1
        else "\n---\n".join(f"[Email {i + 1}]\n{m['text']}" for i, m in enumerate(messages))
    )

    reply_instruction = (
        "IMPORTANT: Plain text responses are NOT delivered. "
        "You MUST use the gmail_reply tool to reply to this email.\n"
        f'Call: gmail_reply(to="{sender}", subject="Re: {subject}", body="your reply")'
    )

    escalation_instruction = (
        "\nIf you are unsure how to answer or the question requires the "
        "owner's personal input (scheduling, pricing, preferences, etc.), "
        "call ask_owner(question=\"...\") instead of guessing. After calling "
        "ask_owner, do NOT send any message to the user — just stop."
    )

    full_message = (
        f"{combined_text}\n\n"
        f"---\n"
        f"[Email from {from_name} <{sender}>] {reply_instruction}"
        f"{escalation_instruction}"
    )

    try:
        result = ChatService.send_message(
            agent_name=agent_name,
            message=full_message,
            instance_id=instance_id,
        )
        logger.info("Email agent done: agent=%s sender=%s pass=%s turns=%s",
                     agent_name, sender, pass_num, result.get("turns_used"))
    except Exception:
        logger.exception("Email agent failed: agent=%s sender=%s pass=%s", agent_name, sender, pass_num)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _drain(cache, key: str) -> list[dict]:
    real_key = cache.make_key(key)
    pipe = cache.pipeline()
    pipe.lrange(real_key, 0, -1)
    pipe.delete(real_key)
    raw_list, _ = pipe.execute()
    result = []
    for item in (raw_list or []):
        try:
            raw = item.decode() if isinstance(item, bytes) else item
            result.append(json.loads(raw))
        except Exception:
            pass
    return result


def _get_or_create_instance(agent_name: str, sender: str) -> str:
    instance_id = _instance_id(sender)
    if not frappe.db.exists("Runtime Instance", instance_id):
        frappe.get_doc({
            "doctype": "Runtime Instance",
            "name": instance_id,
            "instance_id": instance_id,
            "agent_name": agent_name,
            "title": f"Email: {sender}",
            "status": "active",
        }).insert(ignore_permissions=True)
        frappe.db.commit()
    return instance_id


def _store_unrouted(sender: str, subject: str, text: str, from_name: str, comm_name: str) -> None:
    from sena_agents_backend.sena_agents_backend.core.memory import MemoryStore

    instance_id = _instance_id(sender)
    _get_or_create_instance("", sender)
    memory = MemoryStore("", instance_id)
    memory.append_message({
        "role": "user",
        "content": f"Subject: {subject}\n\n{text}",
        "sender_name": from_name or sender,
    })
    frappe.db.commit()


def process_inbound_email_direct(sender: str, sender_name: str, subject: str, body: str) -> None:
    """Process an inbound email directly (from Gmail API poll, not Communication doc)."""
    if not sender:
        return

    # Truncate long emails
    if len(body) > 3000:
        body = body[:3000] + "\n\n[... email truncated ...]"

    text = f"Subject: {subject}\n\n{body}" if subject else body
    logger.info("Email inbound (API): from=%s subject=%s", sender, subject[:50])

    target_agent = resolve_gmail_agent(sender, subject, body)

    if not target_agent:
        _store_unrouted(sender, subject, body, sender_name)
        frappe.publish_realtime("em_new_message", {"sender": sender, "routed": False})
        return

    envelope = json.dumps({
        "text": text,
        "sender": sender,
        "from_name": sender_name,
        "subject": subject,
    })

    email_key = sender.lower().strip()
    cache = frappe.cache()
    lock_key = _site_key(_LOCK_KEY.format(email_key=email_key))
    pending_key = _site_key(_PENDING_KEY.format(email_key=email_key))
    instance_id = _instance_id(sender)
    inject_key = _site_key(_INJECT_KEY.format(instance_id=instance_id))

    acquired = cache.set(lock_key, "1", nx=True, ex=_LOCK_TTL)

    if not acquired:
        cache.rpush(inject_key, envelope)
        cache.expire(inject_key, _INJECT_TTL)
        return

    cache.rpush(pending_key, envelope)
    cache.expire(pending_key, 3600)

    frappe.enqueue(
        "sena_gmail.api.gmail._run_email_wakeup",
        agent_name=target_agent,
        email_key=email_key,
        sender=sender,
        queue="long",
        enqueue_after_commit=False,
    )

    frappe.publish_realtime("em_new_message", {"sender": sender, "agent": target_agent})
    logger.info("Email inbound: enqueued wakeup for sender=%s → agent=%s", sender, target_agent)
