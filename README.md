Cabot Slack Plugin (Affirm Version)
=====

This is an alert plugin for the Cabot service monitoring tool.
It allows you to alert users by their user handle in a Slack channel.

**This plugin is designed to work with the [Affirm/cabot](https://github.com/Affirm/cabot) fork.**

## Installation

Enter the cabot virtual environment.

```bash
pip install cabot-alert-slack
# Add cabot_alert_slack to the installed apps in settings.py
foreman run python manage.py syncdb
foreman start
```

# Use

Create a Slack app with the following permissions: `channels:manage`
(for adding @mentioned users to channels), `channels:join` (for automatically joining public channels),
`users:read` and `users:read.email` (to look up who to @msg by email),
`files:write` (for uploading Grafana images), `chat:write` (for posting alerts).

Open the admin panel and add a `slack instance`.
You'll need the server URL and a bot access token.

Add the `Slack` alert type to the service you want to alert on.
Make sure you also select a `slack instance` and enter a `slack channel name`.
