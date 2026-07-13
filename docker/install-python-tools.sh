#!/bin/sh
# docker/install-python-tools.sh
#
# 「Python の道具一式」の唯一の定義（#584）。
#
# Dockerfile.python と Dockerfile.full の *両方* がこのスクリプトを叩く。
# 2 箇所に別々のインストール手順があると必ずドリフトする — #584 はまさにそれで、
# verify は pytest を --json-report 付きで起動するのに、そのプラグインは python
# イメージにしか焼かれておらず、別イメージで起動したコンテナは初回 verify が必ず
# 落ちていた。
#
# イメージの契約: **verify が dispatch しうるツールは全て存在すること。**
# 各 Dockerfile の HEALTHCHECK がこれを実行時に表明する。
#
# sandbox ユーザで実行すること。VIRTUAL_ENV は Dockerfile.base の ENV で通って
# いるため、uv はフラグなしで /home/sandbox/.venv へ入れる（#380）。
set -eux

uv pip install \
    ruff \
    pyright \
    pytest \
    pytest-json-report \
    pytest-xdist

# pytest-xdist は「焼くが既定では使わない」。verify は直列で pytest を回す。
#   実測 (sunaba 自身のスイート, 16 コア): 直列 32s → -n 4 で 16s。
#   だが -n 8 では *実行のたびに別のテストが落ちる*（単体では全て通る）。
#   スイートにテスト間の結合が残っており、直列の実行順序がたまたまそれを満た
#   しているだけ。ここで並列を既定にすると、#584 が消そうとしている「嘘の失敗」
#   を別の形で作り込む。
# 使いたいときは既存の脱出ハッチで明示的に:
#   verify_in_container(pytest_args="-n 4")
# 既定 ON はスイートを並列安全にしてから（別 issue）。

# pyright は PyPI ラッパーで、実体は初回起動時に取得する node アプリ。
# コンテナはネットワーク off が既定なので、ビルド時（ネットワーク可）に
# 一度起動して本体をキャッシュへ焼いておく。
pyright --version
