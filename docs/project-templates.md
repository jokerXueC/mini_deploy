# Deploy Script Templates

VibePilot runs shell scripts. These templates are starting points, not guaranteed final scripts.

Before enabling WebHook deployment, make sure your script can run successfully on the server.

## Basic Shape

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

cd /srv/your-project
git fetch origin main
git checkout main
git pull --ff-only origin main

# build or install dependencies here

# restart your service here

# health check here
curl -fsS http://127.0.0.1:8000/health
```

## Python / FastAPI

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
systemctl restart fastapi-demo
curl -fsS http://127.0.0.1:8001/health
```

The `fastapi-demo.service` file still needs to match your actual app entry, such as `main:app` or `app.main:app`.

## Go

```bash
go mod download
go build -o app ./cmd/server
systemctl restart go-api
curl -fsS http://127.0.0.1:8002/health
```

Confirm the build path matches your repository.

## Java / Spring Boot

```bash
mvn clean package -DskipTests
cp target/*.jar app.jar
systemctl restart java-api
curl -fsS http://127.0.0.1:8003/actuator/health
```

If your project uses Gradle or has multiple jars, adjust the build and copy commands.

## Docker Compose

```bash
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8000/health
```
