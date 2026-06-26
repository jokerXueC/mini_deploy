# VibePilot Deploy

Lightweight self-hosted deploy panel for small servers.

VibePilot Deploy receives Git webhooks, runs your project deploy script, and shows deploy history, logs, server status, and Docker status in a simple web panel.

中文快速上手：[docs/QUICKSTART.zh-CN.md](docs/QUICKSTART.zh-CN.md)

## What It Does

- Receives Gitee / GitHub / GitLab push webhooks
- Runs one deploy script per project
- Supports multiple projects
- Provides manual deploy, rollback, cancel, logs, and history
- Shows CPU, memory, disk, network, and Docker container status
- Uses a password-protected web UI
- Requires no database

## What It Does Not Do

VibePilot does not automatically understand every application.

For each business project, you still need to confirm:

- how to build it
- how to start or restart it
- which port it listens on
- which health URL means it is running correctly

The panel can generate a starter `deploy.sh`, but you should review it before enabling automatic deployment.

## Requirements

- Linux server with root / sudo access
- Python 3.10+
- `git` installed before running the installer
- systemd
- Optional: Docker, if your projects use Docker
- Optional: Nginx / Certbot, which the installer can help set up

## Install

```bash
git clone https://gitee.com/XC1960/mini_deploy.git /root/vibepilot-deploy
cd /root/vibepilot-deploy
bash install.sh
```

The installer asks for language first. Chinese is the default; enter `en` for English.

For non-interactive install:

```bash
DEPLOY_DOMAIN=deploy.example.com bash install.sh
```

For non-interactive English install:

```bash
INSTALL_LANG=en DEPLOY_DOMAIN=deploy.example.com bash install.sh
```

HTTP works without HTTPS:

```text
http://deploy.example.com/deploy/ui
```

Use HTTPS for public access when DNS is ready.

## First Project

In the web panel:

1. Add a project.
2. Fill repository URL, branch, server directory, deploy script path, and health URL.
3. Copy the generated server commands and run them once on the server.
4. Review the generated `deploy.sh`.
5. Click “Check Project”.
6. Configure WebHook URL and Token in your Git platform.
7. Enable the project.

After that, every `git push` to the configured branch can trigger deployment.

## Important Boundary

`deploy.sh` is your project’s deployment recipe.

It usually does:

```text
git pull
install dependencies / build
restart service
health check
```

If your service is managed by systemd, the service file is still part of your business project setup. VibePilot can provide examples and checks, but it cannot guarantee a correct service file for every possible application.

## Common Commands

```bash
systemctl status vibepilot-deploy-agent
journalctl -u vibepilot-deploy-agent -f
curl http://127.0.0.1:9010/health
```

## Security

- Do not expose port `9010` directly to the internet.
- Use HTTPS for public access.
- Use a strong UI password.
- Use a different webhook token for every project.
- Review deploy scripts before enabling automatic deployment.
