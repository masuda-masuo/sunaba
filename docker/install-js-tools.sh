#!/bin/sh
# docker/install-js-tools.sh
#
# 「js の道具一式」の唯一の定義（#588）。
#
# Dockerfile.js と Dockerfile.full の *両方* がこのスクリプトを叩く。2 箇所に
# 別々のインストール手順があると必ずドリフトする -- #584 はまさにそれ
# （pytest-json-report が python イメージにしか焼かれておらず、他イメージから
# 起動したコンテナは初回 verify が必ず落ちていた）。
#
# sandbox ユーザーで実行すること。npm のグローバルインストール先は
# Dockerfile.base の NPM_CONFIG_PREFIX（/home/sandbox/.npm-global）で
# 非 root 書き込み可能な場所へ通してある。
#
# ── これはあくまで「pin していないリポジトリのための既定」 ──────────
# ここで焼くのはグローバルフォールバックであり、主役ではない。
# edit_verify 側の解決ロジック（node_modules/.bin/{eslint,tsc,jest} を
# 優先し、無ければここで焼いたグローバルへフォールバック）が本体。
# バージョンをここで固定しても、リポジトリの package.json が別バージョンを
# pin していれば node_modules 解決が勝つ。逆に固定しなければ、ビルドの
# たびに「今のグローバル既定は何か」が変わりうる -- それ自体は許容する
# （python 側の install-python-tools.sh も ruff/pyright/pytest を無 pin で
# 入れている。倣う）。
set -eux

npm install -g \
  eslint \
  typescript \
  jest

eslint --version
tsc --version
jest --version
