# Sena Gmail — Setup Guide

## Prerequisites

- Frappe bench running
- sena_agents_backend installed
- sena_gmail app installed (`bench get-app sena_gmail && bench --site yoursite install-app sena_gmail`)

---

## Step 1: Google Cloud Console Setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or select existing)
3. **Enable Gmail API:**
   - APIs & Services → Library → search "Gmail API" → Enable
4. **Create OAuth Credentials:**
   - APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
   - Application type: **Web application**
   - Name: "Sena Gmail"
   - Authorized redirect URIs — add:
     ```
     https://YOUR_DOMAIN/api/method/frappe.integrations.doctype.connected_app.connected_app.callback/CONNECTED_APP_NAME
     ```
     (Replace YOUR_DOMAIN with your public URL and CONNECTED_APP_NAME with the auto-generated name after Step 2)
   - Copy **Client ID** and **Client Secret**

5. **OAuth Consent Screen:**
   - APIs & Services → OAuth consent screen
   - Add scopes: `https://mail.google.com/`, `https://www.googleapis.com/auth/gmail.send`, `https://www.googleapis.com/auth/gmail.readonly`
   - Add test users if app is in "Testing" mode

---

## Step 2: Create Connected App in Frappe

Go to `http://yoursite:8000/app/connected-app/new` and fill:

| Field | Value |
|---|---|
| Provider Name | Google |
| Client ID | (from Google Console) |
| Client Secret | (from Google Console) |
| Authorization URI | `https://accounts.google.com/o/oauth2/v2/auth` |
| Token URI | `https://oauth2.googleapis.com/token` |
| Revocation URI | `https://oauth2.googleapis.com/revoke` |
| User Info URI | `https://www.googleapis.com/oauth2/v2/userinfo` |

**Scopes** (add 3 rows in the Scopes table):
- `https://mail.google.com/`
- `https://www.googleapis.com/auth/gmail.send`
- `https://www.googleapis.com/auth/gmail.readonly`

**Query Parameters** (add 2 rows — needed for refresh token):
- Key: `access_type`, Value: `offline`
- Key: `prompt`, Value: `consent`

Save the Connected App. Note the auto-generated name (e.g., `it0p9k2e3a`).

---

## Step 3: Set Redirect URI

After saving, the Connected App generates a redirect URI like:
```
http://yoursite:8000/api/method/frappe.integrations.doctype.connected_app.connected_app.callback/CONNECTED_APP_NAME
```

**If using ngrok (local dev):**

The redirect URI must be your public ngrok URL. Frappe auto-generates it from localhost which won't work.

Option A — Force via bench console:
```python
frappe.db.set_value("Connected App", "CONNECTED_APP_NAME", "redirect_uri",
    "https://your-ngrok-url.ngrok.dev/api/method/frappe.integrations.doctype.connected_app.connected_app.callback/CONNECTED_APP_NAME")
frappe.db.commit()
```

Option B — The sena_gmail app reads the Instagram redirect URI base URL from Runtime Configuration (`instagram_redirect_uri` field) and reuses the domain for Gmail. If you've configured Instagram, Gmail will auto-use the same base URL.

**Add this exact redirect URI to Google Cloud Console** → Credentials → your OAuth client → Authorized redirect URIs.

---

## Step 4: Connect Gmail in Channels UI

1. Open Sena frontend → Channels agent → channels-ui
2. Click the **Email** tab
3. Click **Connect**
4. Google OAuth popup opens → authorize with your Gmail account
5. After authorization, the popup closes and shows "Gmail Connected!"

**Important:** If using ngrok, log in at `https://your-ngrok-url.ngrok.dev` first in the same browser, so the OAuth callback has a valid session.

---

## Step 5: Configure Email Trigger on Agent

In the Frappe admin or via bench console, add an Email trigger to your Channels agent:

```python
agent = frappe.get_doc("Runtime Agent", "Channels")
agent.append("external_triggers", {
    "channel": "Email",
    "enabled": 1,
    "is_default_route": 1,
})
agent.save(ignore_permissions=True)
frappe.db.commit()
```

Also ensure the agent has gmail tools assigned:
- `gmail_send`
- `gmail_reply`
- `gmail_search`

---

## How It Works

### Receiving Emails
- Gmail API is polled every 15 seconds for new unread emails (last 1 hour)
- New emails create Runtime Instances (`std:em:` prefix) and route to the configured agent
- Agent receives the email content and can reply using `gmail_reply` tool

### Sending Emails
- Agent calls `gmail_send(to, subject, body)` or `gmail_reply(to, subject, body)`
- Frappe's `sendmail()` queues the email
- Email Queue worker sends via Gmail SMTP (OAuth)
- The `senaerp_platform` hook (`override_email_send`) detects Gmail accounts and uses SMTP instead of Microsoft Graph

### Token Refresh
- Google OAuth tokens expire after 1 hour
- The `access_type=offline` + `prompt=consent` query params ensure a **refresh_token** is issued
- Frappe's Connected App automatically refreshes the token using the refresh_token

---

## Troubleshooting

### "redirect_uri_mismatch" error
- The redirect URI in Google Console must **exactly** match what Frappe sends
- Check: `frappe.db.get_value("Connected App", "NAME", "redirect_uri")`
- Frappe's `validate()` may overwrite it — use `db.set_value()` to force

### Emails not being sent (queue shows "Not Sent")
- Check Email Queue for errors: `/app/email-queue`
- If error mentions "Graph API" — the `senaerp_platform` hook is intercepting. Ensure the Gmail bypass is in `graph_email.py`

### Old emails being pulled
- The poll service uses `newer_than:1h` Gmail API query — only fetches last hour
- Processed message IDs are tracked in Redis to prevent duplicates

### Token expired / no refresh_token
- Re-authorize: delete Token Cache, delete credential, click Connect again
- Ensure Connected App has `access_type=offline` and `prompt=consent` in Query Parameters

---

## Current Configuration

| Setting | Value |
|---|---|
| Connected App | `it0p9k2e3a` |
| Client ID | `1019601132700-lle30vcu55eo5159vfdgle72rfno0q7h.apps.googleusercontent.com` |
| Redirect URI | `https://senabackend.ngrok.dev/api/method/frappe.integrations.doctype.connected_app.connected_app.callback/it0p9k2e3a` |
| Email Account | `gmail-shelton013@gmail.com` |
| Scopes | `https://mail.google.com/`, `gmail.send`, `gmail.readonly` |
| Poll Interval | 15 seconds |
| Gmail API Query | `is:unread in:inbox newer_than:1h` |
