# VibePilot Deploy

Lightweight self-hosted deploy panel for small servers.

中文小白上手指南: [docs/QUICKSTART.zh-CN.md](docs/QUICKSTART.zh-CN.md)

VibePilot Deploy is a tiny Python deploy agent with a built-in web dashboard. It receives Git webhooks, runs your project deploy scripts, records deploy history, shows server health, and lets you inspect Docker containers and logs.

It is designed for low-resource servers where GitLab CI, Jenkins, or a full CI runner is too heavy.

## Features

- Webhook deploy for Gitee, GitHub, and GitLab style payloads
- Multiple projects in one agent
- Per-project branch, workdir, deploy script, health URL, log file, and secret
- Manual deploy and rollback from the web panel
- Deploy history with status bars
- Server CPU, memory, disk, network status
- Docker container status, logs, restart, stop, start, pause, and resume
- Password-protected UI
- No database required

## Requirements

- Linux server
- Python 3.10+
- git
- systemd
- Optional: Docker and Docker Compose if your projects use containers
- Optional: Nginx or Caddy for HTTPS reverse proxy

## Install

Clone this repository on your server:

```bash
git clone https://github.com/your-org/vibepilot-deploy.git /root/vibepilot-deploy
cd /root/vibepilot-deploy
bash install.sh
```

Edit environment if you want to change the port, host, or log paths:

```bash
nano /etc/vibepilot-deploy-agent.env
```

Edit project configuration:

```bash
nano /opt/vibepilot-deploy/projects.json
```

The bundled example projects are disabled by default. Replace `repo`, `workdir`, `script`, `health_url`, and `webhook_secret`, then set `enabled` to `true`.

Start the agent:

```bash
systemctl restart vibepilot-deploy-agent
systemctl status vibepilot-deploy-agent
curl http://127.0.0.1:9010/health
```

Open the UI through your reverse proxy:

```text
https://your-domain.example/deploy/ui
```

On first open, set the admin password in the web page. The agent writes the encrypted password into `/etc/vibepilot-deploy-agent.env`; restart the service once after that:

```bash
systemctl restart vibepilot-deploy-agent
```

## Beginner Flow

After installation, you do not need to write CI/CD YAML.

1. Open `/deploy/ui` and log in.
2. Click `Add Repository`.
3. Choose your project type:
   - Docker Compose
   - Node / PM2
   - Java / systemd
   - Go / systemd
   - Static website
   - Custom script
4. Fill in repository URL, branch, server directory, health URL, and log file.
5. Copy the generated server commands and run them on your server.
6. Save the project.
7. Click `Check Project` to see what is missing.
8. Copy the generated WebHook URL and Token into Gitee/GitHub/GitLab.
9. Push code and watch the deploy panel.

The generated script is only a starting point. You can edit it for your actual build and restart commands.

For local testing:

```text
http://127.0.0.1:9010/ui
```

## Project Config

Each project points to a shell script. The agent itself is written in Python, but it can deploy Java, Go, Node, Python, Rust, PHP, static sites, Docker Compose apps, or anything else your shell script can handle.

Example:

```json
{
  "projects": [
    {
      "key": "node-api",
      "name": "Node API",
      "template": "node",
      "repo": "git@gitee.com:your-org/node-api.git",
      "branch": "main",
      "workdir": "/srv/node-api",
      "script": "/srv/node-api/deploy/deploy.sh",
      "rollback_script": "/srv/node-api/deploy/rollback.sh",
      "health_url": "https://api.example.com/health",
      "deploy_log_file": "/var/log/vibepilot/node-api-deploy.log",
      "webhook_secret": "replace-with-node-api-webhook-token",
      "enabled": true,
      "manual_deploy_enabled": true,
      "timeout_seconds": 900
    }
  ]
}
```

## Webhook URL

For a project with key `node-api`, use:

```text
https://your-domain.example/deploy/webhook?project=node-api&token=replace-with-node-api-webhook-token
```

Use POST requests. The webhook payload should include a `ref` like:

```json
{
  "ref": "refs/heads/main",
  "before": "old-sha",
  "after": "new-sha"
}
```

Gitee, GitHub, and GitLab push events usually already include these fields.

## Nginx Reverse Proxy

```nginx
location = /deploy/webhook {
    proxy_pass http://127.0.0.1:9010/webhook;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}

location = /deploy/ui {
    return 302 /deploy/ui/;
}

location /deploy/ui/ {
    proxy_pass http://127.0.0.1:9010/ui/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}

location /deploy/ {
    proxy_pass http://127.0.0.1:9010/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Security Notes

This agent can run shell scripts and control Docker containers. Treat it as an admin tool.

- Do not expose port `9010` directly to the internet.
- Use HTTPS reverse proxy.
- Use a strong UI password.
- Use a different webhook secret for every project.
- Keep `projects.json` and `/etc/vibepilot-deploy-agent.env` readable only by root.
- Review every deploy script before enabling manual deploy.

## Development

Run locally:

```bash
python3 agent.py hash-password
DEPLOY_PROJECTS_FILE=examples/projects.example.json \
DEPLOY_AGENT_STATE_FILE=state.json \
DEPLOY_AGENT_LOG=agent.log \
DEPLOY_LOG_FILE=deploy.log \
DEPLOY_UI_PASSWORD_HASH='...' \
DEPLOY_UI_SESSION_SECRET='...' \
python3 agent.py
```

Then open:

```text
http://127.0.0.1:9010/ui
```
