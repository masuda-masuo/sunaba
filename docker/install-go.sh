#!/bin/sh
# docker/install-go.sh
#
# 「Go の道具一式」の唯一の定義（#584）。
# Dockerfile.go と Dockerfile.full の *両方* がこのスクリプトを叩く。
#
# root で実行すること（/usr/local へ展開する）。
# 引数: $1 = TARGETARCH (amd64|arm64), $2 = Go のバージョン
#
# 注意: GOPATH / GOCACHE / GOFLAGS は go ツールしか読まないので Dockerfile 側の
# ENV に焼いてよい。GOMAXPROCS は *イメージ内の全 Go バイナリ* が読むため焼かない
# （#584。詳細は Dockerfile.go の ENV 節のコメント）。
set -eux

TARGETARCH="$1"
GO_VERSION="$2"

case "${TARGETARCH}" in
  amd64) GO_ARCH="amd64" ;;
  arm64) GO_ARCH="arm64" ;;
  *) echo "Unsupported arch: ${TARGETARCH}" >&2; exit 1 ;;
esac

curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz" \
  | tar -xz -C /usr/local
ln -s /usr/local/go/bin/go /usr/local/bin/go
ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt
