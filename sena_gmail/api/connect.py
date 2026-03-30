"""Gmail OAuth connect/disconnect flow.

Uses Frappe's built-in Connected App + Email Account for Gmail OAuth.

Connect flow:
  1. get_auth_url()       → creates/updates Email Account with OAuth, returns Google auth URL
  2. oauth_callback()     → Google redirects here after user authorizes, closes popup
  3. get_status()         → returns connection status

Per-user Gmail config uses Frappe's Email Account + Connected App (OAuth client).
The Connected App name is stored in User Tool Credential (composite="gmail").
"""
from __future__ import annotations

import json
import logging
from typing import Any

import frappe

logger = logging.getLogger(__name__)


def _get_redirect_uri_for_connected_app(app_name: str) -> str:
    """Return the correct redirect URI for the Connected App, using ngrok URL if configured."""
    conf = frappe.get_single("Runtime Configuration")
    ig_uri = getattr(conf, "instagram_redirect_uri", "") or ""
    if ig_uri:
        from urllib.parse import urlparse
        parsed = urlparse(ig_uri)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        return f"{base_url}/api/method/frappe.integrations.doctype.connected_app.connected_app.callback/{app_name}"
    return frappe.db.get_value("Connected App", app_name, "redirect_uri") or ""


def _get_connected_app_name() -> str:
    """Get the Connected App name for Gmail from Runtime Configuration or find existing."""
    # Check if there's a Gmail Connected App configured
    conf = frappe.get_single("Runtime Configuration")
    app_name = getattr(conf, "gmail_connected_app", "") or ""
    if app_name and frappe.db.exists("Connected App", app_name):
        return app_name

    # Find any Connected App for Google/Gmail
    apps = frappe.get_all(
        "Connected App",
        filters={"provider_name": ["like", "%Google%"]},
        fields=["name"],
        limit=1,
    )
    if apps:
        return apps[0].name

    # Also check by client_id pattern (Google client IDs end with .apps.googleusercontent.com)
    apps = frappe.get_all(
        "Connected App",
        filters={"client_id": ["like", "%.apps.googleusercontent.com"]},
        fields=["name"],
        limit=1,
    )
    if apps:
        return apps[0].name

    return ""


def _get_email_account_name(user: str) -> str:
    """Get or generate email account name for this user."""
    # Check if user already has a Gmail OAuth email account
    accounts = frappe.get_all(
        "Email Account",
        filters={
            "connected_user": user,
            "service": "GMail",
            "auth_method": "OAuth",
        },
        fields=["name"],
        limit=1,
    )
    if accounts:
        return accounts[0].name
    return f"gmail-{frappe.scrub(user)}"


# ── Public API endpoints ─────────────────────────────────────────────────────

@frappe.whitelist()
def get_auth_url() -> dict[str, Any]:
    """Create/update Email Account with Gmail OAuth and return authorization URL.

    POST /api/method/sena_gmail.api.connect.get_auth_url
    """
    connected_app_name = _get_connected_app_name()
    if not connected_app_name:
        frappe.throw(
            "No Gmail Connected App found. Please create a Connected App for Google "
            "in the Frappe admin (Setup → Connected App) with your Google OAuth client credentials.",
            frappe.ValidationError,
        )

    user = frappe.session.user
    email_account_name = _get_email_account_name(user)

    # Create or update Email Account for Gmail OAuth
    if frappe.db.exists("Email Account", email_account_name):
        email_account = frappe.get_doc("Email Account", email_account_name)
    else:
        email_account = frappe.new_doc("Email Account")
        email_account.email_account_name = email_account_name

    email_account.email_id = user
    email_account.service = "GMail"
    email_account.auth_method = "OAuth"
    email_account.connected_app = connected_app_name
    email_account.connected_user = user
    email_account.enable_incoming = 1
    email_account.enable_outgoing = 1
    email_account.use_imap = 1
    email_account.email_server = "imap.gmail.com"
    email_account.incoming_port = 993
    email_account.use_ssl = 1
    email_account.validate_ssl_certificate = 1
    email_account.smtp_server = "smtp.gmail.com"
    email_account.smtp_port = 587
    email_account.use_tls = 1

    # Disable incoming temporarily so validate doesn't try to connect IMAP before OAuth
    email_account.enable_incoming = 0
    email_account.enable_outgoing = 0

    if not email_account.imap_folder:
        email_account.append("imap_folder", {"folder_name": "INBOX"})

    email_account.flags.ignore_validate = True
    email_account.save(ignore_permissions=True)
    frappe.db.commit()

    # Initiate OAuth flow via Frappe's Connected App
    # Override redirect_uri to use our configured ngrok/public URL
    connected_app = frappe.get_doc("Connected App", connected_app_name)
    override_redirect = _get_redirect_uri_for_connected_app(connected_app_name)
    connected_app.redirect_uri = override_redirect
    callback_uri = "/api/method/sena_gmail.api.connect.oauth_callback"
    authorization_url = connected_app.initiate_web_application_flow(
        user=user,
        success_uri=callback_uri,
    )

    frappe.db.commit()

    return {
        "success": True,
        "authorization_url": authorization_url,
        "email_account_name": email_account_name,
    }


@frappe.whitelist(allow_guest=True)
def oauth_callback():
    """Handle OAuth callback after Google authorization.

    GET /api/method/sena_gmail.api.connect.oauth_callback
    """
    # Frappe's Connected App flow handles token exchange automatically.
    # By the time we get here, the token is already stored.
    # Verify the token actually exists before creating the credential.

    user = frappe.session.user
    connected_app_name = _get_connected_app_name()
    token_valid = False

    if user and user != "Guest" and connected_app_name:
        try:
            connected_app = frappe.get_doc("Connected App", connected_app_name)
            token_cache = connected_app.get_token_cache(user)
            token_valid = token_cache is not None and bool(getattr(token_cache, "access_token", None))
        except Exception:
            token_valid = False

        if token_valid:
            email_account_name = _get_email_account_name(user)
            # Enable incoming/outgoing now that OAuth is done
            if frappe.db.exists("Email Account", email_account_name):
                frappe.db.set_value("Email Account", email_account_name, {
                    "enable_incoming": 1,
                    "enable_outgoing": 1,
                })
                # Set uidnext to current latest so we only get NEW emails
                _set_uidnext_to_latest(email_account_name)

            _upsert_credential(user, {
                "email_account": email_account_name,
                "connected_app": connected_app_name,
                "email_id": user,
                "status": "Connected",
            })
            frappe.db.commit()

    if token_valid:
        html = """<!DOCTYPE html><html><body>
        <div style="font-family:sans-serif;text-align:center;margin-top:60px">
            <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                <polyline points="22 4 12 14.01 9 11.01"/>
            </svg>
            <p style="font-size:18px;font-weight:600;color:#10B981;margin-top:16px">Gmail Connected!</p>
            <p style="color:#64748b">This window will close automatically.</p>
        </div>
        <script>
            window.opener && window.opener.postMessage({type:'gmail_connect_success'},'*');
            setTimeout(function(){ window.close(); }, 2000);
        </script>
        </body></html>"""
    else:
        html = """<!DOCTYPE html><html><body>
        <div style="font-family:sans-serif;text-align:center;margin-top:60px">
            <p style="font-size:18px;font-weight:600;color:#EF4444;margin-top:16px">Connection Failed</p>
            <p style="color:#64748b">Gmail authorization did not complete. Please try again.</p>
        </div>
        <script>
            window.opener && window.opener.postMessage({type:'gmail_connect_error',error:'Token not received'},'*');
            setTimeout(function(){ window.close(); }, 3000);
        </script>
        </body></html>"""
    frappe.respond_as_web_page("Gmail Connected", html, http_status_code=200)


@frappe.whitelist()
def get_status() -> dict[str, Any]:
    """Return Gmail connection status for the current user.

    POST /api/method/sena_gmail.api.connect.get_status
    """
    user = frappe.session.user
    connected_app_name = _get_connected_app_name()

    if not connected_app_name:
        return {"status": "not_configured"}

    # Check if user has an OAuth Email Account with a valid token
    accounts = frappe.get_all(
        "Email Account",
        filters={
            "connected_user": user,
            "service": "GMail",
            "auth_method": "OAuth",
            "connected_app": connected_app_name,
        },
        fields=["name", "email_id"],
        limit=1,
    )

    if not accounts:
        return {"status": "not_connected"}

    # Verify token exists and has an actual access_token
    try:
        connected_app = frappe.get_doc("Connected App", connected_app_name)
        token_cache = connected_app.get_token_cache(user)
        if token_cache and getattr(token_cache, "access_token", None):
            return {
                "status": "connected",
                "email": accounts[0].email_id,
                "email_account": accounts[0].name,
            }
    except Exception:
        pass

    return {"status": "not_connected"}


@frappe.whitelist()
def disconnect() -> dict[str, Any]:
    """Disconnect Gmail for the current user.

    POST /api/method/sena_gmail.api.connect.disconnect
    """
    user = frappe.session.user

    # Delete the Email Account
    accounts = frappe.get_all(
        "Email Account",
        filters={
            "connected_user": user,
            "service": "GMail",
            "auth_method": "OAuth",
        },
        fields=["name"],
    )
    for acc in accounts:
        frappe.delete_doc("Email Account", acc.name, ignore_permissions=True)

    # Disable credential
    from sena_agents_backend.sena_agents_backend.doctype.user_tool_credential.user_tool_credential import (
        get_credential_for_user,
    )
    cred = get_credential_for_user(user, composite="gmail")
    if cred:
        cred.enabled = 0
        cred.config_json = json.dumps({})
        cred.flags.ignore_links = True
        cred.save(ignore_permissions=True)

    frappe.db.commit()
    logger.info("Gmail disconnected for user=%s", user)
    return {"status": "disconnected"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_uidnext_to_latest(email_account_name: str) -> None:
    """Connect to IMAP and set uidnext to the latest UID so only new emails are pulled."""
    try:
        import imaplib
        ea = frappe.get_doc("Email Account", email_account_name)
        token = ea.get_access_token()
        if not token:
            return

        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        # OAuth2 IMAP auth
        auth_string = f"user={ea.email_id}\x01auth=Bearer {token}\x01\x01"
        imap.authenticate("XOAUTH2", lambda x: auth_string.encode())
        imap.select("INBOX")

        # Get the highest UID
        status, data = imap.uid("search", None, "ALL")
        if status == "OK" and data[0]:
            uids = data[0].split()
            if uids:
                latest_uid = int(uids[-1])
                # Set uidnext to latest+1 so only brand new emails are fetched
                ea.db_set("uidnext", latest_uid + 1)
                frappe.db.commit()
                logger.info("Set uidnext to %s for %s", latest_uid + 1, email_account_name)

        imap.close()
        imap.logout()
    except Exception as exc:
        logger.warning("Failed to set uidnext: %s", exc)


def _upsert_credential(user: str, config: dict) -> None:
    """Create or update the user's Gmail User Tool Credential."""
    from sena_agents_backend.sena_agents_backend.doctype.user_tool_credential.user_tool_credential import (
        get_credential_for_user,
    )

    cred = get_credential_for_user(user, composite="gmail")
    if cred:
        existing = cred.get_config()
        existing.update(config)
        cred.config_json = json.dumps(existing)
        cred.enabled = 1
        cred.flags.ignore_links = True
        cred.save(ignore_permissions=True)
    else:
        doc = frappe.get_doc({
            "doctype": "User Tool Credential",
            "user": user,
            "composite": "gmail",
            "enabled": 1,
            "config_json": json.dumps(config),
        })
        doc.flags.ignore_links = True
        doc.insert(ignore_permissions=True)
