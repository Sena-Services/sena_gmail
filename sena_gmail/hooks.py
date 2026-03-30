app_name = "sena_gmail"
app_title = "Sena Gmail"
app_publisher = "Sena Services"
app_description = "Gmail integration for Sena Agents"
app_version = "0.0.1"
required_apps = ["sena_agents_backend"]

after_install = "sena_gmail.setup.after_install"
after_migrate = "sena_gmail.setup.after_migrate"
before_uninstall = "sena_gmail.setup.before_uninstall"

# Register Gmail tools with the sena_agents_backend tool registry
sena_tool_modules = ["sena_gmail.tools.gmail_tools"]

# Poll for new emails — "all" runs every ~5 seconds in dev mode
scheduler_events = {
    "all": [
        "sena_gmail.services.gmail_poll_service.poll_new_emails",
    ],
}
