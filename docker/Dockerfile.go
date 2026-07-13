# syntax=docker/dockerfile:1
# docker/Dockerfile.go
#
# Code Sandbox MCP — Go backend イメージ（base + Go ツールチェーン）
# 公開タグ: ghcr.io/<owner>/sunaba/sandbox:go
# 設計: docs/design_multilang_support.md §6
# ビルド:
#   docker build -f docker/Dockerfile.go \
#     --build-arg BASE_IMAGE=sunaba/sandbox:base \
#     -t sunaba/sandbox:go .
#
# 位置づけ (#584): これは *明示 image= 用の lean イメージ*。既定で使われるのは
# 全部入りの sandbox:full。ツールチェーンの定義は install-go.sh に一本化し、
# Dockerfile.full と共有する。

ARG BASE_IMAGE=sunaba/sandbox:base
FROM ${BASE_IMAGE}

# ── Go ツールチェーン ─────────────────────────────────────────────
USER root
ARG TARGETARCH
ARG GO_VERSION=1.26.4
COPY docker/install-go.sh /tmp/install-go.sh
RUN sh /tmp/install-go.sh "${TARGETARCH}" "${GO_VERSION}" && rm /tmp/install-go.sh

# GOPATH はユーザ home、GOCACHE は書込可能な /tmp 配下（read-only ルート対策）。
# buildvcs=false: クローン外のディレクトリでも go build が VCS スタンプで失敗しないように。
#
# GOMAXPROCS は *ここに焼かない*（#584）。pids 上限（100）超過による fork 枯渇を
# 防ぐ目的（#233）だが、GOMAXPROCS はイメージ内の **全ての Go バイナリ** が読む
# ため、gh（Go 製）まで 1 スレッドに絞られる。全部入りイメージ（sandbox:full）で
# は Go 以外の作業が主になるので、この漏れは看過できない。fork 枯渇対策は
# go build / go test の呼び出しに属する性質なので、edit_verify が exec 時の env
# として渡す（_GO_ENV）。
ENV GOPATH=/home/sandbox/go \
    GOCACHE=/tmp/.gocache \
    GOFLAGS="-buildvcs=false -p=1"

USER sandbox
WORKDIR /home/sandbox

# ── ヘルスチェック (base 継承 + go 固有ツール) ──────────────────
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD go version && rg --version && sg --version && node --version || exit 1

CMD ["bash"]
