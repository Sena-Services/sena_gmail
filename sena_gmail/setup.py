"""Install / uninstall hooks for sena_gmail."""
from __future__ import annotations

import frappe

GMAIL_TOOL_NAMES = ["gmail_send", "gmail_reply", "gmail_search"]
COMPOSITE_NAME = "gmail"


def _patch_channel_options(add: bool) -> None:
    """Add or remove 'Email' from Runtime External Trigger.channel select options."""
    field = frappe.db.get_value(
        "DocField",
        {"parent": "Runtime External Trigger", "fieldname": "channel"},
        ["name", "options"],
        as_dict=True,
    )
    if not field:
        return

    options = [o for o in (field.options or "").split("\n") if o.strip()]

    if add and "Email" not in options:
        options.append("Email")
        frappe.db.set_value("DocField", field.name, "options", "\n".join([""] + options))

    elif not add and "Email" in options:
        options.remove("Email")
        frappe.db.set_value("DocField", field.name, "options", "\n".join([""] + options))


def after_install() -> None:
    """Create the Gmail composite and tag tools."""
    _patch_channel_options(add=True)

    if not frappe.db.exists("Runtime Composite", COMPOSITE_NAME):
        frappe.get_doc({
            "doctype": "Runtime Composite",
            "composite_name": COMPOSITE_NAME,
            "title": "Gmail",
            "description": "Gmail email integration via OAuth",
            "is_system": 0,
            "channel_prefix": "std:em:",
            "inject_queue_template": "em_inject:{instance_id}",
            "reply_tool": "gmail_reply",
            "reply_instruction_template": (
                "IMPORTANT: You MUST use the {reply_tool} tool to reply to {reply_to}. "
                "Do NOT use any other method."
            ),
        }).insert(ignore_permissions=True)

    for name in GMAIL_TOOL_NAMES:
        if frappe.db.exists("Runtime Tool", name):
            frappe.db.set_value("Runtime Tool", name, "composite", COMPOSITE_NAME)

    frappe.db.commit()


def after_migrate() -> None:
    """Re-sync on migrate."""
    _patch_channel_options(add=True)


def before_uninstall() -> None:
    """Clean up composite."""
    for name in GMAIL_TOOL_NAMES:
        if frappe.db.exists("Runtime Tool", name):
            frappe.db.set_value("Runtime Tool", name, "composite", "")

    _patch_channel_options(add=False)

    if frappe.db.exists("Runtime Composite", COMPOSITE_NAME):
        frappe.delete_doc("Runtime Composite", COMPOSITE_NAME, ignore_permissions=True)

    frappe.db.commit()
