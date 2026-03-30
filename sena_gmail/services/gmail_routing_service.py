"""Route incoming emails to the correct agent.

Same pattern as WhatsApp/Telegram routing — checks Runtime External Trigger
child table with channel="Email".
"""
from __future__ import annotations

import logging

import frappe

logger = logging.getLogger(__name__)

_SAFE_BUILTINS = {"len": len, "str": str, "int": int, "bool": bool}


def resolve_gmail_agent(
    sender: str,
    subject: str,
    message_text: str = "",
) -> str | None:
    """Return the agent name that should handle this inbound email.

    Evaluation context (available in routing_script):
    - sender: sender email address
    - subject: email subject line
    - message: email body text
    - frappe: frappe module
    """
    try:
        agents = frappe.get_all(
            "Runtime Agent",
            filters={"trigger_channel_wakeup": 1, "enabled": 1},
            fields=["name"],
        )
    except Exception as exc:
        logger.error("resolve_gmail_agent: failed to list agents: %s", exc)
        return None

    default_agent: str | None = None
    context = {
        "sender": sender,
        "subject": subject,
        "message": message_text,
        "frappe": frappe,
    }

    for agent in agents:
        try:
            triggers = frappe.get_all(
                "Runtime External Trigger",
                filters={
                    "parent": agent.name,
                    "parenttype": "Runtime Agent",
                    "channel": "Email",
                    "enabled": 1,
                },
                fields=["routing_script", "is_default_route"],
                order_by="is_default_route asc",
            )
        except Exception:
            continue

        for trigger in triggers:
            if trigger.is_default_route:
                if not default_agent:
                    default_agent = agent.name
                continue

            script = (trigger.routing_script or "").strip()
            if not script:
                continue

            try:
                if eval(script, {"__builtins__": _SAFE_BUILTINS}, context):  # noqa: S307
                    logger.info("resolve_gmail_agent: matched agent=%s for sender=%s", agent.name, sender)
                    return agent.name
            except Exception as exc:
                logger.error("routing_script error on agent %s: %s", agent.name, exc)

    if default_agent:
        logger.info("resolve_gmail_agent: default agent=%s for sender=%s", default_agent, sender)

    return default_agent
