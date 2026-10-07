# mini_deploy

Lightweight, self-hosted server operations and request monitoring panel for Linux.
The name stays mini_deploy; Git-based application deployment is no longer part of
the product.

- [中文快速上手](docs/QUICKSTART.zh-CN.md)
- [首次使用与升级说明](docs/BEGINNER_DEPLOY_FLOW.zh-CN.md)
- [Linux 验收清单](docs/LINUX_TEST.zh-CN.md)
- [独立请求网关](docs/REQUEST_GATEWAY.zh-CN.md)
- [Roadmap](docs/ROADMAP.zh-CN.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## Scope

- CPU, memory, disk and network monitoring.
- Website availability, response latency and live TLS certificate checks: enter a URL; no proxy changes required.
- Sustained alerts, recovery messages, repeat limits and temporary notification mute via WeCom, DingTalk or email.
- Docker container status, logs and management; image listing, search, pull and removal.
- Edit existing host Docker mirror settings in the panel, with validation, backups and automatic activation.
- Discover websites and public certificates from local services, Docker configurations and common certificate directories, without initial proxy selection or path entry.
- Local or Docker Nginx, business sites and uploaded PEM certificate management.
- Request collection from supported Nginx/Caddy logs.
- Independent Docker request gateways for HTTP, SSE and WebSocket traffic.

Business services must already be running. There is no repository onboarding,
deployment script form, push-to-deploy WebHook, build pipeline, application rollback
or automatic generation of business runtime files.

Open **Domains and certificates** to see discovered websites and certificate expiry.
Scanning starts in the background and repeats every ten minutes without changing
business configuration. Supported configuration readers are Nginx and Caddy. Other
certificate files are listed separately and are not assumed to be active websites.
Automatic renewals stay with the original service. Eligible manually configured
Nginx certificates can be replaced with validation, backups and rollback; symlinks,
read-only external mounts and single-file container mounts are not overwritten.
Previously managed certificates use the same replacement action, preserving their
metadata and backups. Internal/default sites are collapsed; duplicate setup forms
have been removed without deleting existing server configuration.

## Install And Sign In

Requirements: Linux with root/sudo, systemd, Python 3.10+ and Git to download
mini_deploy itself. Docker and Nginx are optional, depending on the features used.
No business repository or deployment credentials are needed.

Optional Docker installation supports container management, Docker Nginx and request
gateways. `SETUP_DOCKER=ask` prompts interactively with a default of No and skips
installation without a terminal; it never automatically selects Yes. Neither Docker
Compose nor PyYAML is required.

> [!WARNING]
> This is a pre-release project. The default installer runs the Agent as root;
> privilege separation and complete restore/version rollback are unfinished.
> Use a dedicated, controlled Linux test server.

```bash
git clone https://gitee.com/XC1960/mini_deploy.git /root/mini_deploy
cd /root/mini_deploy
bash install.sh
```

Set the administrator password in the installer, then open
`http://<server-public-ip>:6868`. The Agent listens on `0.0.0.0:6868`; allow TCP
6868 in the cloud security group and restrict sources. The installer can adjust
active UFW/firewalld rules. Use HTTPS or an SSH tunnel before sending sensitive
values. A domain is optional; `DEPLOY_DOMAIN` configures the panel's additional
Nginx entry point.

Start with server metrics and Docker status. For an existing website, select a
supported proxy log source or follow the [request gateway guide](docs/REQUEST_GATEWAY.zh-CN.md).
Monitoring does not require adding a Git project.

## Upgrade Boundary

Deployment pages, queues, script execution and repository detection adapters have
been removed. Old deployment APIs, including manual deployment, deployment rollback
and WebHook triggers, return HTTP `410 Gone`.

Sites are saved in `sites.json`. Legacy `projects.json` is read only to extract site
settings and is not modified. Monitoring writes `monitoring-state.json`; the original
`state.json` is retained and its deployment queue is never loaded or resumed,
including after Agent restarts.

Existing server business files, configurations, scripts, services, containers and
persistent data are retained. Removing deployment support does not uninstall,
stop or recreate business services. Application releases remain the owner's
responsibility outside this panel.

Reuse the existing installation paths and environment file. Preserve a server
snapshot and the installer backup before upgrading. Do not remove old
`projects.json`, state, scripts or business directories to bypass a migration error.

## Maintenance

Default-layout commands (custom installations must use the parameterized commands
printed by their installer):

```bash
systemctl status mini-deploy-agent
journalctl -u mini-deploy-agent -n 100 --no-pager
curl -fsS http://127.0.0.1:6868/health
bash /opt/mini_deploy/scripts/doctor.sh
python3 /opt/mini_deploy/agent.py admin set-password
python3 /opt/mini_deploy/agent.py admin reset-session
```

Restart the Agent after changing administrator credentials.

The default data directory is `/var/lib/mini-deploy-agent`, logs are in
`/var/log/mini_deploy`, and installation backups are in
`/var/backups/mini-deploy-agent`. Keep legacy configuration and backups private:
they can still contain credentials. A complete backup bundle contains
`installation.tar.gz`, `manifest` and `complete`. Validate a retained bundle with:

```bash
python3 /opt/mini_deploy/scripts/verify_backup.py <bundle-directory>
```

Validation is read-only and does not restore anything. Panel backups do not replace
backups of business databases, Docker volumes or external proxy configuration.
For manual backups and custom paths, use the complete backup command printed by
the installer. See [Security](SECURITY.md) for remaining limitations.

## Validation Status

The full automated suite passed 476 tests, with 19 skipped because Linux/Docker
environments were unavailable. Site creation, editing and deletion also passed a UI
test against the real backend. Linux server acceptance has not yet been performed;
see the [checklist](docs/LINUX_TEST.zh-CN.md) and [roadmap](docs/ROADMAP.zh-CN.md).
