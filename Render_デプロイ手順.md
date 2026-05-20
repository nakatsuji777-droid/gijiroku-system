# 議事録システム — Render デプロイ手順

## 概要

Render（sakura-app と同じサービス）にデプロイして、  
**ブラウザから常時アクセスできる** Web アプリとして動かします。

---

## 事前準備（1回だけ）

### 必要なもの
- [ ] GitHub アカウント（無料）
- [ ] Render アカウント（無料）→ https://render.com
- [ ] Google AI Studio の API キー → https://aistudio.google.com/apikey

---

## ステップ1：GitHub にプッシュ

議事録システムフォルダで以下を実行（コマンドプロンプト or PowerShell）：

```bash
cd "C:\Users\NIKKEN-PC20\Desktop\開発中システム\議事録システム"

# 初回のみ：リモートリポジトリを GitHub に作成してから
git remote add origin https://github.com/あなたのユーザー名/gijiroku-system.git

# プッシュ
git add .
git commit -m "Render デプロイ設定を追加"
git push -u origin main
```

> ⚠️ `.env` ファイルは `.gitignore` で除外済みのため、APIキーは GitHub に上がりません。安全です。

---

## ステップ2：Render でサービスを作成

1. https://dashboard.render.com にログイン
2. 右上の **「New +」→「Web Service」** をクリック
3. **「Connect a repository」** → GitHub の `gijiroku-system` を選択
4. 以下の設定を確認（`render.yaml` から自動読み込みされます）：

| 項目 | 設定値 |
|---|---|
| Name | gijiroku-system |
| Runtime | Python |
| Build Command | `apt-get install -y ffmpeg && pip install -r requirements.txt` |
| Start Command | `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true` |
| Plan | **Free**（無料） |

5. **「Create Web Service」** をクリック

---

## ステップ3：環境変数を設定

Render ダッシュボード → 作成したサービス → **「Environment」タブ** で以下を追加：

| キー名 | 値 | 説明 |
|---|---|---|
| `GOOGLE_API_KEY` | `AIza...` | Gemini API キー（必須） |
| `APP_PASSWORD` | 任意のパスワード | ログイン用（例：nikken2026） |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Claude APIキー（任意） |

> ⚠️ `APP_PASSWORD` を設定しないと、URL を知っている人は誰でもアクセスできます。必ず設定してください。

---

## ステップ4：デプロイ完了を確認

1. Render ダッシュボードで **「Deploy」** ログが流れる（3〜5分かかります）
2. `Build successful` と表示されたら完了
3. 上部の URL（`https://gijiroku-system.onrender.com`）にアクセス
4. パスワード入力画面が出たら成功！

---

## 今後の更新方法

テンプレート編集・機能追加など変更した場合：

```bash
git add .
git commit -m "テンプレートを更新"
git push
```

GitHub にプッシュすると **Render が自動でデプロイ**します（2〜3分）。

---

## 無料プランの制限と対処法

| 制限 | 内容 | 対処法 |
|---|---|---|
| **スリープ** | 15分間アクセスがないと休止 | アクセスすると30秒で自動起動 |
| **ストレージ** | 再起動で過去の議事録が消える | 生成直後にダウンロードする習慣を |
| **月750時間** | 無料枠 | 1サービスなら問題なし |

> 💡 **有料プラン（$7/月）** にすると、スリープなし・ファイル永続化が可能です。

---

## トラブルシューティング

### ビルドエラーが出る場合
- Render ダッシュボードの「Logs」タブでエラー内容を確認
- `requirements.txt` に全パッケージが記載されているか確認

### パスワード画面が出ない場合
- Environment で `APP_PASSWORD` が設定されているか確認
- 設定後は「Manual Deploy」→「Deploy latest commit」で再デプロイ

### 議事録が生成されない場合
- `GOOGLE_API_KEY` が正しく設定されているか確認
- Environment タブの値をコピーし直す
