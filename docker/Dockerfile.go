# syntax=docker/dockerfile:1
# docker/Dockerfile.go
#
# Code Sandbox MCP — Go backend イメージ（base + Go ツールチェーン）
# 公開タグ: ghcr.io/<owner>/code-sandbox-mcp/sandbox:go
# 設計: docs/design-multilang-support.md §6
# ビルド:
#   docker build -f docker/Dockerfile.go \
#     --build-arg BASE_IMAGE=code-sandbox-mcp/sandbox:base \
#     -t code-sandbox-mcp/sandbox:go .

ARG BASE_IMAGE=code-sandbox-mcp/sandbox:base
FROM ${BASE_IMAGE}

# ── Go ツールチェーン ─────────────────────────────────────────────
USER root
ARG TARGETARCH
ARG GO_VERSION=1.26.4

RUN set -ex; \
    case "${TARGETARCH}" in \
      amd64) GO_ARCH="amd64" ;; \
      arm64) GO_ARCH="arm64" ;; \
      *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac; \
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" \
      | tar -xz -C /usr/local; \
    ln -s /usr/local/go/bin/go /usr/local/bin/go; \
    ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt

# GOPATH はユーザ home、GOCACHE は書込可能な /tmp 配下（read-only ルート対策）。
# buildvcs=false: クローン外のディレクトリでも go build が VCS スタンプで失敗しないように。
# GOMAXPROCS=1: pids 上限（100）超過による fork 枯渇を防ぐ（Issue #233）。
#   Go の並列コンパイルは多数の compile/vet/link を fork するため、
#   デフォルトの pids_limit=100 を容易に超過する。直列化で回避。
ENV GOPATH=/home/sandbox/go \
    GOCACHE=/tmp/.gocache \
    GOMAXPROCS=1 \
    GOFLAGS="-buildvcs=false -p=1"

USER sandbox
WORKDIR /home/sandbox

# ── ヘルスチェック (go イメージが保有するツール) ──────────────────
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD go version && rg --version && sg --version && semgrep --version || exit 1

CMD ["bash"]
