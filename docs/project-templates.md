# Project Deploy Script Templates

VibePilot Deploy runs shell scripts. These are starting points; copy one into your project and adjust it.

## Node / Vite / Next.js

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

cd /srv/node-api
git fetch origin main
git checkout main
git pull --ff-only origin main
pnpm install --frozen-lockfile
pnpm build
pm2 restart node-api
curl -fsS https://api.example.com/health
```

## Java Spring Boot

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

cd /srv/spring-api
git fetch origin main
git checkout main
git pull --ff-only origin main
mvn clean package -DskipTests
systemctl restart spring-api
curl -fsS https://spring.example.com/actuator/health
```

## Go

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

cd /srv/go-service
git fetch origin main
git checkout main
git pull --ff-only origin main
go build -o app ./cmd/server
systemctl restart go-service
curl -fsS https://go.example.com/health
```

## Docker Compose

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

cd /srv/my-compose-app
git fetch origin main
git checkout main
git pull --ff-only origin main
docker compose up -d --build
docker compose ps
curl -fsS https://app.example.com/health
```

