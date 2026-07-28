#!/bin/sh
# docker/install-rust.sh
#
# 「Rust の道具一式」の唯一の定義。
# Dockerfile.rust と Dockerfile.full の *両方* がこのスクリプトを叩く。
#
# root で実行すること。Go と違い、このスクリプトは 2 段構え:
#   1. apt で C コンパイラ/リンカを入れる（root 所有のシステムパッケージ）。
#      rusqlite の bundled feature のようなネイティブ拡張のリンクに要る --
#      base イメージには cc が無い。
#   2. rustup でツールチェーンを sandbox ユーザーの $HOME 配下
#      （RUSTUP_HOME / CARGO_HOME）に敷き、最後に chown する。
#      非 root の `rustup target add` を実行時に成立させるには、
#      RUSTUP_HOME/CARGO_HOME が sandbox 所有でなければならない。
#      root のまま rustup-init を走らせても、インストール先を env で
#      sandbox の $HOME 配下に向けておけば場所は変わる -- 最後の chown で
#      所有権だけ付け替える。
#
# 引数: $1 = Rust のバージョン（例: 1.97.1）
#
# 注意: RUSTUP_HOME / CARGO_HOME / PATH は cargo・rustup・rustc しか
# 読まないので Dockerfile 側の ENV に焼いてよい（GOPATH/GOCACHE と同じ理由、
# install-go.sh の注意書き参照）。GOMAXPROCS 相当の「イメージ内の全バイナリが
# 読む env var」は Rust 側には焼いていない -- RUSTFLAGS や RUST_LOG のような
# ビルド成果物や実行時の全バイナリに影響する変数はここでは設定しない。
set -eux

RUST_VERSION="$1"

# ── C コンパイラ / リンカ（rusqlite bundled 等のネイティブ拡張ビルドに必要）──
apt-get update
apt-get install -y --no-install-recommends build-essential
rm -rf /var/lib/apt/lists/*

# ── rustup + ツールチェーン（sandbox ユーザー所有の場所へ、image に焼く）──
# 公式インストーラ (sh.rustup.rs) はターゲットの arch/OS を自前で検出するため、
# install-go.sh と違って TARGETARCH を渡す必要が無い。
export RUSTUP_HOME=/home/sandbox/.rustup
export CARGO_HOME=/home/sandbox/.cargo

# パイプだと POSIX の set -e が curl の失敗を拾えない（最後のコマンド sh しか
# 見ない）ので、取得と実行を分ける。途中で切れたスクリプトを実行しない。
curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init.sh https://sh.rustup.rs
sh /tmp/rustup-init.sh -y --no-modify-path --profile minimal --default-toolchain "${RUST_VERSION}"
rm /tmp/rustup-init.sh

# clippy と rustfmt は minimal profile に含まれないので明示的に足す
# （cargo clippy / cargo fmt --check が要件のため）。
"${CARGO_HOME}/bin/rustup" component add clippy rustfmt

# クロスコンパイル用ターゲットの実行時追加 (`rustup target add <triple>`) が
# 非 root で通るように、sandbox ユーザーへ所有権を渡す。
chown -R sandbox:sandbox "${RUSTUP_HOME}" "${CARGO_HOME}"
