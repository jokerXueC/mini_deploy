# mini_deploy

Lightweight self-hosted deploy panel for small servers.

mini_deploy receives Git webhooks, runs your project deploy script, and shows deploy history, logs, server status, and Docker status in a simple web panel.

- 中文快速上手：[docs/QUICKSTART.zh-CN.md](docs/QUICKSTART.zh-CN.md)
- Linux 实测清单：[docs/LINUX_TEST.zh-CN.md](docs/LINUX_TEST.zh-CN.md)
- 开源开发路线图：[docs/ROADMAP.zh-CN.md](docs/ROADMAP.zh-CN.md)
- 安全策略：[SECURITY.md](SECURITY.md)
- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)

## What It Does

- Receives Gitee / GitHub / GitLab push webhooks
- Runs one deploy script per project
- Supports multiple projects
- Provides manual deploy, rollback, cancel, logs, and history
- Shows CPU, memory, disk, network, and Docker container status
- Generates per-project Nginx reverse proxy config for business domains
- Manages uploaded PEM certificates for project domains from the UI: expiry, replacement, HTTPS activation and removal
- Detects local and Docker Nginx, saves a shared runtime selection and checks each project's upstream connectivity
- Uses a password-protected web UI
- Requires no database

## What It Does Not Do

mini_deploy does not automatically understand every application.

For each business project, you still need to confirm:

- how to build it
- how to start or restart it
- which port it listens on
- which health URL means it is running correctly

The panel can initialize a project directory, clone/pull code, generate a starter `deploy.sh`, create a starter systemd service for common backend templates, and generate Nginx reverse proxy config for a business domain. You should still review generated scripts before enabling automatic deployment.

## Requirements

- Linux server with root / sudo access
- Python 3.10 or newer; CI currently verifies Python 3.10-3.13
- `git` installed before running the installer
- systemd
- Optional: Docker, if your projects use Docker. The installer can help install it.
- Optional: Nginx / Certbot, which the installer can help set up

## Install

> [!WARNING]
> This repository is still preparing its first public test release. The default
> installer runs the Agent as `root`, and privilege separation, automatic failed
> upgrade rollback, restore, version rollback, and uninstall are not complete.
> Use only a dedicated, controlled Linux test server; do not treat the current
> branch as production-ready.

```bash
git clone https://gitee.com/XC1960/mini_deploy.git /root/mini_deploy
cd /root/mini_deploy
bash install.sh
```

After installation, open `http://<server-public-ip>:6868` and sign in with the
password you set during installation. No domain, Nginx, or certificate is required.
The Agent always listens on `0.0.0.0:6868`; the port cannot be overridden. The
installer updates old host/port settings and its existing Nginx upstreams on upgrade.
It opens TCP 6868 in active UFW/firewalld installations; allow this port in your
cloud security group as well. If public-IP detection fails, use the IP shown in
your cloud console, or set `DEPLOY_PUBLIC_IP` for the printed address.

Domain/HTTPS access remains optional: provide `DEPLOY_DOMAIN=deploy.example.com`
when installing to configure the additional Nginx entry point.

`scripts/bootstrap_server.sh` is an equivalent server-oriented entry point. Copy
`server.env.example` to `server.env` first if you want to preconfigure the
installer's location, domain, language, or Docker/Nginx choices.
Create that file as root (for example,
`sudo install -m 600 server.env.example server.env`) because the bootstrap
intentionally requires a root-owned, non-symlink file with mode `0600` or stricter.

Installer-managed paths must be absolute, use the supported characters shown in
`server.env.example`, point to dedicated directories, and must not overlap. On an
upgrade, reuse the existing `server.env`; the installer stops instead of silently
migrating a conflicting state, log, project-config, or service path.

An unmarked `APP_HOME`, systemd unit, or Nginx configuration that only resembles an
older mini_deploy installation is rejected by default. After verifying every path and
taking an independent server snapshot, set
`DEPLOY_ALLOW_LEGACY_INSTALL_ADOPTION=true` for that reviewed migration only. The
installer must atomically publish its original-state backup bundle before it adopts
the files and writes managed markers; return the option to `false` afterwards.

The installer asks for language first. Chinese is the default; enter `en` for English.

During an interactive install, it also asks twice for the administrator password. The
password hash and an independent Session Secret are written before the service or
public Nginx route is started. Browser-based administrator initialization is not
available.

If your projects use Docker Compose and Docker is not installed, the installer will ask whether to install Docker. The default is no.

For a non-interactive install, provide a root-readable password file and delete it
after the installer succeeds:

```bash
install -m 600 /dev/null /root/.mini-deploy-password
read -r -s -p "Administrator password: " MINI_DEPLOY_PASSWORD; echo
printf '%s\n' "$MINI_DEPLOY_PASSWORD" > /root/.mini-deploy-password
unset MINI_DEPLOY_PASSWORD
DEPLOY_UI_PASSWORD_FILE=/root/.mini-deploy-password \
  INSTALL_LANG=en SETUP_NGINX=false SETUP_HTTPS=no SETUP_DOCKER=no bash install.sh
rm -f /root/.mini-deploy-password
```

Change the explicit language and setup switches if the unattended environment should
also configure HTTPS or Docker.

HTTP works without HTTPS:

```text
http://<server-public-ip>:6868
```

Use HTTPS for public access when DNS is ready.

## First Project

In the web panel:

1. Add a project.
2. Fill the project key, repository URL and branch. Use “识别仓库” to detect common project templates, then confirm the port and startup command.
3. Use “预览部署文件” to review the script and optional systemd service, and check the confirmation box.
4. Click “Auto Initialize” to clone the code and create missing files. Existing scripts and service files are preserved; initialization does not start the application.
5. Click “Check Project”, install missing business runtimes, then enable the project and try a manual deployment. Failure details include suggested troubleshooting steps.
6. Configure the URL in your Git platform, then put the project Token in Gitee's
   password/Token field, GitLab's Secret token field, or GitHub's Secret field.
7. Enable the project.

New installations start with no projects. The files under `examples/` are reference
templates only; they are not imported into the active project configuration.

After that, every `git push` to the configured branch can trigger deployment.

For a business domain, first select and save the Nginx runtime under Nginx Certificates,
then check/save the project's upstream address and use Configure Domain. Docker mode
requires a local Docker Unix socket and directory bind mounts for `/etc/nginx/conf.d`
and `/etc/mini-deploy/certificates`; bridge networks need a reachable backend service
name or host address, not `127.0.0.1`. Existing containers are never recreated automatically.
See [the quickstart](docs/QUICKSTART.zh-CN.md) and [new-container example](examples/nginx.compose.yml).

## Important Boundary

`deploy.sh` is your project’s deployment recipe.

It usually does:

```text
git pull
install dependencies / build
restart service
health check
```

If your service is managed by systemd, the service file is still part of your business project setup. mini_deploy can provide examples and checks, but it cannot guarantee a correct service file for every possible application.

## Common Commands

The following examples assume the default layout. For a custom installation, copy
the parameterized diagnostic, administrator, and backup commands printed by
`install.sh`; those commands include the actual environment file, paths, and service
name.

```bash
systemctl status mini-deploy-agent
journalctl -u mini-deploy-agent -f
curl http://127.0.0.1:6868/health
bash /opt/mini_deploy/scripts/doctor.sh
python3 /opt/mini_deploy/agent.py admin set-password
python3 /opt/mini_deploy/agent.py admin reset-session
systemctl restart mini-deploy-agent
bash /opt/mini_deploy/scripts/backup-installation.sh
nano /var/lib/mini-deploy-agent/projects.json
```

For a custom installation backup, do not only replace the script path: copy the
complete `Manual safety backup` command printed by `install.sh`, including its
`APP_HOME`, `DATA_HOME`, `LOG_HOME`, service, Nginx, and backup-directory values.

The active project configuration is mutable data, not a release file. Its default
location is `/var/lib/mini-deploy-agent/projects.json`; configuration backups remain
under `/var/lib/mini-deploy-agent/backups`. During an upgrade, the installer copies
the old default `/opt/mini_deploy/projects.json` to the data directory only after a
verified original-state backup. During that first migration, if old and new files
both exist with different content, or `DEPLOY_PROJECTS_FILE` points outside the two
managed locations, the installer stops instead of guessing which copy contains the
current Tokens. Once the environment file already declares the data-directory copy
authoritative, a root-private legacy copy may remain in `APP_HOME` as stale recovery
evidence and is no longer used for conflict selection.

Prefer the web panel for project changes. If you edit the file directly, keep it
root-owned and `0600`, validate the exact file, and restart the Agent because it does
not watch external file changes:

```bash
chown root:root /var/lib/mini-deploy-agent/projects.json
chmod 600 /var/lib/mini-deploy-agent/projects.json
DEPLOY_PROJECTS_FILE=/var/lib/mini-deploy-agent/projects.json \
  python3 /opt/mini_deploy/agent.py validate-config
systemctl restart mini-deploy-agent
```

When `install.sh` recognizes an existing installation, it first atomically publishes
a root-private backup bundle under the configured backup directory (default:
`/var/backups/mini-deploy-agent`). Each `mini-deploy-<timestamp>-<suffix>/` bundle
contains `installation.tar.gz`, a SHA-256 `manifest`, and a `complete` marker; only a
bundle with all three files is complete. The backup command prints the
`<bundle>/installation.tar.gz` path; its sibling `manifest` and `complete` files are
part of the same backup and must be retained with it. Before changing an existing
installation, `install.sh` automatically runs the read-only `format=2` validator on
the new bundle and stops if validation fails. You can also run that validator as
root, passing either the bundle directory or its archive:

```bash
python3 /opt/mini_deploy/scripts/verify_backup.py \
  /var/backups/mini-deploy-agent/mini-deploy-YYYYmmddTHHMMSSZ-suffix
```

The validator checks the private bundle/control files, manifest and archive digest,
managed archive roots, hard-link resolution, and Agent release fingerprint without
extracting or modifying the archive. It counts symbolic links without following them
and rejects archive members that descend through a link, but it does not approve a
symbolic link's eventual restore target. A successful result is an integrity and
structure check; it does not restore anything or prove that a future restore will
succeed.

The archive contains the Agent program, project configuration, state, environment
file, old in-tree backups, and the systemd unit or Nginx configuration when those
control files exist. The
installer-managed log directory (default: `/var/log/mini_deploy`) remains in place
and is not selected as a separate archive input. Because the backup script archives
`APP_HOME` and `DATA_HOME` as complete trees, legacy or manually placed log files
inside either tree can still be included; treat every archive as sensitive.

The Agent, installer, and standalone backup use
`/run/mini-deploy-agent/maintenance.lock` to coordinate access. Accepted deploy and
project rollback jobs keep a shared lock from queue admission until completion. While an
installer or backup holds the exclusive maintenance lock, new WebHook/manual deploy
and rollback triggers return HTTP `503`; configuration, state, and audit writes wait
for maintenance to finish. systemd keeps the root-private runtime directory across
service restarts with `RuntimeDirectoryPreserve=yes`, and recreates it after a host
reboot. `/run/mini-deploy-agent` is still ephemeral runtime state and must not hold
persistent data.

Versioned release rollback and restore commands are still planned; check the roadmap
before using this pre-release project in production.

For Docker projects, `scripts/deploy.sample.sh` can optionally manage Docker
registry mirrors. Set `DOCKER_REGISTRY_MIRRORS` in the agent environment only
when you want the deploy script to update `/etc/docker/daemon.json`; it is
disabled by default.

## Security

- The default entry point is HTTP on TCP 6868. Restrict allowed sources where practical;
  optional domain/HTTPS access provides transport encryption.
- Use a strong UI password.
- Use a different webhook token for every project.
- Never append a WebHook Token to the URL. Query-string authentication is disabled
  by default because URLs leak through proxies, browser history, and logs.
- Forwarded client-IP headers are ignored by default. Set
  `DEPLOY_TRUST_LOOPBACK_PROXY_HEADERS=true` in the Agent environment, then restart
  the service, only when local reverse proxies are trusted and overwrite `X-Real-IP`; headers
  from non-loopback peers are never trusted. Nginx configuration generated by the
  installer satisfies those conditions and enables the option automatically.
- Keep `DEPLOY_UI_SESSION_SECRET` independent from every WebHook Secret.
- Review deploy scripts before enabling automatic deployment.
- The default installer currently runs the Agent as `root`. Read
  [SECURITY.md](SECURITY.md) and use a dedicated controlled test server until
  privilege separation is implemented.
