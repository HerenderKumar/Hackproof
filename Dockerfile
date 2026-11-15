# syntax=docker/dockerfile:1
# One image definition for the two Python services. Targets: `api`, `worker`.
# Dependency and tool layers come before the source, so a code change rebuilds only the last COPY.

FROM python:3.12-slim-bookworm AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # Bare fixture repos and extracted archives are bind-mounted and owned by the host user.
    && git config --system --add safe.directory '*'
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# The application source, shared by both targets.
FROM scratch AS source
COPY alembic.ini /app/
COPY core /app/core
COPY api /app/api
COPY worker /app/worker
COPY runner /app/runner
COPY prompts /app/prompts
COPY scripts /app/scripts
# The kickoff scanner: the API serves it to participants as one file (M9).
COPY scanner /app/scanner

FROM deps AS api
COPY --from=source /app /app
EXPOSE 8000
CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM deps AS worker
ARG TARGETARCH
# Pinned in D-026. The checksums are from the release's checksums.txt.
ARG RAILPACK_VERSION=0.43.0
ARG RAILPACK_SHA256_ARM64=732ece14eb4f4676a43ebba19431f954edf7b130acb76159ef0f2ff86ac42ac9
ARG RAILPACK_SHA256_AMD64=248a8f60f36854aabed6124113631078a802de3461660855fc4de9c26065c1fb
ARG GRAPHIFY_VERSION=0.9.22
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ripgrep xz-utils \
    && rm -rf /var/lib/apt/lists/*
# Docker CLI (the worker drives the host daemon through the mounted socket) and buildx for `docker build`.
COPY --from=docker:29.9.3-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:29.9.3-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) arch=arm64; sum="${RAILPACK_SHA256_ARM64}" ;; \
      amd64) arch=x86_64; sum="${RAILPACK_SHA256_AMD64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/railpack.tgz \
      "https://github.com/railwayapp/railpack/releases/download/v${RAILPACK_VERSION}/railpack-v${RAILPACK_VERSION}-${arch}-unknown-linux-musl.tar.gz"; \
    echo "${sum}  /tmp/railpack.tgz" | sha256sum -c -; \
    tar -xzf /tmp/railpack.tgz -C /usr/local/bin railpack; \
    rm /tmp/railpack.tgz; \
    railpack --version
# Railpack runs mise to resolve language versions. Without it, Railpack silently downloads a
# 58 MB archive from GitHub on first use in every new container (D-042). Bake the exact version
# this Railpack expects (`railpack --verbose prepare` logs it) at the path Railpack checks.
# A Railpack upgrade must update MISE_VERSION; tests/integration/test_railpack.py catches a miss.
# Fetched from mise's own release mirror; the checksums are from the release's SHASUMS256.txt.
ARG MISE_VERSION=2026.10.14
ARG MISE_SHA256_ARM64=2b661edd0ba55d4f9fac940d0e108c79893bb30642ac5b532a677b49fa7cb8b3
ARG MISE_SHA256_AMD64=d2ce450af6abc290d153723f790546d836428aa60f8b61d4a3d2c1dacde5d6c5
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) arch=arm64; sum="${MISE_SHA256_ARM64}" ;; \
      amd64) arch=x64; sum="${MISE_SHA256_AMD64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 -o /tmp/mise.tar.xz \
      "https://mise.jdx.dev/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-${arch}-musl.tar.xz"; \
    echo "${sum}  /tmp/mise.tar.xz" | sha256sum -c -; \
    mkdir -p /tmp/railpack/mise; \
    tar -xJf /tmp/mise.tar.xz -C /tmp/railpack/mise --strip-components=2 mise/bin/mise; \
    mv /tmp/railpack/mise/mise "/tmp/railpack/mise/mise-${MISE_VERSION}"; \
    rm /tmp/mise.tar.xz; \
    "/tmp/railpack/mise/mise-${MISE_VERSION}" --version
RUN UV_TOOL_BIN_DIR=/usr/local/bin UV_TOOL_DIR=/opt/uv-tools uv tool install "graphifyy==${GRAPHIFY_VERSION}" \
    && graphify --help >/dev/null
# The worker image doubles as the pipeline test environment (integration tests, fixture suite).
RUN uv sync --frozen
ENV RAILPACK_VERSION=${RAILPACK_VERSION}
COPY --from=source /app /app
CMD ["python", "-m", "worker"]
