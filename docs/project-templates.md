# Legacy Deployment Templates

Deployment templates are retired. mini_deploy is now a server operations and
request monitoring panel; it no longer guides repository onboarding, deployment
scripts, WebHooks or automatic business runtime generation.

Existing business files, configurations, scripts, services and containers are
retained on upgrade. Deployment execution has been removed, old deployment APIs
return HTTP 410, and old queues do not resume after Agent startup or restart.

Repository deployment templates and adapters have been removed. Existing server
scripts are retained; business releases remain outside the panel.

Use the [quickstart](QUICKSTART.zh-CN.md) for server monitoring, Docker management,
Nginx sites/certificates and request collection. Sites are independent of Git:
enter a name, domain and port, then configure the backend address in the Nginx
entry settings. Health checks and the advanced site key are optional.
