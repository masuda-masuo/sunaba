#!/bin/sh
# docker/install-browser-deps.sh
#
# 「headless Chromium の実行時ライブラリ一式」の定義（#923）。
# Dockerfile.full だけがこのスクリプトを叩く。
#
# ブラウザ本体は焼かない。ブラウザのビルドは利用側の `playwright` パッケージの
# バージョンと対になっており、実行時に `playwright install` が取得する
# （ビルド時に焼き込んでも、利用側の playwright のバージョンが変われば
# 対応するバイナリが変わって無意味になる）。ここでは「どのバージョンの
# chromium-headless-shell でも起動に必要な」システムライブラリだけを入れる。
#
# root で実行すること（apt でシステムパッケージを入れる。sandbox ユーザーは
# 実行時に apt を叩けないので、ビルド時に root で焼く）。
#
# パッケージ一覧の出典: Playwright 1.63.0 が持つ Debian 12 (bookworm) x64 の
# 依存表 nativeDeps["debian12-x64"].chromium。
#   <site-packages>/playwright/driver/package/lib/server/registry/nativeDeps.js
#   （1.63.0 では同値が coreBundle.js に同梱されている）
# tools 一覧（xvfb やフォント類）は Chromium の *起動* に不要なので含めない
# （headless は X を必要とせず、フォントは描画品質の問題であって起動失敗の原因
# ではない）。issue #923 で ldd が実測した not found 20 soname は全て
# chromium 一覧のパッケージが提供する。
set -eux

apt-get update
apt-get install -y --no-install-recommends \
  libasound2 \
  libatk-bridge2.0-0 \
  libatk1.0-0 \
  libatspi2.0-0 \
  libcairo2 \
  libcups2 \
  libdbus-1-3 \
  libdrm2 \
  libgbm1 \
  libglib2.0-0 \
  libnspr4 \
  libnss3 \
  libpango-1.0-0 \
  libx11-6 \
  libxcb1 \
  libxcomposite1 \
  libxdamage1 \
  libxext6 \
  libxfixes3 \
  libxkbcommon0 \
  libxrandr2
rm -rf /var/lib/apt/lists/*

# ── 自己検査: 実測で欠けていた soname が全て解決できるか ──────────
# 入れた「はず」ではなく、実際に ldconfig が解決できることをビルド時に確認する。
# 一つでも解決できなければここで落とす（黙って欠けたままの image を出さない）。
# 一覧は issue #923 の ldd 測定（not found 20 soname）そのもの。
missing=0
for soname in \
  libglib-2.0.so.0 \
  libgobject-2.0.so.0 \
  libnspr4.so \
  libnss3.so \
  libnssutil3.so \
  libgio-2.0.so.0 \
  libatk-1.0.so.0 \
  libatk-bridge-2.0.so.0 \
  libdbus-1.so.3 \
  libX11.so.6 \
  libXcomposite.so.1 \
  libXdamage.so.1 \
  libXext.so.6 \
  libXfixes.so.3 \
  libXrandr.so.2 \
  libgbm.so.1 \
  libxcb.so.1 \
  libxkbcommon.so.0 \
  libasound.so.2 \
  libatspi.so.0
do
  if ! ldconfig -p | grep -qF "${soname} "; then
    echo "install-browser-deps.sh: missing soname: ${soname}" >&2
    missing=1
  fi
done
if [ "${missing}" -ne 0 ]; then
  echo "install-browser-deps.sh: chromium runtime libraries are incomplete" >&2
  exit 1
fi
