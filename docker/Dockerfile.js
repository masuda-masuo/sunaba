# syntax=docker/dockerfile:1
# docker/Dockerfile.js
#
# Code Sandbox MCP — JS/TS 開発ツールイメージ（base + js toolchain）
# 公開タグ: ghcr.io/<owner>/sunaba/sandbox:js
# 設計: docs/design_multilang_support.md §6 / Issue #588
# ビルド:
#   docker build -f docker/Dockerfile.js \
#     --build-arg BASE_IMAGE=sunaba/sandbox:base \
#     -t sunaba/sandbox:js .
#
# 位置づけ (#588): これは *明示 image= 用の lean イメージ*。既定で使われるのは
# 全部入りの sandbox:full（js もすでに含む）。ツール一式の定義は
# install-js-tools.sh に一本化し、Dockerfile.full と共有する（2 箇所に
# 別々のインストール手順を書くと必ずドリフトする、#584 の教訓）。
#
# node_modules/.bin 解決について: このイメージを明示指定しても、リポジトリの
# node_modules に eslint/tsc/jest が入っていればそちらが優先される
# (edit_verify 側の解決ロジック、#588 の核心)。ここで焼くのは
# pin していないリポジトリのための既定に過ぎない。

ARG BASE_IMAGE=sunaba/sandbox:base
FROM ${BASE_IMAGE}

# ── js 開発ツール (sandbox ユーザー) ────────────────────────────
# npm のグローバル書き込み先 (NPM_CONFIG_PREFIX) は base で通してある。
USER sandbox
WORKDIR /workspace
COPY --chown=sandbox:sandbox docker/install-js-tools.sh /tmp/install-js-tools.sh
RUN sh /tmp/install-js-tools.sh && rm /tmp/install-js-tools.sh

# ── ヘルスチェック (base 継承 + js 固有ツール) ────────────────────
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD rg --version && sg --version && node --version && npm --version \
   && eslint --version && tsc --version && jest --version || exit 1

CMD ["bash"]
