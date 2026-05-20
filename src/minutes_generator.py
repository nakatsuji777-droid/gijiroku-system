"""議事録生成モジュール（Ver2.1: 段階間バリデーション + フォールバック改善 + プロンプト強化）

改善内容:
- Step1 出力が文字起こしの 50% 未満なら強化プロンプトで再抽出
- Step2 出力が Step1 の 65% 未満なら強化プロンプトで再整形
- フォールバックモデル使用時は thinking_budget を自動スケール
- 抽出プロンプト冒頭に文字数最低基準を明示
- 整形プロンプトに圧縮禁止の数値ルールを追加
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

from .template_loader import fill_template

load_dotenv()


# ===== ロギング =====
_LOG_PATH = Path(__file__).parent.parent / "minutes_generator_debug.log"
logger = logging.getLogger("minutes_generator")
if not logger.handlers:
    handler = logging.FileHandler(str(_LOG_PATH), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ===========================================================================
# ステップ間バリデーション閾値
# ===========================================================================

# Step1（構造化抽出）: transcript の何%以上の文字数が必要か
_STEP1_MIN_RATIO = 0.45

# Step2（整形）: Step1 出力の何%以上が必要か
_STEP2_MIN_RATIO = 0.65


# ===========================================================================
# 品質プリセット
# ===========================================================================

QUALITY_PRESETS: dict[str, dict] = {
    "fast": {
        "label": "⚡ 高速",
        "model": "models/gemini-2.5-flash",
        "two_stage": False,
        "thinking_budget": 0,
        "temperature": 0.2,
        "max_output_tokens": 32768,
        "validate_retries": 0,
    },
    "balanced": {
        "label": "⚖️ バランス（推奨）",
        "model": "models/gemini-2.5-flash",
        "two_stage": True,
        "thinking_budget": 4096,
        "temperature": 0.3,
        "max_output_tokens": 65536,
        "validate_retries": 1,
    },
    "best": {
        "label": "🎯 最高品質（Pro）",
        "model": "models/gemini-2.5-pro",
        "two_stage": True,
        "thinking_budget": 16384,
        "temperature": 0.3,
        "max_output_tokens": 65536,
        "validate_retries": 2,
    },
}

DEFAULT_QUALITY = "balanced"


# ===========================================================================
# 既定の固有名詞辞書
# ===========================================================================

DEFAULT_GLOSSARY: list[tuple[str, list[str]]] = [
    ("ニッケン建設", ["日建建設", "日建", "ニッケンケンセツ", "ニッケンケン設", "二建建設"]),
    ("中辻良太", ["中津井", "中辻 良太", "ナカツジリョウタ", "中津井良太", "中辻さん"]),
    ("横澤代表", ["横沢代表", "ヨコザワ代表"]),
    ("倉方取締役", ["クラカタ取締役", "倉形取締役"]),
    ("ユーミーマンション", ["弓マンション", "有民マンション", "有マンション", "ユーミマンション"]),
    ("勝瀬保育園", ["活性保育園", "活法育会", "活保保育園", "手保育園", "カツセ保育園"]),
    ("日進町", ["新町", "ニッシンチョウ"]),
    ("桜ヶ丘", ["桜小学中央", "サクラガオカ"]),
    ("きらぼし銀行", ["キラ星銀行"]),
    ("船井総研", ["船内さん", "船井さん"]),
    ("川崎信用金庫", ["川しさん"]),
    ("長嶺工業", ["長め工業所", "ナガミネ工業"]),
    ("薩田自動車", ["山自動車", "サツタ自動車"]),
    ("ANDPAD", ["アンドパッド", "and pad"]),
]


def render_glossary_for_prompt(custom_glossary_text: str = "") -> str:
    lines: list[str] = []
    for correct, wrongs in DEFAULT_GLOSSARY:
        if wrongs:
            lines.append(f"- 「{correct}」（誤：{'、'.join(wrongs)}）")
        else:
            lines.append(f"- 「{correct}」")
    base = "\n".join(lines)

    if custom_glossary_text and custom_glossary_text.strip():
        base += "\n\n【会議固有の辞書（ユーザー追加）】\n" + custom_glossary_text.strip()
    return base


# ===========================================================================
# 構造化抽出プロンプト（Step 1）
# ===========================================================================

EXTRACTION_PROMPT = """【最初に必ず確認：出力文字数の最低基準】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
入力「文字起こしデータ」の文字数を X とすると：
  ✅ あなたの出力は X × 0.8 以上（目標: X × 0.9）
  ❌ X × 0.5 未満の出力 → 作業失敗・再実行

具体例：X = 20,000文字 → 出力は最低 16,000文字

【途中で「長すぎる」と感じても絶対に省略しない】
省略したくなったら「まだ書いていない発言はないか？」と自問してください。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

あなたは会議の専属速記者です。
あなたの仕事は「文字起こしの内容を圧縮せず、整理されたメモに書き起こす」ことです。
**要約担当ではありません。** ジャーナリストが取材ノートを書き起こす感覚で、すべての発言を保持してください。

────────────────────────────
【最重要・絶対厳守】（順守できない場合は失格）
────────────────────────────
1. **発言量の保持率：80%以上**
   - 文字起こしの文字数を A、抽出結果の文字数を B とすると、**B ≧ 0.8 × A** を目指してください。
   - 議論のニュアンス・条件交渉・補足説明・根拠・例え話・数値・固有名詞は一切省略しない。
   - 「短くまとめる」「要するに」「つまり」と書きたくなったら**それは違反**。元の発言をそのまま書き起こす。
   - **各発言者の発言は、元の文字起こしとほぼ同じ長さで書き起こすこと。**
   - **箇条書きにまとめる場合でも、情報の削除は禁止。**

2. **音声認識の誤変換を必ず修正**
   - 文字起こしには誤変換が含まれます。文脈から判断して**自動的に正しい固有名詞へ修正**してください。
   - 以下の語彙は最優先で修正対象（必ずチェック）：

{custom_glossary}

   - 補足情報・参考過去議事録に出てくる人名・会社名・案件名は、すべて正しい表記に統一すること。

3. **発言者の同定**
   - 文字起こしで発言者ラベルが空（「話者A」など）でも、**話題と文脈から発言者を推定**してください。
   - 出席者名簿（補足情報）と類似音の発言者名は照合して**正しい人物名に統一**。
   - どうしても判断できない場合のみ `[発言者不明]` と記載。捏造は禁止。

4. **相槌・フィラーは徹底的に削除**（重要）
   - 削除対象（例）：
     * 純粋な相槌：「はい」「ええ」「うん」「うんうん」「なるほど」「なるほどなるほど」「そうそう」「ふんふん」
     * 同意の繰り返し：「そうですね」「ですね」「ええ、ええ」「あー、はい」「はい、はい」
     * 言い淀み・フィラー：「えー」「あー」「あのー」「えーっと」「うーん」「まあ」「まー」「ま」「えっと」「そのー」「なんか」「ちょっと」（独立した語として）
     * 言い直しの前部分：「あれが…いや、これが」→「これが」のみ残す
   - **削除しないもの**：
     * 意思表示：「分かりました」「了解しました」「承知しました」「同意します」
     * 明確な否定：「いえ」「違います」「いいえ」（反対意見として）

────────────────────────────
【抽出すべき要素】
────────────────────────────

## 1. 会議の概要
- 会議名（文脈から推定）
- 日時、場所、出席者、欠席者

## 2. 議題リスト（時系列順）
各議題について以下を**漏らさず**記載：
- 議題のタイトル（議論されたテーマ）
- 議論された内容の**すべて**（要約せず原文に近い形で）
- 各発言者の発言（質問・回答・意見・指摘・反論・補足・例え話・業務関連の余談）
- 数値・金額・期日・固有名詞は**絶対に省略禁止**

## 3. 決定事項
- 決定の内容（複数ある場合はすべて）
- 担当者・期限（明示があれば）

## 4. 留意事項・持ち越し事項
- 議論したが結論が出なかった事項
- 次回への持ち越し
- 双方の懸念点・課題認識

────────────────────────────
【出力形式】
────────────────────────────

# 抽出結果

## 会議概要
- 会議名：……
- 日時：……
- 場所：……
- 出席者：……
- 欠席者：……

## 議題1：[タイトル]
### 議論内容（時系列・詳細）
- ……（発言・やりとりを順序立てて、できる限り原文を保持）
- ……

## 議題2：[タイトル]
（同様）

## 決定事項
1. [決定内容]（担当：〇〇 / 期限：〇月〇日）

## 持ち越し・留意事項
- ……

────────────────────────────
【再度の最重要ルール】
────────────────────────────
- **要約しない**。短くしようとしない。**文字起こしの 80% 以上の文字量**を保つ。
- **誤変換は必ず修正**（特に上記カスタム辞書の語）。不明な発言者・名前は捏造せず `[発言者不明]`。
- **数値・金額・期日・固有名詞は1つも省略しない**。
- **相槌・フィラーは徹底的に削除**。意味のある発言のみ残す。
- 出力は上記の構造化テキストのみ。前置き・後書き禁止。

【補足情報（ユーザー入力）】
{metadata}

【参考: 前回までの議事録】
{reference_minutes}

【文字起こしデータ】
{transcript}
"""


FORMATTING_PROMPT_HEADER = """あなたは社内議事録の整形担当です。
以下に「構造化抽出済みの会議情報」と「議事録テンプレート」が提示されます。
構造化情報を**そのままの密度で**テンプレートのフォーマットに落とし込んでください。

────────────────────────────
【最重要・絶対厳守】
────────────────────────────
1. **要約しない・圧縮しない（数値基準）**
   - 入力「構造化抽出済み情報」の文字数を Y とする
   - **あなたの出力は Y × 0.85 以上**でなければならない
   - 例：Y = 15,000文字 → 出力は最低 12,750文字
   - 整形時に発言を短くしたり、複数の発言を1行にまとめたりしない
   - **「整形 = 見出しを変えて並び替えるだけ」** であり、内容は一切削らない

2. **箇条書きを統合・削減しない**
   - 入力に5つの箇条書きがあれば、出力も5つの箇条書きで出力する
   - 「・A ・B ・C」を「・A、B、C」のように1行にまとめることは禁止
   - 各発言の詳細説明・背景・数値は1つも省略しない

3. **誤変換の修正を維持**
   - 構造化情報で正しく修正されている固有名詞（例：ニッケン建設、中辻良太）を、整形時に元の誤変換に戻さない。

4. **テンプレートの指定を厳守**
   - テンプレートが指定する見出し階層・番号・記号・トーン（です・ます調 / である調）を完全に守る。
   - テンプレートに含まれる Few-shot 例の体裁を真似る。

5. **捏造禁止**
   - 構造化情報に含まれていない事実は書かない。
   - 不明箇所は `[不明]` と明記。

6. **テンプレート内のプレースホルダ表記は出力しない**
   - `{metadata}`、`{transcript}`、`{reference_minutes}` などは実際の内容に置換済みなので、そのまま出力しない。

7. **前置き・後書き禁止**
   - 「以下が議事録です」「以上です」のような文言を絶対に書かない。
   - 出力は議事録本文のみ。

8. **相槌・フィラーは徹底的に削除（再混入禁止）**
   - 削除すべき語：「はい」「ええ」「うん」「うんうん」「なるほど」「なるほどなるほど」「そうそう」「ふんふん」「そうですね」「ですね」「あー」「えー」「あのー」「えーっと」「うーん」「まあ」「まー」「ま」「えっと」「そのー」「ちょっと」（独立した語として）
   - 万一、構造化情報に相槌が残っていた場合は、整形時に **徹底的に削除** してください。

【テンプレート（指定フォーマット）】
{template}

【構造化抽出済み情報】
{structured}

【補足情報】
{metadata}

────────────────────────────
【最終確認（整形完了前に必ずチェック）】
────────────────────────────
□ 出力文字数は入力（構造化情報）の85%以上か？
□ テンプレートの見出し構造を厳守しているか？
□ 議論詳細は構造化情報の文字量をそのまま維持しているか（短くしていないか）？
□ 決定事項は箇条書きで、担当・期限が明確か？
□ 固有名詞は構造化情報の修正済み表記に統一されているか？
□ 出力は議事録本文のみか（前置き・後書きなしか）？
"""


# ===========================================================================
# バリデーション
# ===========================================================================

@dataclass
class ValidationResult:
    ok: bool
    missing_sections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    minutes_length: int = 0


_REQUIRED_HINTS = [
    ["基本情報", "会議概要", "概要"],
    ["議論", "議事", "本件", "内容", "話し合い"],
    ["決定", "結論", "決まった", "Action", "アクション", "次に", "ToDo"],
]


def validate_minutes(
    minutes: str,
    template: str = "",
    transcript: str = "",
    min_volume_ratio: float = 0.5,
) -> ValidationResult:
    missing: list[str] = []
    warnings: list[str] = []

    for hints in _REQUIRED_HINTS:
        if not any(hint in minutes for hint in hints):
            missing.append(" / ".join(hints))

    if len(minutes) < 500:
        warnings.append(
            f"議事録が極端に短い（{len(minutes)}文字）。"
            "文字起こしが薄いか、生成が打ち切られた可能性。"
        )

    if transcript and len(transcript) > 1000:
        ratio = len(minutes) / len(transcript)
        if ratio < min_volume_ratio:
            warnings.append(
                f"議事録の分量が文字起こしの {ratio*100:.0f}% しかありません"
                f"（{len(minutes):,}/{len(transcript):,}文字）。"
                f"要約されすぎの可能性。品質プリセットを「最高品質（Pro）」に上げるか、"
                f"カスタム辞書を入力して再生成してください。"
            )

    return ValidationResult(
        ok=not missing,
        missing_sections=missing,
        warnings=warnings,
        minutes_length=len(minutes),
    )


# ===========================================================================
# Gemini 共通呼び出し
# ===========================================================================

def _call_gemini(
    client,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    thinking_budget: int = 0,
) -> str:
    from google.genai import types

    cfg_kwargs = {
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    if thinking_budget and thinking_budget > 0:
        try:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=thinking_budget
            )
        except (AttributeError, TypeError):
            pass

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    text = response.text or ""
    return text


def _call_gemini_with_fallback(
    client,
    primary_model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    thinking_budget: int,
    progress_callback=None,
    label: str = "",
) -> str:
    """503/429 のときは別モデルへフォールバックする。
    フォールバック時は thinking_budget を自動スケールダウンして品質劣化を最小化する。
    """
    fallback = [
        primary_model,
        "models/gemini-2.5-flash",
        "models/gemini-flash-latest",
    ]
    seen: set[str] = set()
    fallback = [m for m in fallback if not (m in seen or seen.add(m))]  # type: ignore[func-returns-value]

    last_err: Optional[Exception] = None
    for i, model in enumerate(fallback):
        # フォールバックモデルでは thinking_budget を段階的に下げる
        if i == 0:
            effective_thinking = thinking_budget
        elif i == 1:
            effective_thinking = min(thinking_budget, 4096)
        else:
            effective_thinking = 0  # 3段目以降は thinking 無効

        if progress_callback:
            fallback_note = f"（フォールバック {i}）" if i > 0 else ""
            progress_callback(
                f"{label} {model.replace('models/', '')} で生成中…{fallback_note}"
            )
        for attempt in range(2):
            try:
                logger.info(
                    f"{label} {model} attempt={attempt+1} "
                    f"thinking={effective_thinking} (original={thinking_budget})"
                )
                text = _call_gemini(
                    client=client,
                    model=model,
                    prompt=prompt,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    thinking_budget=effective_thinking,
                )
                if text.strip():
                    return text
                last_err = RuntimeError("Gemini が空応答を返しました")
            except Exception as e:
                last_err = e
                err_str = str(e)
                logger.warning(f"{label} {model} 失敗: {type(e).__name__}: {err_str[:200]}")
                if "429" in err_str:
                    break
                if "503" in err_str and attempt == 0:
                    if progress_callback:
                        progress_callback(f"{label} {model} 混雑中、15秒待機…")
                    time.sleep(15)
                    continue
                break

    raise RuntimeError(
        f"{label} 全モデルで失敗: {type(last_err).__name__}: {last_err}"
    )


# ===========================================================================
# Google Gemini（メインエントリ）
# ===========================================================================

def generate_minutes_gemini(
    transcript: str,
    template: str,
    meeting_date: str = "",
    meeting_location: str = "",
    attendees: str = "",
    model: str = "models/gemini-2.5-flash",
    fallback_models: Optional[list] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    quality_preset: str = DEFAULT_QUALITY,
    reference_minutes: Optional[list[str]] = None,
    custom_glossary: str = "",
    project_names: str = "",
) -> str:
    from google import genai

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY が設定されていません。\n"
            "https://aistudio.google.com/apikey で無料取得し、"
            ".env ファイルに設定してください。"
        )

    preset = QUALITY_PRESETS.get(quality_preset, QUALITY_PRESETS[DEFAULT_QUALITY])
    primary_model = preset["model"]
    temperature = preset["temperature"]
    max_tokens = preset["max_output_tokens"]
    thinking_budget = preset["thinking_budget"]
    two_stage = preset["two_stage"]
    validate_retries = preset["validate_retries"]

    if model and model != "models/gemini-2.5-flash":
        primary_model = model

    logger.info(
        f"=== 議事録生成開始 preset={quality_preset} model={primary_model} "
        f"two_stage={two_stage} thinking={thinking_budget} transcript_len={len(transcript)} ==="
    )

    metadata_block = _build_metadata_block(
        meeting_date, meeting_location, attendees, project_names
    )
    reference_block = _build_reference_block(reference_minutes)

    client = genai.Client(api_key=api_key)

    last_minutes = ""
    last_validation: Optional[ValidationResult] = None
    for attempt in range(validate_retries + 1):
        if progress_callback and attempt > 0:
            progress_callback(
                f"前回の生成で必須項目が欠落していたため再生成中…（試行 {attempt + 1}）"
            )

        if two_stage:
            minutes = _generate_two_stage(
                client=client,
                transcript=transcript,
                template=template,
                metadata_block=metadata_block,
                reference_block=reference_block,
                custom_glossary=custom_glossary,
                primary_model=primary_model,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking_budget=thinking_budget,
                progress_callback=progress_callback,
            )
        else:
            minutes = _generate_single_stage(
                client=client,
                transcript=transcript,
                template=template,
                meeting_date=meeting_date,
                meeting_location=meeting_location,
                attendees=attendees,
                reference_block=reference_block,
                custom_glossary=custom_glossary,
                project_names=project_names,
                primary_model=primary_model,
                temperature=temperature,
                max_tokens=max_tokens,
                thinking_budget=thinking_budget,
                progress_callback=progress_callback,
            )

        last_minutes = minutes
        last_validation = validate_minutes(minutes, template)
        logger.info(
            f"試行{attempt+1} 結果: ok={last_validation.ok} "
            f"missing={last_validation.missing_sections} "
            f"len={last_validation.minutes_length}"
        )
        if last_validation.ok:
            break

    logger.info(f"=== 議事録生成終了: 文字数 {len(last_minutes)} ===")
    return last_minutes


# ===========================================================================
# 二段階生成（ステップ間バリデーション付き）
# ===========================================================================

def _generate_two_stage(
    client,
    transcript: str,
    template: str,
    metadata_block: str,
    reference_block: str,
    primary_model: str,
    temperature: float,
    max_tokens: int,
    thinking_budget: int,
    progress_callback=None,
    custom_glossary: str = "",
) -> str:
    """
    Step 1: 構造化抽出 → Step 2: テンプレート整形
    各ステップ後に文字数比率をチェックし、不足なら強化プロンプトで1回再試行する。
    """
    transcript_len = len(transcript)

    # === Step 1: 構造化抽出 ===
    if progress_callback:
        progress_callback("Step 1/2: 議題・発言・決定事項を構造化抽出中…")

    extraction_prompt = _safe_render_extraction(
        transcript=transcript,
        metadata=metadata_block,
        reference=reference_block,
        custom_glossary=custom_glossary,
    )
    structured = _call_gemini_with_fallback(
        client=client,
        primary_model=primary_model,
        prompt=extraction_prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
        thinking_budget=thinking_budget,
        progress_callback=progress_callback,
        label="[Step 1/2 抽出]",
    )
    step1_ratio = len(structured) / max(transcript_len, 1)
    logger.info(f"Step1 抽出結果: {len(structured)}文字 / transcript: {transcript_len}文字 = {step1_ratio:.1%}")

    # --- Step 1 バリデーション: 比率が低すぎる場合は再抽出 ---
    if step1_ratio < _STEP1_MIN_RATIO:
        logger.warning(
            f"Step1 出力が不十分（{step1_ratio:.1%} < {_STEP1_MIN_RATIO:.0%}）。"
            f"強化プロンプトで再抽出します。"
        )
        if progress_callback:
            progress_callback(
                f"⚠️ Step 1 の抽出量が不足（{step1_ratio:.0%}）。"
                f"強化プロンプトで再抽出中…"
            )
        min_chars = int(transcript_len * 0.8)
        retry_prefix = (
            f"【緊急追加指示】\n"
            f"前回の抽出結果が文字起こし（{transcript_len}文字）のわずか {step1_ratio:.0%} しかありませんでした。\n"
            f"これは失格です。文字起こし全体を読み直し、**最低 {min_chars} 文字**の抽出結果を出力してください。\n"
            f"省略・要約は一切禁止です。すべての発言・議論・数値・固有名詞を書き起こしてください。\n\n"
        )
        structured = _call_gemini_with_fallback(
            client=client,
            primary_model=primary_model,
            prompt=retry_prefix + extraction_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
            thinking_budget=thinking_budget,
            progress_callback=progress_callback,
            label="[Step 1/2 再抽出]",
        )
        logger.info(f"Step1 再抽出結果: {len(structured)}文字 ({len(structured)/max(transcript_len,1):.1%})")

    step1_len = len(structured)

    # === Step 2: テンプレート整形 ===
    if progress_callback:
        progress_callback("Step 2/2: テンプレートに沿って整形中…")

    formatting_prompt = _safe_render_formatting(
        template=template,
        structured=structured,
        metadata=metadata_block,
    )
    minutes = _call_gemini_with_fallback(
        client=client,
        primary_model=primary_model,
        prompt=formatting_prompt,
        temperature=max(temperature - 0.1, 0.0),
        max_output_tokens=max_tokens,
        thinking_budget=max(thinking_budget // 2, 0),
        progress_callback=progress_callback,
        label="[Step 2/2 整形]",
    )
    step2_ratio = len(minutes) / max(step1_len, 1)
    logger.info(f"Step2 整形結果: {len(minutes)}文字 / Step1: {step1_len}文字 = {step2_ratio:.1%}")

    # --- Step 2 バリデーション: 圧縮しすぎなら再整形 ---
    if step2_ratio < _STEP2_MIN_RATIO:
        logger.warning(
            f"Step2 整形で情報が圧縮されすぎ（{step2_ratio:.1%} < {_STEP2_MIN_RATIO:.0%}）。"
            f"強化プロンプトで再整形します。"
        )
        if progress_callback:
            progress_callback(
                f"⚠️ Step 2 の出力が圧縮されすぎ（{step2_ratio:.0%}）。"
                f"強化プロンプトで再整形中…"
            )
        min_chars = int(step1_len * 0.8)
        retry_prefix = (
            f"【緊急追加指示】\n"
            f"前回の整形結果（{len(minutes)}文字）が構造化情報（{step1_len}文字）の"
            f"{step2_ratio:.0%} しかありませんでした。情報が大量に失われています。\n"
            f"**最低 {min_chars} 文字**の議事録を出力してください。\n"
            f"各議題の箇条書き項目を省略・統合せず、すべてをテンプレートに落とし込んでください。\n\n"
        )
        minutes = _call_gemini_with_fallback(
            client=client,
            primary_model=primary_model,
            prompt=retry_prefix + formatting_prompt,
            temperature=max(temperature - 0.1, 0.0),
            max_output_tokens=max_tokens,
            thinking_budget=max(thinking_budget // 2, 0),
            progress_callback=progress_callback,
            label="[Step 2/2 再整形]",
        )
        logger.info(f"Step2 再整形結果: {len(minutes)}文字 ({len(minutes)/max(step1_len,1):.1%})")

    return minutes


# ===========================================================================
# 一段階生成（fast プリセット用）
# ===========================================================================

def _generate_single_stage(
    client,
    transcript: str,
    template: str,
    meeting_date: str,
    meeting_location: str,
    attendees: str,
    reference_block: str,
    primary_model: str,
    temperature: float,
    max_tokens: int,
    thinking_budget: int,
    progress_callback=None,
    custom_glossary: str = "",
    project_names: str = "",
) -> str:
    prompt = fill_template(
        template=template,
        transcript=transcript,
        meeting_date=meeting_date,
        meeting_location=meeting_location,
        attendees=attendees,
        project_names=project_names,
    )
    if reference_block.strip() and "{reference_minutes}" not in template:
        prompt += "\n\n# 参考: 前回までの議事録\n" + reference_block
    glossary_block = render_glossary_for_prompt(custom_glossary)
    prompt = (
        "【最重要・固有名詞辞書】文字起こしの誤変換は以下を参考に必ず修正してください。\n"
        + glossary_block
        + "\n\n"
        + prompt
    )

    if progress_callback:
        progress_callback("議事録を生成中…（一発生成）")

    return _call_gemini_with_fallback(
        client=client,
        primary_model=primary_model,
        prompt=prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
        thinking_budget=thinking_budget,
        progress_callback=progress_callback,
        label="[一発生成]",
    )


# ===========================================================================
# プロンプト用ブロック組み立て
# ===========================================================================

def _build_metadata_block(
    meeting_date: str,
    meeting_location: str,
    attendees: str,
    project_names: str = "",
) -> str:
    lines = []
    if meeting_date:
        lines.append(f"- 開催日：{meeting_date}")
    if meeting_location:
        lines.append(f"- 開催場所：{meeting_location}")
    if attendees:
        lines.append(f"- 確定出席者：{attendees}")
    if project_names and project_names.strip():
        names = [
            n.strip()
            for n in project_names.replace("、", ",").replace("\n", ",").split(",")
            if n.strip()
        ]
        if names:
            lines.append(
                "- 関連プロジェクト・案件名（**正しい固有名詞として扱うこと**）：\n  "
                + "\n  ".join(f"・{n}" for n in names)
            )
    return "\n".join(lines) if lines else "（補足情報なし）"


def _build_reference_block(reference_minutes: Optional[list[str]]) -> str:
    if not reference_minutes:
        return "（参考過去議事録なし）"
    parts = []
    for i, ref in enumerate(reference_minutes[:3], start=1):
        truncated = ref[:3000] + ("\n…（以下省略）…" if len(ref) > 3000 else "")
        parts.append(f"【参考{i}】\n{truncated}")
    return "\n\n".join(parts)


def _safe_render_extraction(
    transcript: str,
    metadata: str,
    reference: str,
    custom_glossary: str = "",
) -> str:
    glossary_block = render_glossary_for_prompt(custom_glossary)
    return (
        EXTRACTION_PROMPT
        .replace("{custom_glossary}", glossary_block)
        .replace("{metadata}", metadata)
        .replace("{reference_minutes}", reference)
        .replace("{transcript}", transcript)
    )


def _safe_render_formatting(template: str, structured: str, metadata: str) -> str:
    return (
        FORMATTING_PROMPT_HEADER
        .replace("{template}", template)
        .replace("{structured}", structured)
        .replace("{metadata}", metadata)
    )


# ===========================================================================
# Ollama（ローカルLLM）
# ===========================================================================

def generate_minutes_ollama(
    transcript: str,
    template: str,
    meeting_date: str = "",
    meeting_location: str = "",
    attendees: str = "",
    model: str = "qwen2.5:7b",
    base_url: str = "http://localhost:11434",
    num_predict: int = 8000,
) -> str:
    prompt = fill_template(
        template=template,
        transcript=transcript,
        meeting_date=meeting_date,
        meeting_location=meeting_location,
        attendees=attendees,
    )

    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": num_predict,
                    "temperature": 0.3,
                },
            },
            timeout=600,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Ollama に接続できません。\nOllama が起動しているか確認してください。"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(
            "Ollama の応答がタイムアウトしました。\n"
            "テキストを短くするか、しばらく待ってから再試行してください。"
        )

    return response.json().get("response", "")


def is_ollama_running(base_url: str = "http://localhost:11434") -> bool:
    try:
        r = requests.get(f"{base_url}/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


# ===========================================================================
# Anthropic Claude
# ===========================================================================

def generate_minutes(
    transcript: str,
    template: str,
    meeting_date: str = "",
    meeting_location: str = "",
    attendees: str = "",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 32000,
    quality_preset: str = DEFAULT_QUALITY,
    reference_minutes: Optional[list[str]] = None,
    custom_glossary: str = "",
    project_names: str = "",
) -> str:
    """Claude API を使って議事録を生成する（二段階生成 + ステップ間バリデーション付き）。"""
    from anthropic import Anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY が設定されていません。"
            ".env ファイルに APIキーを設定してください。"
        )

    preset = QUALITY_PRESETS.get(quality_preset, QUALITY_PRESETS[DEFAULT_QUALITY])
    two_stage = preset["two_stage"]
    metadata_block = _build_metadata_block(
        meeting_date, meeting_location, attendees, project_names
    )
    reference_block = _build_reference_block(reference_minutes)

    client = Anthropic(api_key=api_key)

    def _claude_call(prompt: str, temperature: float = 0.3) -> str:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in message.content if hasattr(b, "text"))

    if not two_stage:
        prompt = fill_template(
            template=template,
            transcript=transcript,
            meeting_date=meeting_date,
            meeting_location=meeting_location,
            attendees=attendees,
            project_names=project_names,
        )
        if reference_block.strip():
            prompt += "\n\n# 参考: 前回までの議事録\n" + reference_block
        glossary_block = render_glossary_for_prompt(custom_glossary)
        prompt = (
            "【最重要・固有名詞辞書】文字起こしの誤変換は以下を参考に必ず修正してください。\n"
            + glossary_block
            + "\n\n"
            + prompt
        )
        return _claude_call(prompt, temperature=0.2)

    # === 二段階生成（バリデーション付き）===
    extraction_prompt = _safe_render_extraction(
        transcript=transcript,
        metadata=metadata_block,
        reference=reference_block,
        custom_glossary=custom_glossary,
    )
    structured = _claude_call(extraction_prompt, temperature=0.3)

    # Step 1 バリデーション
    step1_ratio = len(structured) / max(len(transcript), 1)
    logger.info(f"[Claude] Step1: {len(structured)}文字 / transcript: {len(transcript)}文字 = {step1_ratio:.1%}")
    if step1_ratio < _STEP1_MIN_RATIO:
        logger.warning(f"[Claude] Step1 不足。再抽出します。")
        min_chars = int(len(transcript) * 0.8)
        retry_prefix = (
            f"【緊急追加指示】前回の抽出が {step1_ratio:.0%} しかありませんでした。"
            f"最低 {min_chars} 文字で再抽出してください。\n\n"
        )
        structured = _claude_call(retry_prefix + extraction_prompt, temperature=0.3)

    formatting_prompt = _safe_render_formatting(
        template=template,
        structured=structured,
        metadata=metadata_block,
    )
    minutes = _claude_call(formatting_prompt, temperature=0.2)

    # Step 2 バリデーション
    step2_ratio = len(minutes) / max(len(structured), 1)
    logger.info(f"[Claude] Step2: {len(minutes)}文字 / Step1: {len(structured)}文字 = {step2_ratio:.1%}")
    if step2_ratio < _STEP2_MIN_RATIO:
        logger.warning(f"[Claude] Step2 圧縮されすぎ。再整形します。")
        min_chars = int(len(structured) * 0.8)
        retry_prefix = (
            f"【緊急追加指示】前回の整形が {step2_ratio:.0%} に圧縮されました。"
            f"最低 {min_chars} 文字で再整形してください。\n\n"
        )
        minutes = _claude_call(retry_prefix + formatting_prompt, temperature=0.2)

    return minutes
