"""Google Contacts API for Sena Agents.

Fetches contacts from Google People API using the existing Gmail OAuth token.
Provides search and listing with cross-channel matching.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any

import frappe

logger = logging.getLogger(__name__)

_PEOPLE_API = "https://people.googleapis.com/v1"


def _get_google_token() -> str:
    """Get Google OAuth access token from Connected App."""
    try:
        creds = frappe.get_all(
            "User Tool Credential",
            filters={"composite": "gmail", "enabled": 1, "user": frappe.session.user},
            fields=["config_json"],
            limit=1,
        )
        if not creds:
            return ""

        config = json.loads(creds[0].config_json or "{}")
        connected_app_name = config.get("connected_app", "")
        if not connected_app_name:
            apps = frappe.get_all("Connected App", filters={"provider_name": "Google"}, fields=["name"], limit=1)
            if not apps:
                return ""
            connected_app_name = apps[0].name

        connected_app = frappe.get_doc("Connected App", connected_app_name)
        email_id = config.get("email_id") or frappe.session.user
        token_cache = connected_app.get_active_token(email_id)
        if token_cache:
            return token_cache.get_password("access_token")
    except Exception as exc:
        logger.debug("Could not get Google token: %s", exc)
    return ""


@frappe.whitelist()
def get_contacts(query: str = "", page_size: int = 50, page_token: str = "") -> dict[str, Any]:
    """Fetch Google Contacts for the current user.

    POST /api/method/sena_gmail.api.contacts.get_contacts
    """
    token = _get_google_token()
    if not token:
        return {"success": False, "contacts": [], "message": "Not connected to Google"}

    try:
        if query:
            contacts = _search_contacts(token, query, page_size)
        else:
            contacts = _list_contacts(token, page_size, page_token)

        # Enrich with cross-channel matching
        _match_channels(contacts.get("contacts", []))

        return contacts
    except Exception as exc:
        logger.exception("Failed to fetch contacts")
        return {"success": False, "contacts": [], "message": str(exc)}


def _list_contacts(token: str, page_size: int, page_token: str) -> dict:
    """List contacts from Google People API."""
    params = {
        "pageSize": min(page_size, 100),
        "personFields": "names,emailAddresses,phoneNumbers,photos,organizations",
        "sortOrder": "FIRST_NAME_ASCENDING",
    }
    if page_token:
        params["pageToken"] = page_token

    url = f"{_PEOPLE_API}/people/me/connections?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    contacts = []
    for person in data.get("connections", []):
        contact = _parse_person(person)
        if contact:
            contacts.append(contact)

    return {
        "success": True,
        "contacts": contacts,
        "next_page_token": data.get("nextPageToken", ""),
        "total": data.get("totalPeople", len(contacts)),
    }


def _search_contacts(token: str, query: str, page_size: int) -> dict:
    """Search contacts by name, email, or phone."""
    params = {
        "query": query,
        "pageSize": min(page_size, 30),
        "readMask": "names,emailAddresses,phoneNumbers,photos,organizations",
    }

    url = f"{_PEOPLE_API}/people:searchContacts?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())

    contacts = []
    for result in data.get("results", []):
        person = result.get("person", {})
        contact = _parse_person(person)
        if contact:
            contacts.append(contact)

    return {
        "success": True,
        "contacts": contacts,
        "next_page_token": "",
        "total": len(contacts),
    }


def _parse_person(person: dict) -> dict | None:
    """Parse a Google People API person into a simplified contact dict."""
    names = person.get("names", [])
    emails = person.get("emailAddresses", [])
    phones = person.get("phoneNumbers", [])
    photos = person.get("photos", [])
    orgs = person.get("organizations", [])

    if not names and not emails and not phones:
        return None

    name = names[0].get("displayName", "") if names else ""
    if not name and emails:
        name = emails[0].get("value", "")

    return {
        "id": person.get("resourceName", ""),
        "name": name,
        "first_name": names[0].get("givenName", "") if names else "",
        "last_name": names[0].get("familyName", "") if names else "",
        "emails": [e.get("value", "") for e in emails],
        "phones": [_normalize_phone(p.get("value", "")) for p in phones],
        "photo": photos[0].get("url", "") if photos else "",
        "organization": orgs[0].get("name", "") if orgs else "",
        "channels": [],  # Populated by _match_channels
    }


def _normalize_phone(phone: str) -> str:
    """Normalize phone number — strip spaces, dashes."""
    return phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")


def _match_channels(contacts: list) -> None:
    """Match contacts to available channels.

    Shows BOTH:
    - Existing conversations (instance already exists)
    - Potential channels (has phone → can WhatsApp/Telegram, has email → can Email)

    This lets users start new conversations with contacts who haven't messaged yet.
    """
    if not contacts:
        return

    # Check which channel apps are connected (have credentials)
    connected_channels = set()
    creds = frappe.get_all(
        "User Tool Credential",
        filters={"enabled": 1},
        fields=["composite"],
    )
    for c in creds:
        if c.composite == "whatsapp":
            connected_channels.add("whatsapp")
        elif c.composite == "telegram":
            connected_channels.add("telegram")
        elif c.composite == "gmail":
            connected_channels.add("email")
        elif c.composite == "slack":
            connected_channels.add("slack")
        elif c.composite == "instagram":
            connected_channels.add("instagram")

    # Collect all phones and emails from contacts
    all_phones = set()
    all_emails = set()
    for c in contacts:
        all_phones.update(c.get("phones", []))
        all_emails.update(e.lower() for e in c.get("emails", []))

    # Find matching Runtime Instances across all channels
    instances = frappe.get_all(
        "Runtime Instance",
        filters=[
            ["instance_id", "like", "std:%"],
            ["status", "!=", "killed"],
        ],
        fields=["instance_id", "title", "agent_name"],
        limit=500,
    )

    # Build lookup maps
    instance_map = {}  # normalized identifier → instance info
    for inst in instances:
        iid = inst.instance_id

        if iid.startswith("std:wa:"):
            # WhatsApp — match by phone
            phone = iid[7:]  # strip std:wa:
            if phone.startswith("+"):
                phone = phone[1:]
            instance_map[phone] = {"channel": "whatsapp", "instance_id": iid, "title": inst.title}
            # Also store without country code variants
            if len(phone) > 10:
                instance_map[phone[-10:]] = {"channel": "whatsapp", "instance_id": iid, "title": inst.title}

        elif iid.startswith("std:tg:"):
            # Telegram — match by chat_id (not phone, so limited matching)
            pass

        elif iid.startswith("std:em:"):
            # Email — match by email
            email_part = iid[7:].replace("_at_", "@").replace("_", ".").lower()
            instance_map[email_part] = {"channel": "email", "instance_id": iid, "title": inst.title}

        elif iid.startswith("std:sl:"):
            # Slack — match by channel ID (limited matching)
            pass

        elif iid.startswith("std:ig:"):
            # Instagram — match by sender ID (limited matching)
            pass

    # Match contacts — show both existing conversations AND potential channels
    for contact in contacts:
        channels = []
        seen_channels = set()  # avoid duplicates

        # Match existing conversations by phone
        for phone in contact.get("phones", []):
            clean = phone.lstrip("+")
            if clean in instance_map:
                ch = instance_map[clean]
                channels.append({**ch, "exists": True})
                seen_channels.add(ch["channel"])
            elif len(clean) > 10 and clean[-10:] in instance_map:
                ch = instance_map[clean[-10:]]
                channels.append({**ch, "exists": True})
                seen_channels.add(ch["channel"])

        # Match existing conversations by email
        for email in contact.get("emails", []):
            if email.lower() in instance_map:
                ch = instance_map[email.lower()]
                channels.append({**ch, "exists": True})
                seen_channels.add(ch["channel"])

        # Add POTENTIAL channels (no conversation yet, but can start one)
        phones = contact.get("phones", [])
        emails = contact.get("emails", [])

        if phones and "whatsapp" not in seen_channels and "whatsapp" in connected_channels:
            phone = phones[0].lstrip("+")
            channels.append({
                "channel": "whatsapp",
                "instance_id": f"std:wa:{phone}",
                "title": f"WhatsApp: {phones[0]}",
                "exists": False,
                "target": phone,
            })

        if phones and "telegram" not in seen_channels and "telegram" in connected_channels:
            channels.append({
                "channel": "telegram",
                "instance_id": "",
                "title": f"Telegram",
                "exists": False,
                "target": phones[0],
            })

        if emails and "email" not in seen_channels and "email" in connected_channels:
            email = emails[0]
            safe = email.lower().replace("@", "_at_").replace(".", "_")
            channels.append({
                "channel": "email",
                "instance_id": f"std:em:{safe}",
                "title": f"Email: {email}",
                "exists": False,
                "target": email,
            })

        contact["channels"] = channels
