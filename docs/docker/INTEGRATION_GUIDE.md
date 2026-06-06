# code-sandbox-mcp 統合セットアップガイド（Windows + WSL）

Claude Desktop から code-sandbox-mcp サーバーに、`mcp-launcher` でセキュアにアクセスします。
WSL 上のバイナリを直接実行し、GitHub App の短期トークンを自動取得して `GITHUB_TOKEN` として注入します。

---

## 📦 前提条件

- WSL2 がインストール済みで起動している
- WSL 上に code-sandbox-mcp がインストール済み（`pip install`）
- Docker Desktop が WSL2 バックエンドで起動している
- [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher/releases) をダウンロード済み
- Claude Desktop がインストール済み
- GitHub App を作成済み（App ID・インストール ID・秘密鍵を取得済み）

---

## 🚀 セットアップ

### 1️⃣ code-sandbox-mcp を WSL にインストール

WSL ターミナルで:

```bash
pip install git+https://github.com/masuda-masuo/code-sandbox-mcp
which code-sandbox-mcp-launcher  # パスを控えておく
# 例: /home/masuda/.local/bin/code-sandbox-mcp-launcher
```

### 2️⃣ mcp-launcher を配置

Releases ページからダウンロードした `mcp-launcher.exe` を配置します。

```
C:\work\mcp\mcp-launcher.exe
```

### 3️⃣ GitHub App の認証情報を Credential Manager に登録

```powershell
mcp-launcher register github APP_ID 123456
mcp-launcher register github INSTALLATION_ID 789012
mcp-launcher register github PRIVATE_KEY "-----BEGIN RSA PRIVATE KEY-----..."
mcp-launcher register github GITHUB_PERSONAL_ACCESS_TOKEN ""
```

> **注意**: `GITHUB_PERSONAL_ACCESS_TOKEN` は空文字で登録しておきます（`token_source` が自動で上書きします）。

### 4️⃣ launcher.json を作成

```powershell
mkdir %USERPROFILE%\.mcp-launcher
notepad %USERPROFILE%\.mcp-launcher\launcher.json
```

内容:

```json
{
  "code-sandbox-mcp": {
    "command": "wsl.exe",
    "args": [
      "--exec", "/home/masuda/.local/bin/code-sandbox-mcp-launcher",
      "--update-spec", "git+https://github.com/masuda-masuo/code-sandbox-mcp",
      "--pass-through-env", "GITHUB_TOKEN",
      "--terminal", "dummy"
    ],
    "env_keys": {
      "GITHUB_TOKEN": "mcp-launcher/github/GITHUB_PERSONAL_ACCESS_TOKEN"
    },
    "token_source": {
      "type": "github_app",
      "app_id_key": "mcp-launcher/github/APP_ID",
      "private_key_key": "mcp-launcher/github/PRIVATE_KEY",
      "installation_id_key": "mcp-launcher/github/INSTALLATION_ID",
      "target_env_key": "GITHUB_TOKEN",
      "refresh_before_seconds": 600
    },
    "check_interval_seconds": 60
  }
}
```

#### 各パラメータの説明

| パラメータ | 説明 |
|---|---|
| `command: wsl.exe` | WSL 経由でコマンドを実行 |
| `--exec /home/masuda/.local/bin/code-sandbox-mcp-launcher` | WSL 上のランチャーバイナリのパス（`which` で確認したパス） |
| `--update-spec` | `sandbox_update_start()` で使う pip インストール元 |
| `--pass-through-env GITHUB_TOKEN` | コンテナに渡す環境変数（カンマ区切りで複数指定可） |
| `--terminal dummy` | ターミナルウィンドウ機能を無効化（WSL 上では不要） |
| `env_keys.GITHUB_TOKEN` | Credential Manager からトークンを読み込むキー |
| `token_source` | GitHub App から短期トークンを自動取得する設定 |
| `refresh_before_seconds: 600` | トークン期限の 10 分前に自動更新 |

### 5️⃣ Claude Desktop を設定

```powershell
notepad %APPDATA%\Claude\claude_desktop_config.json
```

内容（既存の `mcpServers` に追加）:

```json
{
  "mcpServers": {
    "code-sandbox-mcp": {
      "command": "C:\\work\\mcp\\mcp-launcher.exe",
      "args": ["code-sandbox-mcp"]
    }
  }
}
```

> **注意**: Claude Desktop は PATH を継承しないため、`mcp-launcher.exe` の絶対パスを指定してください。

### 6️⃣ Claude Desktop を再起動

タスクトレイのアイコンを右クリック → Quit して再度起動。

### 7️⃣ 動作確認

Claude Desktop チャットで:

```
以下の Python コードを実行してください:

print("Hello from code-sandbox-mcp!")
import platform
print(f"Python: {platform.python_version()}")
```

コンテナ内で実行された結果が返ればOK。

---

## 🔧 トラブルシューティング

| 問題 | 解決方法 |
|---|---|
| `mcp-launcher が見つからない` | `claude_desktop_config.json` の絶対パスを確認 |
| `token error` | App ID・Installation ID・秘密鍵の登録を確認 |
| `wsl.exe: command not found` | WSL2 が有効になっているか確認 |
| `code-sandbox-mcp-launcher not found` | WSL で `which code-sandbox-mcp-launcher` を実行してパスを確認 |
| Docker が使えない | Docker Desktop が WSL2 バックエンドで起動しているか確認 |
| Claude Desktop が接続できない | タスクトレイから完全に Quit して再起動 |

**詳細ログの確認:**

```powershell
# launcher.json の構文チェック
type %USERPROFILE%\.mcp-launcher\launcher.json

# WSL 上でバイナリが存在するか確認
wsl.exe which code-sandbox-mcp-launcher

# 手動でバイナリ起動テスト
wsl.exe --exec /home/masuda/.local/bin/code-sandbox-mcp-launcher --help
```

---

## ✅ チェックリスト

- [ ] WSL2 が有効で起動している
- [ ] WSL 上で `pip install git+https://github.com/masuda-masuo/code-sandbox-mcp` 済み
- [ ] Docker Desktop が WSL2 バックエンドで起動している
- [ ] `mcp-launcher register github APP_ID` で App ID を登録した
- [ ] `mcp-launcher register github INSTALLATION_ID` で Installation ID を登録した
- [ ] `mcp-launcher register github PRIVATE_KEY` で秘密鍵を登録した
- [ ] `launcher.json` を作成した（`wsl.exe` 経由の設定）
- [ ] `launcher.json` の `--exec` パスが WSL 上の実際のパスと一致している
- [ ] `claude_desktop_config.json` に絶対パスで `mcp-launcher.exe` を指定した
- [ ] Claude Desktop を完全に再起動した
- [ ] チャットで Python コードを実行して動作確認した

---

## 📚 参考資料

- [mcp-launcher](https://github.com/masuda-masuo/mcp-launcher)
- [code-sandbox-mcp](https://github.com/masuda-masuo/code-sandbox-mcp)
- [GitHub App setup](https://github.com/masuda-masuo/mcp-launcher/blob/main/docs/setup/github-app-setup.md)
- [MCP セキュリティベストプラクティス](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
