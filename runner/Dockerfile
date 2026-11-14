# syntax=docker/dockerfile:1
# The test runner. It shares a per-run internal network with the team's app and nothing else:
# no secrets, no Docker socket, no internet. Build from the repo root: `make runner-image`.
FROM mcr.microsoft.com/playwright/python:v1.65.0-noble

RUN pip install --no-cache-dir --break-system-packages \
      "playwright==1.65.0" \
      "httpx>=0.27,<1" \
      "jsonpath-ng>=1.7,<2" \
      "pydantic>=2.9,<3"

WORKDIR /opt/hackproof
# Only the modules the runner needs: no settings, no database code. The pure `core` modules
# are shared with the worker (spec schema, templates, assertions); UI tests arrive in M11.
COPY core/__init__.py core/hashing.py core/specs.py core/templating.py core/assertions.py ./core/
COPY runner/__init__.py runner/__main__.py runner/main.py runner/startup.py runner/api_tests.py ./runner/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RUNNER_VERSION=1
# The image's own non-root user (section 12.3).
USER pwuser
ENTRYPOINT ["python", "-m", "runner"]
