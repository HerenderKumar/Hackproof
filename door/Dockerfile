# syntax=docker/dockerfile:1
# The model door (Prune design, B1): one per run attempt, between the run's internal network and
# Prune. It holds the Prune key for the run; the team's app never does. Build from the repo root:
# `make door-image`.
FROM python:3.12-slim-bookworm AS door
RUN pip install --no-cache-dir "starlette==1.10.0" "uvicorn==0.54.0" "httpx==0.28.1" \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin door
WORKDIR /opt/hackproof
COPY door/__init__.py door/__main__.py door/app.py door/egress.py ./door/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOOR_VERSION=1
USER door
EXPOSE 8080 8081
ENTRYPOINT ["python", "-m", "door"]
