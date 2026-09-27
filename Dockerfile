# AtlasOS container image.
#
# Holds code + Python deps + the kubectl/aws/helm binaries the skills shell
# out to. Holds NO credentials: ~/.aws, ~/.kube/config and .env are supplied
# at run time (see docker-compose.yml), and .dockerignore keeps them out of
# the build context so they can never be baked into a layer.
#
# Tool versions match the operator's workstation so container and local runs
# behave the same; override with --build-arg to upgrade.

ARG PYTHON_VERSION=3.11

# ---------------------------------------------------------------------------
# Stage 1: resolve and install Python dependencies from the lock file.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, project second: editing source code then reuses the
# (slow) dependency layer instead of reinstalling everything.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

# ---------------------------------------------------------------------------
# Stage 2: runtime. No uv, no build cache — just the venv, code and CLIs.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG KUBECTL_VERSION=v1.36.1
ARG HELM_VERSION=v4.3.0
ARG AWSCLI_VERSION=2.34.63
ARG TARGETARCH

# kubectl and helm are checked against their published SHA-256; the AWS CLI
# zip comes over HTTPS from AWS's own domain.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl unzip; \
    arch="${TARGETARCH:-amd64}"; \
    case "$arch" in \
      amd64) aws_arch=x86_64 ;; \
      arm64) aws_arch=aarch64 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    curl -fsSLo kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    echo "$(curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl.sha256")  kubectl" | sha256sum -c -; \
    install -m 0755 kubectl /usr/local/bin/kubectl; \
    curl -fsSLo helm.tgz "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz"; \
    echo "$(curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz.sha256sum" | cut -d' ' -f1)  helm.tgz" | sha256sum -c -; \
    tar -xzf helm.tgz; \
    install -m 0755 "linux-${arch}/helm" /usr/local/bin/helm; \
    curl -fsSLo awscli.zip "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}-${AWSCLI_VERSION}.zip"; \
    unzip -q awscli.zip; \
    ./aws/install; \
    apt-get purge -y --auto-remove unzip; \
    rm -rf /tmp/* /var/lib/apt/lists/*; \
    kubectl version --client; helm version --short; aws --version

# Non-root: a compromised tool call must not own the container. The ~/.kube
# directory is pre-created so kubectl can write its discovery cache next to
# a read-only mounted config file.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin atlas \
 && mkdir -p /home/atlas/.kube /home/atlas/.aws /app/data/vault /app/chroma_db /app/backups \
 && chown -R atlas:atlas /home/atlas /app

WORKDIR /app
COPY --from=builder --chown=atlas:atlas /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

USER atlas
EXPOSE 8000

# /mcp answers 401 without a token, which still proves the server is up —
# the probe must not need the secret.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${MCP_PORT}/mcp") \
      && [ "$code" = "401" ]

# Default: the remote MCP server (read-only unless MCP_READONLY=0). Any CLI
# command also works:  docker compose run --rm atlasos agent k8s nodes
CMD ["python", "-m", "agent.mcp_server"]
