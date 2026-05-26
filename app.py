"""議事録作成システム - Streamlit UI"""
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# Streamlit Cloud: st.secrets の値を環境変数へ橋渡し
try:
    for _key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        if _key not in os.environ and _key in st.secrets:
            os.environ[_key] = st.secrets[_key]
except Exception:
    pass

# ===== Cloud 環境判定 =====
IS_CLOUD = os.getenv("STREAMLIT_SHARING_MODE") or os.getenv("STREAMLIT_SERVER_ADDRESS") == "0.0.0.0"

from src.config import ProjectConfig
from src.minutes_archive import (
    MinutesMetadata,
    collect_all_tags,
    export_minutes_as_zip,
    filter_minutes,
    generate_output_filename,
    highlight_snippet,
    list_minutes,
    load_metadata,
    save_metadata,
    search_minutes,
)
from src.cache import get_default_cache
from src.run_log import get_default_logger, log_generate, log_transcribe
from src.minutes_generator import (
    QUALITY_PRESETS,
    generate_minutes,
    generate_minutes_gemini,
    generate_minutes_ollama,
    is_ollama_running,
    validate_minutes,
)
from src.exporters import MeetingMeta, to_docx_bytes, to_xlsx_bytes
from src.template_loader import load_template_content, load_templates
from src.transcriber import transcribe_audio, transcribe_audio_gemini



# ===== ページ設定 =====
st.set_page_config(
    page_title="議事録作成システム",
    page_icon="📝",
    layout="wide",
)



# ===== キャッシュ関数 =====
@st.cache_resource
def get_config() -> ProjectConfig:
    return ProjectConfig()


@st.cache_data
def get_templates(config_path_str: str) -> dict:
    return load_templates(Path(config_path_str))


config = get_config()
templates = get_templates(str(config.templates_config_path))


def has_api_key() -> bool:
    """
    APIキーの有効性を簡易チェックする。
    - 未設定・空文字はNG
    - プレースホルダー（.env.example のサンプル値）もNG扱い
    """
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return False
    if "xxxxxxxx" in key.lower():
        return False
    return True


# ===== タイトル =====
st.title("議事録作成システム")
st.caption("ニッケン建設 / JC神奈川ブロック 議事録作成支援ツール")

_engine = config.generation_engine
if _engine == "gemini" and not os.getenv("GOOGLE_API_KEY", "").strip():
    st.warning(
        "**GOOGLE_API_KEY が設定されていません。** "
        "[Google AI Studio](https://aistudio.google.com/apikey) で無料取得し、"
        "`.env` ファイルに `GOOGLE_API_KEY=...` を追加してください。"
    )
elif _engine == "claude" and not has_api_key():
    st.warning(
        "**ANTHROPIC_API_KEY が設定されていません。** "
        "`.env` ファイルに APIキーを設定してください。"
    )


# ===== サイドバー（両タブ共通） =====
with st.sidebar:
    # ===== 今日のAPI使用量インジケータ =====
    try:
        _today_str = datetime.now().strftime("%Y-%m-%d")
        _today_records = [
            r for r in get_default_logger().read_all()
            if r.timestamp.startswith(_today_str) and r.success
        ]
        _today_api = sum(
            max(1, r.chunk_count) if r.kind == "transcribe" else 2
            for r in _today_records
        )
        _remaining = max(0, 20 - _today_api)
        _bar_pct = min(100, int(_today_api / 20 * 100))
        _color = "🟢" if _remaining > 8 else "🟡" if _remaining > 3 else "🔴"
        st.caption(f"{_color} 今日のAPI使用：≈{_today_api}/20 req（残り{_remaining}）")
        st.progress(_bar_pct / 100)
        if _remaining <= 3:
            st.warning("⚠️ Free Tier 残量わずか")
    except Exception:
        pass

    st.header("会議種別の選択")
    template_id = st.selectbox(
        "テンプレート",
        options=list(templates.keys()),
        format_func=lambda k: f"{k}: {templates[k]['name']}",
        key="template_selector",
    )
    st.caption(templates[template_id].get("description", ""))

    st.divider()
    output_format_label = (
        "Markdown"
        if templates[template_id].get("output_format") == "markdown"
        else "プレーンテキスト"
    )
    st.write(f"**出力形式：** {output_format_label}")

    st.divider()
    if st.button(
        "新規作成（状態をリセット）",
        use_container_width=True,
        help="文字起こし・生成結果をクリアして最初からやり直します",
    ):
        for key in (
            "transcript",
            "minutes",
            "generated_filename",
            "manual_text_input",
            "transcript_editor",
        ):
            st.session_state.pop(key, None)
        st.rerun()

    with st.expander("詳細設定", expanded=False):
        _GEMINI_MODELS = [
            "models/gemini-2.5-flash",
            "models/gemini-2.5-pro",
            "models/gemini-2.0-flash",
            "models/gemini-1.5-flash",
        ]
        st.caption("音声認識エンジン")
        st.code(config.transcription_engine)
        if config.transcription_engine == "gemini":
            st.caption("🎙 音声認識モデル")
            _tx_default_idx = (
                _GEMINI_MODELS.index(config.gemini_transcription_model)
                if config.gemini_transcription_model in _GEMINI_MODELS else 0
            )
            st.selectbox(
                "音声認識Geminiモデル",
                options=_GEMINI_MODELS,
                index=_tx_default_idx,
                format_func=lambda m: {
                    "models/gemini-2.5-flash": "gemini-2.5-flash（推奨・無料枠OK）",
                    "models/gemini-2.5-pro": "gemini-2.5-pro（高精度・有料枠推奨）",
                    "models/gemini-2.0-flash": "gemini-2.0-flash（旧版安定）",
                    "models/gemini-1.5-flash": "gemini-1.5-flash（軽量）",
                }.get(m, m),
                key="gemini_tx_model_select",
                label_visibility="collapsed",
            )
        else:
            st.caption("Whisperモデル")
            st.code(config.whisper_model_size)
        st.caption("生成エンジン")
        st.code(config.generation_engine)
        if config.generation_engine == "gemini":
            st.caption("🤖 議事録生成モデル（品質プリセットを上書き）")
            _GEN_OPTIONS = ["（品質プリセット任せ）"] + _GEMINI_MODELS
            st.selectbox(
                "生成Geminiモデル",
                options=_GEN_OPTIONS,
                index=0,
                format_func=lambda m: m if m.startswith("（") else {
                    "models/gemini-2.5-flash": "gemini-2.5-flash（高速・無料枠OK）",
                    "models/gemini-2.5-pro": "gemini-2.5-pro（最高品質・有料枠推奨）",
                    "models/gemini-2.0-flash": "gemini-2.0-flash（旧版安定）",
                    "models/gemini-1.5-flash": "gemini-1.5-flash（軽量）",
                }.get(m, m),
                key="gemini_gen_model_select",
                label_visibility="collapsed",
            )
            _sel_gen = st.session_state.get("gemini_gen_model_select", "（品質プリセット任せ）")
            if _sel_gen and not _sel_gen.startswith("（"):
                st.warning(f"⚠️ モデル固定中: **{_sel_gen.replace('models/', '')}**")
        elif config.generation_engine == "ollama":
            st.caption("Ollamaモデル")
            st.code(config.ollama_model)
        else:
            st.caption("Claudeモデル")
            st.code(config.claude_model)


# ===== タブ構成 =====
tab_generate, tab_archive, tab_stats, tab_templates = st.tabs(
    ["📝 議事録生成", "📚 過去の議事録", "📊 統計", "⚙️ テンプレート編集"]
)


# ===== タブ1: 議事録生成 =====
with tab_generate:
    col_input, col_meta = st.columns([2, 1])

    with col_input:
        st.subheader("1. 入力")
        input_mode = st.radio(
            "入力方法",
            ["音声ファイル", "文字起こしテキスト"],
            horizontal=True,
        )

        if input_mode == "音声ファイル":
            audio_file = st.file_uploader(
                "音声ファイルをアップロード",
                type=["mp3", "wav", "m4a", "ogg", "flac"],
                help="mp3 / wav / m4a / ogg / flac 形式に対応",
            )

            speed_preset = st.radio(
                "文字起こし速度",
                ["balanced", "fastest", "quality"],
                index=["balanced", "fastest", "quality"].index(
                    config.transcription_speed_preset
                ),
                horizontal=True,
                format_func=lambda x: {
                    "fastest": "🚀 最速（1.5倍速・30分区切り）",
                    "balanced": "⚖️ バランス（推奨・25分区切り）",
                    "quality": "🎯 高品質（等倍・20分区切り）",
                }[x],
                help=(
                    "🚀 最速：1.5倍速＋無音除去＋30分区切り。1時間音声を2回で完了。\n\n"
                    "⚖️ バランス：1.2倍速＋無音除去＋25分区切り。速度と品質を両立（推奨）。\n\n"
                    "🎯 高品質：等倍・前処理なし＋20分区切り。最高品質。\n\n"
                    "※ Gemini無料枠（5 req/min）を尊重するため、内部でレート制限を自動管理。"
                    "短時間での連続実行は控えめにお願いします。"
                ),
            )

            # ===== 重複実行防止 =====
            # Streamlit は再実行が多いため、文字起こし中にもう一度クリックされると
            # クォータが二重消費されてしまう。session_state でロックする。
            transcribing = st.session_state.get("transcribing", False)
            run_button_label = (
                "⏳ 文字起こし実行中…（完了までお待ちください）"
                if transcribing
                else "文字起こしを実行"
            )
            run_clicked = st.button(
                run_button_label,
                type="primary",
                disabled=transcribing or not audio_file,
                key="run_transcribe_btn",
            )

            if audio_file and run_clicked and not transcribing:
                st.session_state["transcribing"] = True
                temp_path = config.input_dir / audio_file.name
                audio_bytes = audio_file.getbuffer().tobytes()
                temp_path.write_bytes(audio_bytes)
                _tx_start = time.time()
                _tx_engine = config.transcription_engine
                _tx_model = (
                    st.session_state.get(
                        "gemini_tx_model_select", config.gemini_transcription_model
                    )
                    if _tx_engine == "gemini"
                    else f"whisper-{config.whisper_model_size}"
                )

                # ===== キャッシュチェック =====
                cache = get_default_cache()
                cache_key = cache.hash_audio(audio_bytes, speed_preset, _tx_model)
                cached_transcript = cache.get_transcript(cache_key)

                progress_bar = st.progress(0.0, text="準備中…")

                def update_progress(ratio: float, status: str) -> None:
                    progress_bar.progress(ratio, text=status)

                _tx_error = ""
                _tx_success = False
                _tx_from_cache = False
                try:
                    if cached_transcript is not None:
                        # キャッシュヒット → APIを呼ばない
                        transcript = cached_transcript
                        _tx_from_cache = True
                        progress_bar.progress(1.0, text="✅ キャッシュから即時取得")
                    elif config.transcription_engine == "gemini":
                        if not os.getenv("GOOGLE_API_KEY", "").strip():
                            st.error(
                                "GOOGLE_API_KEY が設定されていません。"
                                "`.env` ファイルに追加してください。"
                            )
                            st.stop()
                        transcript = transcribe_audio_gemini(
                            temp_path,
                            model=_tx_model,
                            progress_callback=update_progress,
                            speed_preset=speed_preset,
                        )
                    else:
                        transcript = transcribe_audio(
                            temp_path,
                            model_size=config.whisper_model_size,
                            device=config.whisper_device,
                            compute_type=config.whisper_compute_type,
                            progress_callback=update_progress,
                        )
                    st.session_state["transcript"] = transcript
                    _tx_success = "文字起こしに失敗しました" not in transcript
                    # キャッシュに保存（成功時のみ）
                    if _tx_success and not _tx_from_cache:
                        try:
                            cache.save_transcript(
                                cache_key, transcript,
                                source_label=audio_file.name,
                            )
                        except Exception:
                            pass
                    if _tx_from_cache:
                        st.success(
                            f"✅ 文字起こしをキャッシュから復元しました "
                            f"（{len(transcript):,}文字、APIリクエスト消費なし）"
                        )
                    elif "文字起こしに失敗しました" in transcript:
                        st.warning(
                            "一部の区間で文字起こしに失敗しました（結果内に "
                            "`[XX:XX〜XX:XX のセグメントは文字起こしに失敗…]` と表示されます）。"
                            "時間を置いてからもう一度実行するか、失敗箇所は手動で補完してください。"
                        )
                    else:
                        st.success("文字起こしが完了しました")
                except ImportError as e:
                    _tx_error = f"ImportError: {e}"
                    st.error(
                        f"必要なパッケージがインストールされていません：{e}\n\n"
                        "`起動.bat` を使って依存パッケージをインストールしてください。"
                    )
                except Exception as e:
                    err_msg = str(e)
                    _tx_error = f"{type(e).__name__}: {err_msg[:300]}"
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                        st.error(
                            "Gemini APIの無料枠（毎分5リクエスト・1日250リクエスト）を"
                            "超過しました。\n\n"
                            "▶ 1〜2分待ってから再実行してください。\n"
                            "▶ それでも繰り返す場合は、AI Studio で API 利用枠を確認してください："
                            "https://ai.google.dev/gemini-api/docs/rate-limits"
                        )
                    elif "503" in err_msg or "UNAVAILABLE" in err_msg:
                        st.error(
                            "Gemini API サーバーが混雑中です（503）。"
                            "少し時間を置いてから再実行してください。"
                        )
                    else:
                        st.error(
                            f"文字起こし中にエラーが発生しました：{e}\n\n"
                            f"`transcriber_debug.log` に詳細を記録しています。"
                        )
                finally:
                    st.session_state["transcribing"] = False
                    # ===== 実行ログを記録 =====
                    try:
                        _tx_text = st.session_state.get("transcript", "") or ""
                        log_transcribe(
                            success=_tx_success,
                            duration_sec=time.time() - _tx_start,
                            audio_filename=audio_file.name if audio_file else "",
                            transcript_chars=len(_tx_text),
                            speed_preset=speed_preset,
                            model=_tx_model,
                            error=_tx_error,
                        )
                    except Exception:
                        pass
        else:
            manual_text = st.text_area(
                "文字起こしテキストを貼り付け",
                height=300,
                placeholder="ここに文字起こし済みテキストを貼り付けてください",
                key="manual_text_input",
            )
            if manual_text:
                st.session_state["transcript"] = manual_text

        # 文字起こし結果の編集
        if "transcript" in st.session_state and st.session_state["transcript"]:
            char_count = len(st.session_state["transcript"])
            with st.expander(
                f"文字起こし結果を確認・編集（{char_count:,} 文字）",
                expanded=False,
            ):
                edited = st.text_area(
                    "内容を修正できます",
                    value=st.session_state["transcript"],
                    height=400,
                    key="transcript_editor",
                )
                if edited != st.session_state["transcript"]:
                    st.session_state["transcript"] = edited

    with col_meta:
        st.subheader("2. 会議情報（任意）")
        meeting_date = st.date_input("開催日", value=date.today())
        meeting_location = st.text_input(
            "開催場所",
            placeholder="例：本社会議室",
        )
        attendees_note = st.text_area(
            "確定出席者",
            placeholder="例：横澤代表、倉方取締役、中辻良太",
            height=150,
        )
        project_names = st.text_area(
            "📌 関連プロジェクト・案件名",
            placeholder=(
                "例：勝瀬保育園 第2期工事、桜ヶ丘マンション 修繕、"
                "新規案件「日進町プロジェクト」"
            ),
            height=100,
            help=(
                "今回の会議で扱うプロジェクト名・案件名を入力してください。"
                "AIがこれらを正しい固有名詞として認識し、誤変換を防ぎます。"
                "改行またはカンマ区切りで複数入力可。"
            ),
            key="project_names_input",
        )

    st.divider()

    # ===== 議事録生成 =====
    st.subheader("3. 議事録の生成")

    transcript_ready = bool(
        st.session_state.get("transcript", "").strip()
    )

    status_cols = st.columns(2)
    with status_cols[0]:
        if transcript_ready:
            st.success("文字起こし：準備完了")
        else:
            st.info("文字起こし：未入力")

    if config.generation_engine == "gemini":
        gemini_ready = bool(os.getenv("GOOGLE_API_KEY", "").strip())
        can_generate = transcript_ready and gemini_ready
        with status_cols[1]:
            if gemini_ready:
                st.success(f"Gemini：APIキー設定済み")
            else:
                st.error("Gemini：APIキー未設定（.env に GOOGLE_API_KEY を追加）")

    elif config.generation_engine == "ollama":
        ollama_ready = is_ollama_running(config.ollama_base_url)
        can_generate = transcript_ready and ollama_ready
        with status_cols[1]:
            if ollama_ready:
                st.success(f"Ollama：起動中（{config.ollama_model}）")
            else:
                st.error("Ollama：未起動")

    else:  # claude
        api_key_ready = has_api_key()
        can_generate = transcript_ready and api_key_ready
        with status_cols[1]:
            if api_key_ready:
                st.success("Claude：APIキー設定済み")
            else:
                st.error("Claude：APIキー未設定")

    # ===== 品質プリセット =====
    quality_preset = st.radio(
        "生成品質",
        list(QUALITY_PRESETS.keys()),
        index=list(QUALITY_PRESETS.keys()).index(config.minutes_quality_preset),
        horizontal=True,
        format_func=lambda k: QUALITY_PRESETS[k]["label"],
        help=(
            "⚡ 高速：1ステップ生成・思考なし・gemini-2.5-flash（無料枠を節約したい場合）\n\n"
            "⚖️ バランス：**2段階生成**（① 議題抽出 → ② テンプレート整形）＋ 軽い思考＋自動再試行1回。"
            "推奨。無料枠OK。\n\n"
            "🎯 最高品質：**gemini-2.5-pro** ＋ 深い思考＋自動再試行2回。"
            "プロ品質。Pro モデルは無料枠が小さいため、有料枠推奨。"
        ),
        key="quality_preset_radio",
    )

    # ===== 会議固有の固有名詞辞書（オプション） =====
    with st.expander(
        "🏷️ 会議固有の固有名詞辞書（誤変換修正・任意）",
        expanded=False,
    ):
        st.caption(
            "音声認識の誤変換が起きやすい固有名詞を、正しい表記に強制統一します。"
            "ニッケン建設・中辻良太など社内共通の語彙はすでに登録済み。"
            "ここには会議固有の新しい人物名・会社名・案件名を追加してください。"
        )
        custom_glossary = st.text_area(
            "辞書（フリーフォーマット）",
            value=st.session_state.get("custom_glossary_input", ""),
            height=120,
            placeholder=(
                "例:\n"
                "- 「サーカス株式会社」（誤：サーカスエージェント、サーカス、Circus）\n"
                "- 「藤田」（誤：藤太、フジタ）\n"
                "- 「サーカスエージェント」（製品名、誤：サーカスエージュント）\n"
            ),
            key="custom_glossary_input",
        )

    # ===== 過去議事録の参照（オプション） =====
    with st.expander("📚 過去の議事録を参考にする（オプション）", expanded=False):
        st.caption(
            "同じテンプレートの過去議事録を最大3件まで選び、用語の統一・継続課題のフォローに活用します。"
        )
        try:
            all_archive = list_minutes(config.output_dir)
            same_template_archive = [
                e for e in all_archive if e.template_id == template_id
            ]
        except Exception:
            same_template_archive = []
        if same_template_archive:
            ref_choices = [
                f"{e.meeting_date} - {e.filename}" for e in same_template_archive
            ]
            selected_refs = st.multiselect(
                "参考にする過去議事録",
                options=ref_choices,
                max_selections=3,
                key="reference_minutes_select",
            )
        else:
            selected_refs = []
            st.info(
                f"テンプレート「{templates[template_id]['name']}」の過去議事録はまだありません。"
            )

    if st.button(
        "議事録を生成",
        type="primary",
        disabled=not can_generate,
        use_container_width=True,
    ):
        _engine_labels = {
            "gemini": f"Gemini（{config.gemini_model.replace('models/', '')}）",
            "ollama": f"Ollama（{config.ollama_model}）",
            "claude": "Claude",
        }
        engine_label = _engine_labels.get(config.generation_engine, "AI")

        status_placeholder = st.empty()
        status_placeholder.info(f"{engine_label} が議事録を生成中です…（1〜3分かかる場合があります）")

        _gen_start = time.time()
        _gen_success = False
        _gen_error = ""
        _gen_minutes_chars = 0
        _gen_model_override = st.session_state.get("gemini_gen_model_select", "（品質プリセット任せ）")
        _gen_model_override = None if (not _gen_model_override or _gen_model_override.startswith("（")) else _gen_model_override
        _gen_model = (
            (_gen_model_override or QUALITY_PRESETS[quality_preset]["model"])
            if config.generation_engine == "gemini"
            else (config.claude_model if config.generation_engine == "claude" else config.ollama_model)
        )
        try:
            template_content = load_template_content(
                config.templates_dir / templates[template_id]["file"]
            )

            # 過去議事録を読み込んでコンテキスト化
            reference_minutes_texts: list[str] = []
            if selected_refs:
                ref_filenames = [r.split(" - ", 1)[1] for r in selected_refs]
                for fn in ref_filenames:
                    try:
                        ref_path = config.output_dir / fn
                        reference_minutes_texts.append(
                            ref_path.read_text(encoding="utf-8")
                        )
                    except Exception:
                        pass

            # ===== キャッシュチェック =====
            mn_cache = get_default_cache()
            meeting_meta_str = (
                f"{meeting_date}|{meeting_location}|{attendees_note}|{template_id}"
            )
            mn_cache_key = mn_cache.hash_minutes(
                transcript=st.session_state["transcript"],
                template_content=template_content,
                quality_preset=quality_preset,
                meeting_meta=meeting_meta_str,
                custom_glossary=custom_glossary,
                project_names=project_names,
            )
            cached_minutes = mn_cache.get_minutes(mn_cache_key)
            _gen_from_cache = False

            if cached_minutes is not None:
                minutes = cached_minutes
                _gen_from_cache = True
                _gen_success = True
                _gen_minutes_chars = len(minutes)
                status_placeholder.empty()
                st.info(
                    f"✅ 議事録をキャッシュから復元しました "
                    f"（{len(minutes):,}文字、APIリクエスト消費なし）"
                )
            elif config.generation_engine == "gemini":
                def _update_status(msg: str):
                    status_placeholder.info(
                        f"{engine_label}（{QUALITY_PRESETS[quality_preset]['label']}）：{msg}"
                    )

                minutes = generate_minutes_gemini(
                    transcript=st.session_state["transcript"],
                    template=template_content,
                    meeting_date=str(meeting_date),
                    meeting_location=meeting_location,
                    attendees=attendees_note,
                    model=config.gemini_model,
                    model_override=_gen_model_override,
                    progress_callback=_update_status,
                    quality_preset=quality_preset,
                    reference_minutes=reference_minutes_texts or None,
                    custom_glossary=custom_glossary,
                    project_names=project_names,
                )
            elif config.generation_engine == "ollama":
                minutes = generate_minutes_ollama(
                    transcript=st.session_state["transcript"],
                    template=template_content,
                    meeting_date=str(meeting_date),
                    meeting_location=meeting_location,
                    attendees=attendees_note,
                    model=config.ollama_model,
                    base_url=config.ollama_base_url,
                    num_predict=config.ollama_num_predict,
                )
            else:  # claude
                minutes = generate_minutes(
                    transcript=st.session_state["transcript"],
                    template=template_content,
                    meeting_date=str(meeting_date),
                    meeting_location=meeting_location,
                    attendees=attendees_note,
                    model=config.claude_model,
                    max_tokens=config.claude_max_tokens,
                    quality_preset=quality_preset,
                    reference_minutes=reference_minutes_texts or None,
                    custom_glossary=custom_glossary,
                    project_names=project_names,
                )

            status_placeholder.empty()
            st.session_state["minutes"] = minutes

            # ユニークなファイル名で保存
            output_filename = generate_output_filename(
                template_id, str(meeting_date)
            )
            output_path = config.output_dir / output_filename
            output_path.write_text(minutes, encoding="utf-8")

            st.session_state["generated_filename"] = output_filename
            _gen_minutes_chars = len(minutes)
            _gen_success = True

            # ===== 議事録キャッシュへ保存（API呼び出し後のみ）=====
            if not _gen_from_cache:
                try:
                    mn_cache.save_minutes(
                        mn_cache_key, minutes,
                        source_label=f"{template_id} / {meeting_date} / {quality_preset}",
                    )
                except Exception:
                    pass

            # ===== 品質バリデーション結果を表示 =====
            validation = validate_minutes(
                minutes,
                template_content,
                transcript=st.session_state.get("transcript", ""),
            )
            transcript_text = st.session_state.get("transcript", "")
            volume_ratio = len(minutes) / max(len(transcript_text), 1)

            # 品質スコアカード
            q_cols = st.columns(3)
            with q_cols[0]:
                ratio_pct = int(volume_ratio * 100)
                if ratio_pct >= 70:
                    st.metric("📊 文字起こし保持率", f"{ratio_pct}%", "良好")
                elif ratio_pct >= 45:
                    st.metric("📊 文字起こし保持率", f"{ratio_pct}%", "やや低め")
                else:
                    st.metric("📊 文字起こし保持率", f"{ratio_pct}%", "⚠️ 要確認")
            with q_cols[1]:
                st.metric("📝 議事録文字数", f"{validation.minutes_length:,} 文字")
            with q_cols[2]:
                section_status = "✅ 全セクションOK" if validation.ok else f"⚠️ 欠落あり"
                st.metric("📋 必須セクション", section_status)

            if validation.ok and not validation.warnings and volume_ratio >= 0.5:
                st.success(f"✅ 議事録を保存しました：{output_filename}")
            else:
                st.success(f"議事録を保存しました：{output_filename}")
                if volume_ratio < 0.2:
                    st.error(
                        f"🚨 **品質警告：文字起こし保持率が {volume_ratio*100:.0f}% と極端に低いです。**\n\n"
                        "AIが要約モードで動作した可能性があります。\n"
                        "**「議事録を生成」をもう一度押して再生成してください。**\n"
                        "（同じ入力でも結果が変わることがあります）"
                    )
                elif volume_ratio < 0.45:
                    st.warning(
                        f"⚠️ 文字起こし保持率が {volume_ratio*100:.0f}% とやや低めです。\n"
                        "重要な発言が省略されている可能性があります。内容をご確認ください。"
                    )
                if validation.missing_sections:
                    st.warning(
                        "⚠️ 以下の必須セクションが見つかりませんでした："
                        + " / ".join(validation.missing_sections)
                        + "\n\n品質プリセットを「最高品質」に上げて再生成すると改善する可能性があります。"
                    )
                for w in validation.warnings:
                    st.warning(f"⚠️ {w}")
        except (ValueError, RuntimeError) as e:
            status_placeholder.empty()
            _gen_error = f"{type(e).__name__}: {str(e)[:300]}"
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                st.error(
                    "**Gemini APIの無料枠（毎分5リクエスト・1日20リクエスト）を超過しました。**\n\n"
                    "▶ **1〜2分待ってから「議事録を生成」を再クリック**してください。\n"
                    "▶ `⚖️ バランス` プリセット（gemini-2.5-flash）に切り替えると枠を節約できます。\n"
                    "▶ Pro モデルは1日の無料枠が少ないため、有料APIキーの取得も検討してください。"
                )
            elif "全モデルで失敗" in err_str:
                st.error(
                    "**すべてのAIモデルでエラーが発生しました。**\n\n"
                    "▶ APIキーが有効か確認してください。\n"
                    "▶ しばらく待ってから再試行してください。\n\n"
                    f"詳細: {err_str[:200]}"
                )
            else:
                st.error(err_str)
        except Exception as e:
            status_placeholder.empty()
            _gen_error = f"{type(e).__name__}: {str(e)[:300]}"
            st.error(
                f"議事録生成中に予期しないエラーが発生しました：{e}\n\n"
                "画面を更新して再試行してください。"
            )
        finally:
            try:
                log_generate(
                    success=_gen_success,
                    duration_sec=time.time() - _gen_start,
                    template_id=template_id,
                    quality_preset=quality_preset,
                    minutes_chars=_gen_minutes_chars,
                    transcript_chars=len(st.session_state.get("transcript", "")),
                    model=_gen_model,
                    error=_gen_error,
                )
            except Exception:
                pass

    # ===== 結果表示 =====
    if "minutes" in st.session_state:
        st.subheader("4. 生成結果")

        output_format = templates[template_id].get("output_format", "markdown")

        tab_preview, tab_source = st.tabs(["プレビュー", "ソース"])
        with tab_preview:
            if output_format == "markdown":
                st.markdown(st.session_state["minutes"])
            else:
                st.text(st.session_state["minutes"])
        with tab_source:
            st.code(
                st.session_state["minutes"],
                language="markdown" if output_format == "markdown" else "text",
            )

        # ===== ダウンロード（テキスト / Word / Excel） =====
        base_name = st.session_state.get(
            "generated_filename",
            f"議事録_{template_id}_{meeting_date}.md",
        )
        stem = base_name.rsplit(".", 1)[0]
        doc_title = f"{templates[template_id]['name']}（{meeting_date}）"

        # 会議メタ情報を組み立て（Word/Excelで完全活用）
        meeting_meta = MeetingMeta(
            template_name=templates[template_id]["name"],
            meeting_date=str(meeting_date),
            meeting_location=meeting_location or "",
            attendees_note=attendees_note or "",
        )

        st.markdown("**📥 議事録をダウンロード**")
        dl_col1, dl_col2, dl_col3 = st.columns(3)
        with dl_col1:
            st.download_button(
                "📝 テキスト（.md）",
                data=st.session_state["minutes"],
                file_name=base_name,
                mime="text/markdown"
                if output_format == "markdown"
                else "text/plain",
                use_container_width=True,
            )
        with dl_col2:
            try:
                docx_bytes = to_docx_bytes(
                    st.session_state["minutes"],
                    title=doc_title,
                    meta=meeting_meta,
                )
                st.download_button(
                    "📄 Word（.docx）",
                    data=docx_bytes,
                    file_name=f"{stem}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"Word変換に失敗：{e}")
        with dl_col3:
            try:
                xlsx_bytes = to_xlsx_bytes(
                    st.session_state["minutes"],
                    title=doc_title,
                    meta=meeting_meta,
                    transcript=st.session_state.get("transcript"),
                )
                st.download_button(
                    "📊 Excel（.xlsx）",
                    data=xlsx_bytes,
                    file_name=f"{stem}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"Excel変換に失敗：{e}")
        st.caption(
            "📄 Word: ヘッダー/フッター・基本情報の表化・自動採番・ページ番号付き　"
            "📊 Excel: カバー/基本情報/議論詳細/決定事項（タスク表）/文字起こし の5シート構成"
        )


# ===== タブ2: 過去の議事録 =====
with tab_archive:
    st.subheader("📚 過去の議事録")

    entries = list_minutes(config.output_dir)

    if not entries:
        st.info(
            "まだ議事録が保存されていません。"
            "「議事録生成」タブで新規作成してください。"
        )
    else:
        # ===== 全タグの一覧（先に集計） =====
        all_tags = collect_all_tags(config.output_dir, entries)

        # ===== 検索・フィルタUI =====
        with st.container(border=True):
            st.markdown("**🔍 検索・絞り込み**")
            filter_cols = st.columns([1, 2, 1])

            with filter_cols[0]:
                template_filter = st.selectbox(
                    "会議種別",
                    options=["すべて"] + list(templates.keys()),
                    format_func=lambda k: (
                        "すべて"
                        if k == "すべて"
                        else f"{k}: {templates[k]['name']}"
                    ),
                    key="archive_template_filter",
                )

            with filter_cols[1]:
                keyword = st.text_input(
                    "🔍 全文検索",
                    placeholder="議事録の中身を検索（例：勝瀬保育園、安全教育、決定事項）",
                    key="archive_keyword",
                )

            with filter_cols[2]:
                favorite_only = st.checkbox(
                    "⭐ お気に入りのみ",
                    key="archive_favorite_only",
                )

            sub_cols = st.columns([2, 2])
            with sub_cols[0]:
                tag_filter = st.multiselect(
                    "🏷️ タグで絞り込み",
                    options=all_tags,
                    key="archive_tag_filter",
                )
            with sub_cols[1]:
                date_filter_enabled = st.checkbox(
                    "📅 開催日の範囲で絞り込み",
                    key="archive_date_filter_enabled",
                )

            date_from = date_to = None
            if date_filter_enabled:
                date_cols = st.columns(2)
                with date_cols[0]:
                    df_raw = st.date_input(
                        "開始日",
                        value=None,
                        key="archive_date_from",
                    )
                with date_cols[1]:
                    dt_raw = st.date_input(
                        "終了日",
                        value=None,
                        key="archive_date_to",
                    )
                date_from = str(df_raw) if df_raw else None
                date_to = str(dt_raw) if dt_raw else None

        # ===== 絞り込み実行 =====
        filtered = filter_minutes(
            entries,
            template_id=template_filter,
            keyword=None,  # 全文検索は別関数で行う
            date_from=date_from,
            date_to=date_to,
        )

        # タグ・お気に入りフィルタ
        if favorite_only or tag_filter:
            filtered_with_meta = []
            for e in filtered:
                meta = load_metadata(config.output_dir, e.filename)
                if favorite_only and not meta.favorite:
                    continue
                if tag_filter and not any(t in meta.tags for t in tag_filter):
                    continue
                filtered_with_meta.append(e)
            filtered = filtered_with_meta

        # ===== キーワードがあれば全文検索（スニペット付き） =====
        if keyword and keyword.strip():
            search_hits = search_minutes(filtered, keyword.strip(), snippet_chars=80)
            displayed_entries = [h.entry for h in search_hits]
            hits_by_filename = {h.entry.filename: h for h in search_hits}
        else:
            displayed_entries = filtered
            hits_by_filename = {}

        # ===== サマリ＋ZIPエクスポート =====
        sum_col1, sum_col2, sum_col3 = st.columns([2, 1, 1])
        with sum_col1:
            st.caption(
                f"表示件数：{len(displayed_entries)} / {len(entries)} 件"
                + (f"　🔍 検索: 「{keyword}」" if keyword else "")
            )
        with sum_col2:
            if displayed_entries:
                try:
                    zip_md_only = export_minutes_as_zip(
                        displayed_entries, include_word=False, include_excel=False
                    )
                    st.download_button(
                        "📦 一括ZIP（MD）",
                        data=zip_md_only,
                        file_name=f"minutes_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
                        mime="application/zip",
                        key="archive_zip_md",
                        use_container_width=True,
                    )
                except Exception:
                    pass
        with sum_col3:
            if displayed_entries and len(displayed_entries) <= 30:
                try:
                    zip_full = export_minutes_as_zip(
                        displayed_entries, include_word=True, include_excel=True
                    )
                    st.download_button(
                        "📦 一括ZIP（MD+Word+Excel）",
                        data=zip_full,
                        file_name=f"minutes_full_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",
                        mime="application/zip",
                        key="archive_zip_full",
                        use_container_width=True,
                    )
                except Exception:
                    pass
            elif displayed_entries:
                st.caption("（30件超のため Word+Excel 一括は省略）")

        if not displayed_entries:
            st.info("条件に一致する議事録がありません")
        else:
            # ===== 一覧表示 =====
            for entry in displayed_entries:
                template_name = templates.get(entry.template_id, {}).get(
                    "name", "（不明テンプレート）"
                )
                entry_meta = load_metadata(config.output_dir, entry.filename)
                fav_marker = "⭐ " if entry_meta.favorite else ""
                tag_marker = (
                    "  🏷️ " + " ".join(f"#{t}" for t in entry_meta.tags)
                    if entry_meta.tags else ""
                )
                hit_count = (
                    hits_by_filename[entry.filename].hit_count
                    if entry.filename in hits_by_filename
                    else 0
                )
                hit_marker = f"  🎯 {hit_count}件ヒット" if hit_count > 0 else ""
                header_parts = [
                    f"{fav_marker}{entry.filename}",
                    template_name,
                    f"{entry.created_at:%Y-%m-%d %H:%M}",
                    f"{entry.size_kb:.1f} KB",
                ]
                header = "  ·  ".join(header_parts) + tag_marker + hit_marker

                with st.expander(header):
                    # ===== 検索ヒット時のスニペット表示 =====
                    if entry.filename in hits_by_filename:
                        hit = hits_by_filename[entry.filename]
                        st.markdown(
                            "**🎯 検索ヒット箇所（先頭から最大3件）**"
                        )
                        for i, snippet in enumerate(hit.snippets, start=1):
                            highlighted = highlight_snippet(snippet, keyword)
                            st.markdown(f"{i}. {highlighted}")
                        st.divider()
                    entry_output_format = templates.get(entry.template_id, {}).get(
                        "output_format", "markdown"
                    )

                    try:
                        content = entry.get_content()
                    except Exception as e:
                        st.error(f"ファイル読み込みに失敗しました：{e}")
                        continue

                    preview_tab, source_tab = st.tabs(["プレビュー", "ソース"])
                    with preview_tab:
                        if entry_output_format == "markdown":
                            st.markdown(content)
                        else:
                            st.text(content)
                    with source_tab:
                        st.code(
                            content,
                            language="markdown"
                            if entry_output_format == "markdown"
                            else "text",
                        )

                    # ===== ダウンロード（テキスト / Word / Excel） =====
                    archive_stem = entry.filename.rsplit(".", 1)[0]
                    archive_template_name = templates.get(entry.template_id, {}).get(
                        "name", entry.template_id
                    )
                    archive_title = f"{archive_template_name}（{entry.meeting_date}）"
                    archive_meta = MeetingMeta(
                        template_name=archive_template_name,
                        meeting_date=entry.meeting_date,
                    )
                    arch_col1, arch_col2, arch_col3 = st.columns(3)
                    with arch_col1:
                        st.download_button(
                            "📝 テキスト",
                            data=content,
                            file_name=entry.filename,
                            mime="text/markdown"
                            if entry_output_format == "markdown"
                            else "text/plain",
                            key=f"dl_md_{entry.filename}",
                            use_container_width=True,
                        )
                    with arch_col2:
                        try:
                            archive_docx = to_docx_bytes(
                                content, title=archive_title, meta=archive_meta
                            )
                            st.download_button(
                                "📄 Word",
                                data=archive_docx,
                                file_name=f"{archive_stem}.docx",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                key=f"dl_docx_{entry.filename}",
                                use_container_width=True,
                            )
                        except Exception as e:
                            st.error(f"Word変換失敗：{e}")
                    with arch_col3:
                        try:
                            archive_xlsx = to_xlsx_bytes(
                                content, title=archive_title, meta=archive_meta
                            )
                            st.download_button(
                                "📊 Excel",
                                data=archive_xlsx,
                                file_name=f"{archive_stem}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                key=f"dl_xlsx_{entry.filename}",
                                use_container_width=True,
                            )
                        except Exception as e:
                            st.error(f"Excel変換失敗：{e}")

                    # ===== タグ・お気に入り・メモ編集 =====
                    st.divider()
                    st.caption("🏷️ タグ・お気に入り")
                    tag_col1, tag_col2 = st.columns([3, 1])
                    with tag_col1:
                        new_tags_str = st.text_input(
                            "タグ（カンマ区切り）",
                            value=", ".join(entry_meta.tags),
                            key=f"tags_{entry.filename}",
                            placeholder="例: 重要, 進行中, 営業",
                            label_visibility="collapsed",
                        )
                    with tag_col2:
                        new_favorite = st.checkbox(
                            "⭐ お気に入り",
                            value=entry_meta.favorite,
                            key=f"fav_{entry.filename}",
                        )
                    new_memo = st.text_area(
                        "メモ（自分用の補足）",
                        value=entry_meta.memo,
                        key=f"memo_{entry.filename}",
                        height=70,
                        placeholder="例: 次回に向けて検討すべき事項を整理してから持ち込み",
                    )
                    if st.button(
                        "💾 タグ・メモを保存",
                        key=f"save_meta_{entry.filename}",
                    ):
                        new_tags = [
                            t.strip()
                            for t in new_tags_str.replace("、", ",").split(",")
                            if t.strip()
                        ]
                        save_metadata(
                            config.output_dir,
                            entry.filename,
                            MinutesMetadata(
                                tags=new_tags,
                                favorite=new_favorite,
                                memo=new_memo,
                            ),
                        )
                        st.success("保存しました（再読み込みで一覧に反映）")


# ===== タブ3: 統計ダッシュボード =====
with tab_stats:
    st.subheader("📊 統計ダッシュボード")

    run_logger = get_default_logger()
    stats = run_logger.stats()
    cache_mgr = get_default_cache()
    cache_stats = cache_mgr.stats()

    # ===== 今日のAPI使用量パネル =====
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_records = [
        r for r in run_logger.read_all()
        if r.timestamp.startswith(today_str) and r.success
    ]
    today_tx = sum(1 for r in today_records if r.kind == "transcribe")
    today_gen = sum(1 for r in today_records if r.kind == "generate")
    # 概算リクエスト数（チャンク数や2段階生成を考慮）
    today_api_estimate = sum(
        max(1, r.chunk_count) if r.kind == "transcribe"
        else 2  # 2段階生成は2リクエスト
        for r in today_records
    )

    st.markdown("### 🪙 今日のAPI使用量（無料枠管理）")
    api_cols = st.columns([1, 1, 1, 1])
    with api_cols[0]:
        st.metric("今日の文字起こし", f"{today_tx} 回")
    with api_cols[1]:
        st.metric("今日の議事録生成", f"{today_gen} 回")
    with api_cols[2]:
        st.metric(
            "API消費（推定）",
            f"≈{today_api_estimate} req",
            help="Gemini Free Tier の RPD 制限（モデルごとに約20req/日）への目安",
        )
    with api_cols[3]:
        remaining = max(0, 20 - today_api_estimate)
        st.metric(
            "Free 残量推定",
            f"≈{remaining} req",
            delta=("使い切り間近" if remaining <= 5 else "余裕あり"),
            delta_color=("inverse" if remaining <= 5 else "normal"),
        )
    if today_api_estimate >= 18:
        st.warning(
            "⚠️ 今日のAPIリクエストは無料枠（モデルごと20回/日）の上限に近づいています。\n"
            "**キャッシュの活用** または **明日まで待機** を推奨します。"
        )

    # ===== キャッシュ統計 =====
    st.markdown("### 💾 キャッシュ状態（API節約装置）")
    cc1, cc2, cc3, cc4 = st.columns([1, 1, 1, 1])
    with cc1:
        st.metric("文字起こしキャッシュ", f"{cache_stats['transcript_count']} 件")
    with cc2:
        st.metric("議事録キャッシュ", f"{cache_stats['minutes_count']} 件")
    with cc3:
        st.metric("総容量", f"{cache_stats['total_size_kb']:.1f} KB")
    with cc4:
        if st.button("🗑 キャッシュ全削除", key="clear_cache_btn"):
            tx, mn = cache_mgr.clear()
            st.success(f"削除しました：文字起こし {tx} 件、議事録 {mn} 件")
            st.rerun()
    st.caption(
        "同じ音声 / 同じ条件で再実行すると、キャッシュから即時取得しAPIを消費しません。"
        "テンプレート編集・再試行・出力フォーマット確認時に絶大な効果があります。"
    )

    st.divider()

    if stats["total"] == 0:
        st.info(
            "実行履歴がまだありません。"
            "文字起こし or 議事録生成を実行すると、ここに統計が表示されます。"
        )
    else:
        # ===== トップサマリ =====
        st.markdown("### 全体サマリ")
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.metric("総実行回数", f"{stats['total']:,}")
        with m2:
            tx_rate = (
                stats["transcribe_success"] / stats["transcribe_total"] * 100
                if stats["transcribe_total"] > 0 else 0
            )
            st.metric(
                "文字起こし成功率",
                f"{tx_rate:.0f}%",
                f"{stats['transcribe_success']}/{stats['transcribe_total']}",
            )
        with m3:
            gen_rate = (
                stats["generate_success"] / stats["generate_total"] * 100
                if stats["generate_total"] > 0 else 0
            )
            st.metric(
                "議事録生成成功率",
                f"{gen_rate:.0f}%",
                f"{stats['generate_success']}/{stats['generate_total']}",
            )
        with m4:
            st.metric(
                "総処理音声時間",
                f"{stats['total_audio_minutes']:.1f} 分",
            )

        # ===== 平均処理時間 =====
        st.markdown("### 平均処理時間")
        t1, t2 = st.columns(2)
        with t1:
            avg_tx = stats["avg_transcribe_sec"]
            st.metric(
                "文字起こし（1回あたり）",
                f"{avg_tx:.1f} 秒"
                + (f"  （{avg_tx/60:.1f} 分）" if avg_tx >= 60 else ""),
            )
        with t2:
            avg_gen = stats["avg_generate_sec"]
            st.metric(
                "議事録生成（1回あたり）",
                f"{avg_gen:.1f} 秒"
                + (f"  （{avg_gen/60:.1f} 分）" if avg_gen >= 60 else ""),
            )

        # ===== 利用統計 =====
        st.markdown("### 利用傾向")
        u1, u2 = st.columns(2)
        with u1:
            st.markdown("**📋 テンプレート別 議事録作成数**")
            if stats["template_usage"]:
                for tid, count in stats["template_usage"].items():
                    name = templates.get(tid, {}).get("name", "（不明）")
                    st.write(f"- **{tid}**: {name} — {count} 件")
            else:
                st.caption("（記録なし）")

            st.markdown("**🎚 文字起こし速度プリセット**")
            if stats["preset_usage"]:
                for preset, count in stats["preset_usage"].items():
                    st.write(f"- {preset} — {count} 回")
            else:
                st.caption("（記録なし）")

        with u2:
            st.markdown("**⚙️ 議事録 品質プリセット**")
            if stats.get("quality_usage"):
                for q, count in stats["quality_usage"].items():
                    st.write(f"- {q} — {count} 回")
            else:
                st.caption("（記録なし）")

            st.markdown("**🤖 利用モデル**")
            if stats["model_usage"]:
                for m, count in stats["model_usage"].items():
                    short = m.replace("models/", "")
                    st.write(f"- {short} — {count} 回")
            else:
                st.caption("（記録なし）")

        # ===== 日別推移 =====
        st.markdown("### 日別実行推移")
        if stats["by_day"]:
            try:
                import pandas as pd
                df = pd.DataFrame.from_dict(stats["by_day"], orient="index")
                df = df.sort_index()
                st.bar_chart(df)
            except Exception:
                # pandas が無い場合は素朴に
                for day, counts in list(stats["by_day"].items())[-30:]:
                    st.write(
                        f"- {day}: 文字起こし {counts['transcribe']} / "
                        f"議事録 {counts['generate']}"
                    )
        else:
            st.caption("（記録なし）")

        # ===== 最近のログ20件 =====
        with st.expander("📜 最近の実行履歴（直近20件）"):
            recent = list(reversed(run_logger.read_all()))[:20]
            try:
                import pandas as pd
                rows = []
                for r in recent:
                    rows.append({
                        "日時": r.timestamp.replace("T", " "),
                        "種別": "文字起こし" if r.kind == "transcribe" else "議事録生成",
                        "成功": "✅" if r.success else "❌",
                        "処理時間(s)": r.duration_sec,
                        "文字数": r.transcript_chars or r.minutes_chars,
                        "テンプレ/プリセット": (
                            r.template_id or r.speed_preset or r.quality_preset or ""
                        ),
                        "モデル": r.model.replace("models/", "") if r.model else "",
                    })
                if rows:
                    st.dataframe(rows, use_container_width=True, hide_index=True)
            except Exception:
                for r in recent:
                    st.write(
                        f"- {r.timestamp} [{r.kind}] "
                        f"{'OK' if r.success else 'NG'} "
                        f"{r.duration_sec:.1f}s "
                        f"{r.template_id or r.speed_preset or r.quality_preset}"
                    )


# ===== タブ4: テンプレート編集 =====
with tab_templates:
    st.subheader("⚙️ テンプレート編集")
    st.caption(
        "5種類の議事録テンプレートを直接編集できます。"
        "保存時に旧バージョンは `config/templates/_backup/` に自動退避されます。"
    )

    edit_template_id = st.selectbox(
        "編集するテンプレート",
        options=list(templates.keys()),
        format_func=lambda k: f"{k}: {templates[k]['name']}",
        key="template_editor_select",
    )

    template_path = config.templates_dir / templates[edit_template_id]["file"]
    try:
        current_content = template_path.read_text(encoding="utf-8")
    except Exception as e:
        st.error(f"テンプレート読み込み失敗：{e}")
        current_content = ""

    info_cols = st.columns(3)
    with info_cols[0]:
        st.metric("ファイル", templates[edit_template_id]["file"])
    with info_cols[1]:
        st.metric("文字数", f"{len(current_content):,}")
    with info_cols[2]:
        st.metric(
            "出力形式", templates[edit_template_id].get("output_format", "markdown")
        )

    edit_tab_main, edit_tab_preview, edit_tab_help = st.tabs(
        ["編集", "プレビュー", "ヘルプ"]
    )

    with edit_tab_main:
        edited = st.text_area(
            "テンプレート本文",
            value=current_content,
            height=600,
            key=f"template_editor_{edit_template_id}",
        )
        save_cols = st.columns([1, 1, 4])
        with save_cols[0]:
            if st.button("💾 保存", type="primary", key=f"save_template_{edit_template_id}"):
                if edited == current_content:
                    st.info("変更がありません")
                elif "{transcript}" not in edited:
                    st.error(
                        "❌ `{transcript}` プレースホルダが含まれていません。"
                        "文字起こしデータがプロンプトに渡らなくなるので、必ず含めてください。"
                    )
                else:
                    try:
                        # 自動バックアップ
                        backup_dir = config.templates_dir / "_backup"
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        backup_path = (
                            backup_dir
                            / f"{template_path.stem}_{ts}{template_path.suffix}"
                        )
                        backup_path.write_text(current_content, encoding="utf-8")
                        # 上書き保存
                        template_path.write_text(edited, encoding="utf-8")
                        st.success(
                            f"✅ 保存しました（旧版を `{backup_path.name}` にバックアップ）"
                        )
                    except Exception as e:
                        st.error(f"保存失敗：{e}")
        with save_cols[1]:
            if st.button("↩️ 元に戻す", key=f"reset_template_{edit_template_id}"):
                st.session_state[f"template_editor_{edit_template_id}"] = current_content
                st.rerun()

    with edit_tab_preview:
        st.markdown("**現在の保存内容（読み取り専用プレビュー）**")
        st.code(current_content, language="markdown")

    with edit_tab_help:
        st.markdown("""
**プレースホルダ（必ず含める）**:
- `{transcript}` … 文字起こしデータが挿入される（**必須**）
- `{metadata}` … サイドバーで入力した日時・場所・出席者が挿入される

**サンドイッチ構造（推奨）**:
1. 冒頭に **【最重要・絶対厳守ルール】** を3つ程度
2. 役割・詳細指示
3. 用語辞書
4. 出力フォーマット
5. Few-shot 例
6. `{metadata}` と `{transcript}`
7. 末尾に **【最終確認・再度の厳守事項】**

**注意**:
- このタブで保存した内容は次回の議事録生成からすぐ反映されます。
- 旧バージョンは `config/templates/_backup/` 配下に時刻付きで自動保存されます。
- LLMが「以下が議事録です」のような前置きを返す場合、出力フォーマットの末尾に「出力は議事録本文のみ」と明記してください。
        """)

    # ===== バックアップ一覧 =====
    backup_dir = config.templates_dir / "_backup"
    if backup_dir.exists():
        backups = sorted(
            [p for p in backup_dir.glob(f"{template_path.stem}_*{template_path.suffix}")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if backups:
            with st.expander(f"🗂 過去のバックアップ（{len(backups)}件）"):
                for b in backups[:20]:
                    cols = st.columns([3, 1, 1])
                    cols[0].caption(
                        f"{b.name}　({datetime.fromtimestamp(b.stat().st_mtime):%Y-%m-%d %H:%M})"
                    )
                    cols[1].download_button(
                        "ダウンロード",
                        data=b.read_bytes(),
                        file_name=b.name,
                        mime="text/markdown",
                        key=f"dl_backup_{b.name}",
                        use_container_width=True,
                    )
                    if cols[2].button(
                        "↩️ 復元",
                        key=f"restore_backup_{b.name}",
                        use_container_width=True,
                    ):
                        try:
                            template_path.write_text(
                                b.read_text(encoding="utf-8"), encoding="utf-8"
                            )
                            st.success(f"✅ {b.name} の内容を復元しました")
                            st.rerun()
                        except Exception as e:
                            st.error(f"復元失敗：{e}")
