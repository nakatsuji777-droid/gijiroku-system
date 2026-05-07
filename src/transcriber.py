"""音声文字起こし（Gemini 高速安定版：音声前処理 + パイプライン並列 + 自動フォールバック）"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Callable, Optional


def _safe_progress(callback: Optional[Callable[[float, str], None]],
                   ratio: float, status: str) -> None:
    """進捗コールバックを安全に呼び出す。
    Streamlit の ScriptRunContext エラー等でアプリが落ちないようにする。"""
    if callback is None:
        return
    try:
        callback(ratio, status)
    except Exception as e:
        logger.warning(f"progress_callback エラー（無視）: {type(e).__name__}: {e}")


# ===== ロギング =====
_LOG_PATH = Path(__file__).parent.parent / "transcriber_debug.log"
logger = logging.getLogger("transcriber")
if not logger.handlers:
    handler = logging.FileHandler(str(_LOG_PATH), encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ========== 速度プリセット ==========
# Gemini 2.5 Flash の Free Tier は 5 RPM（毎分5リクエスト）。
# 細かく分割すると即座にクォータ超過 → 429 エラー連発。
# 「大きく分けて少なく叩く」ことで、リクエスト数を最小化する。
# 25〜30分チャンクなら 1時間音声でも 2〜3リクエストで完了。
SPEED_PRESETS = {
    "fastest": {
        "chunk_minutes": 30,   # 30分チャンク。前処理後60分音声 → 2リクエスト
        "atempo": 1.5,
        "silence_remove": True,
        "max_parallel": 2,
    },
    "balanced": {
        "chunk_minutes": 25,   # 25分チャンク。前処理後45分音声 → 2リクエスト
        "atempo": 1.2,
        "silence_remove": True,
        "max_parallel": 2,
    },
    "quality": {
        "chunk_minutes": 20,   # 20分チャンク。安全マージン重視
        "atempo": 1.0,
        "silence_remove": False,
        "max_parallel": 1,
    },
}

DEFAULT_PRESET = "balanced"

# inline_data として送れる上限（実用上 18MB くらいで切る）
INLINE_DATA_MAX_BYTES = 18 * 1024 * 1024

# ===== Gemini Free Tier レート制限 =====
# 公式: gemini-2.5-flash の Free Tier は 5〜10 RPM（保守側で 5 を採用）。
# 別モデル（flash-latest 等）は別カウントなのでフォールバック用に温存。
GEMINI_RPM_LIMIT = 5
RATE_WINDOW_SEC = 60.0


class RateLimiter:
    """
    トークンバケット式のレートリミッター。
    モデル別に「直近 RATE_WINDOW_SEC 秒で何リクエスト発行したか」を記録し、
    上限に達していたら最古のリクエストが期限切れになるまで sleep する。
    """

    def __init__(self, max_per_window: int, window_sec: float):
        self.max_per_window = max_per_window
        self.window_sec = window_sec
        self._stamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, model: str) -> None:
        while True:
            with self._lock:
                now = time.time()
                stamps = self._stamps.setdefault(model, [])
                # 期限切れを除去
                cutoff = now - self.window_sec
                while stamps and stamps[0] < cutoff:
                    stamps.pop(0)
                if len(stamps) < self.max_per_window:
                    stamps.append(now)
                    return
                # 最古のリクエストが期限切れになるまでの待ち時間
                wait_sec = stamps[0] + self.window_sec - now + 0.5
            logger.info(
                f"[RateLimiter] {model}: {self.max_per_window} req/min 到達。"
                f"{wait_sec:.1f}秒待機"
            )
            time.sleep(max(0.1, wait_sec))

    def penalize(self, model: str, retry_after_sec: float) -> None:
        """
        429 を受けたとき、サーバーが指示する retryDelay 分はそのモデルへのアクセスを
        強制的に待たせる（直近に max_per_window 件分のスタンプを今+retry_after に詰める）。
        """
        with self._lock:
            now = time.time()
            stamps = self._stamps.setdefault(model, [])
            # 既存スタンプを「retry_after 後に期限切れ」になる時刻に置き換える
            target = now + retry_after_sec - self.window_sec
            stamps[:] = [target] * self.max_per_window


_rate_limiter = RateLimiter(GEMINI_RPM_LIMIT, RATE_WINDOW_SEC)


def _parse_retry_delay(error_str: str) -> Optional[float]:
    """429/503 エラーから retryDelay を抽出（秒）。失敗したら None。"""
    # 形式1: "retryDelay': '40s'" / "retryDelay\": \"40s\""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)\s*s", error_str)
    if m:
        return float(m.group(1))
    # 形式2: "retry in 44.595s" / "Please retry in 39.776s"
    m = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", error_str)
    if m:
        return float(m.group(1))
    return None


TRANSCRIPTION_PROMPT = """あなたは日本語音声の専門文字起こしアシスタントです。
この会議音声を、一言一句漏らさず、できる限り忠実に日本語で書き起こしてください。

【絶対厳守】
- 発言を要約・省略・圧縮しては絶対にいけません
- 相槌（「はい」「ええ」「なるほど」など）であっても、話の流れに関わる応答は残してください
- すべての発言者の発言を時系列で、冒頭から最後まですべて記録してください
- 「えー」「あのー」などの明らかな言い淀みは削ってよいが、発言内容そのものは絶対に削らない
- 聞き取れない箇所のみ [不明瞭] と記載

【形式】
- 話者が変わるごとに改行
- 話者名がわかる場合は「話者A：」「話者B：」のように先頭に付ける（わからなければ付けなくてよい）
- 余計な前置き・後書き・説明・見出しは一切不要。文字起こし本文のみを出力

【重要】
出力が長くなっても構いません。最初から最後まで完全に書き起こしてください。
途中で「以下省略」「要約」といった書き方は絶対にしないでください。
"""


def _format_exc(e: Exception) -> str:
    """例外を必ず読める文字列に変換する。"""
    type_name = type(e).__name__
    msg = str(e).strip()
    if not msg:
        # SDK が空エラーを返した場合、属性も総動員する
        attrs = []
        for attr in ("message", "details", "code", "reason", "response"):
            val = getattr(e, attr, None)
            if val:
                attrs.append(f"{attr}={val}")
        msg = "; ".join(attrs) if attrs else "<メッセージなし>"
    return f"[{type_name}] {msg}"


def _get_ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_duration_seconds(ffmpeg: str, audio_path: Path) -> float:
    result = subprocess.run(
        [ffmpeg, "-i", str(audio_path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError(
            "音声ファイルの長さを取得できませんでした。ファイル形式をご確認ください。"
        )
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def _preprocess_full_audio(
    ffmpeg: str,
    src: Path,
    dst: Path,
    atempo: float,
    silence_remove: bool,
) -> None:
    """元音声全体を最適化（無音除去 + 倍速化 + モノラル + 適度な圧縮）。"""
    filters = []
    if silence_remove:
        filters.append(
            "silenceremove="
            "start_periods=1:start_duration=0.3:start_threshold=-35dB:"
            "stop_periods=-1:stop_duration=0.5:stop_threshold=-35dB"
        )
    if atempo and abs(atempo - 1.0) > 0.01:
        # atempo は 0.5〜2.0 の範囲。範囲外は連結
        remaining = atempo
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        if abs(remaining - 1.0) > 0.01:
            filters.append(f"atempo={remaining:.3f}")

    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(src),
        "-ac", "1",        # モノラル
        "-ar", "16000",    # 16kHz（音声認識の標準）
        "-b:a", "48k",     # 48kbps（音質と速度の妥協点）
        "-vn",
    ]
    if filters:
        cmd.extend(["-af", ",".join(filters)])
    cmd.append(str(dst))

    logger.info(f"前処理コマンド: {' '.join(cmd)}")
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 音声前処理に失敗：{result.stderr[:500]}")


def _extract_chunk(
    ffmpeg: str,
    src: Path,
    out: Path,
    start_sec: float,
    duration_sec: float,
) -> None:
    """前処理済み音声からチャンクを切り出す（再エンコードして確実に独立した有効MP3にする）。"""
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-ss", f"{start_sec:.3f}",
        "-i", str(src),
        "-t", f"{duration_sec:.3f}",
        "-ac", "1", "-ar", "16000", "-b:a", "48k", "-vn",
        str(out),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg チャンク切り出し失敗：{result.stderr[:500]}")


def _transcribe_chunk_inline(client, chunk_path: Path, model: str) -> str:
    """inline_data 方式（高速）でチャンクを送信。RateLimiter経由。"""
    from google.genai import types

    audio_bytes = chunk_path.read_bytes()
    if len(audio_bytes) > INLINE_DATA_MAX_BYTES:
        raise RuntimeError(
            f"チャンクサイズ {len(audio_bytes) / 1024 / 1024:.1f}MB が "
            f"inline_data 上限 {INLINE_DATA_MAX_BYTES / 1024 / 1024:.0f}MB を超過"
        )

    _rate_limiter.acquire(model)
    response = client.models.generate_content(
        model=model,
        contents=[
            TRANSCRIPTION_PROMPT,
            types.Part.from_bytes(data=audio_bytes, mime_type="audio/mpeg"),
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=32768,
        ),
    )
    text = response.text or ""
    if not text.strip():
        raise RuntimeError(
            "Gemini が空のレスポンスを返しました（音声を認識できなかった可能性）"
        )
    return text


def _transcribe_chunk_files_api(client, chunk_path: Path, model: str) -> str:
    """Files API 方式（チャンクが18MB超のとき、または inline で謎の失敗をしたときに使用）。"""
    from google.genai import types

    uploaded = client.files.upload(file=str(chunk_path))
    wait = 0
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        wait += 2
        if wait > 120:
            raise RuntimeError("Gemini Files API の前処理がタイムアウト")
        uploaded = client.files.get(name=uploaded.name)
    if uploaded.state.name != "ACTIVE":
        raise RuntimeError(
            f"Files API: 音声処理失敗（状態：{uploaded.state.name}）"
        )
    try:
        _rate_limiter.acquire(model)
        response = client.models.generate_content(
            model=model,
            contents=[TRANSCRIPTION_PROMPT, uploaded],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=32768,
            ),
        )
        text = response.text or ""
        if not text.strip():
            raise RuntimeError("Files API: Gemini が空のレスポンスを返しました")
        return text
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass


def _is_rate_limit_error(err_str: str) -> bool:
    return "429" in err_str or "RESOURCE_EXHAUSTED" in err_str


def _is_server_busy_error(err_str: str) -> bool:
    return "503" in err_str or "UNAVAILABLE" in err_str


def _try_one_attempt(client, chunk_path: Path, model: str, use_files_api: bool) -> str:
    if use_files_api:
        return _transcribe_chunk_files_api(client, chunk_path, model)
    return _transcribe_chunk_inline(client, chunk_path, model)


def _transcribe_chunk_with_fallback(
    client,
    chunk_path: Path,
    primary_model: str,
    fallback_models: list,
    chunk_label: str = "",
) -> str:
    """
    レートリミッター対応の多段フォールバック。

    戦略:
    - モデルごとに RateLimiter を通すので 5 RPM を超えない（待ち時間で自然調整）
    - 429 を受けたら retryDelay に従って当該モデルを penalize し、次のモデルへ
    - 503 はサーバー側混雑なので retryDelay or 30秒待機して同モデルでリトライ
    - inline_data → Files API のフォールバックは「クォータ消費しない場合のみ」
      （ファイルサイズ超過・空応答・接続エラー時のみ）

    試行順:
    1. primary inline
    2. primary Files API（inlineが「サイズ超過」「空応答」のとき）
    3. fallback_models 各々で inline
    """
    models_to_try = [primary_model] + [m for m in fallback_models if m != primary_model]
    last_err: Optional[Exception] = None

    for current_model in models_to_try:
        # ファイルサイズが大きいなら最初から Files API
        chunk_size = chunk_path.stat().st_size
        prefer_files_api = chunk_size > INLINE_DATA_MAX_BYTES
        skip_to_next_model = False

        for use_files_api in ([True] if prefer_files_api else [False, True]):
            method_name = "Files API" if use_files_api else "inline"
            # 503 を受けたら最大2回まで同モデルでリトライ
            for server_retry in range(3):
                try:
                    logger.info(
                        f"{chunk_label} 試行: {current_model} ({method_name})"
                        + (f" [503リトライ {server_retry}]" if server_retry > 0 else "")
                    )
                    return _try_one_attempt(client, chunk_path, current_model, use_files_api)

                except Exception as e:
                    last_err = e
                    err_str = str(e)
                    logger.warning(
                        f"{chunk_label} {current_model} {method_name} 失敗: {_format_exc(e)}"
                    )

                    if _is_rate_limit_error(err_str):
                        # 429 → このモデルのクォータ枯渇。inline でも Files API でも
                        # 同じクォータを使うので、別モデルへ即座に移る。
                        retry_after = _parse_retry_delay(err_str) or 45.0
                        logger.info(
                            f"{chunk_label} {current_model} レート制限。{retry_after:.0f}秒"
                            f"封印して次モデルへ"
                        )
                        _rate_limiter.penalize(current_model, retry_after)
                        skip_to_next_model = True
                        break

                    if _is_server_busy_error(err_str):
                        # 503 → サーバー側混雑。retryDelay or 30秒待ってリトライ
                        wait = _parse_retry_delay(err_str) or 30.0
                        logger.info(
                            f"{chunk_label} {current_model} {method_name} サーバー混雑。"
                            f"{wait:.0f}秒待機後リトライ"
                        )
                        time.sleep(min(wait, 60))
                        continue

                    # その他のエラー（空応答・サイズ超過・接続エラー等）
                    # → この方式は諦め、同モデルの別方式 or 次モデルへ
                    break

            if skip_to_next_model:
                break  # 外側 for を抜けて次のモデルへ
        # 次のモデルへ

    raise RuntimeError(
        f"全モデル・全方式で失敗。最終エラー: {_format_exc(last_err)}"
        if last_err else "全モデル・全方式で失敗（詳細不明）"
    )


def transcribe_audio_gemini(
    audio_path: Path,
    model: str = "models/gemini-2.5-flash",
    progress_callback: Optional[Callable[[float, str], None]] = None,
    speed_preset: str = DEFAULT_PRESET,
) -> str:
    """
    Gemini API で音声を文字起こしする（高速安定版）。

    最適化:
    - 音声前処理（無音除去 + 控えめな倍速化）で実時間を短縮
    - inline_data 主体で速度最大化、不調時は自動 Files API フォールバック
    - 6並列でパイプライン実行
    - 多段モデル＆方式フォールバックで失敗を最小化
    - 全失敗ログを transcriber_debug.log に記録
    """
    from google import genai

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY が設定されていません。\n"
            "https://aistudio.google.com/apikey で無料取得してください。"
        )

    preset = SPEED_PRESETS.get(speed_preset, SPEED_PRESETS[DEFAULT_PRESET])
    chunk_minutes = preset["chunk_minutes"]
    atempo = preset["atempo"]
    silence_remove = preset["silence_remove"]
    max_parallel = preset["max_parallel"]
    primary_model = model  # 主モデルは常に config 指定（lite を勝手に使わない）

    logger.info(f"=== 文字起こし開始: preset={speed_preset}, model={primary_model} ===")
    logger.info(f"音声ファイル: {audio_path} (size={audio_path.stat().st_size} bytes)")

    client = genai.Client(api_key=api_key)
    ffmpeg = _get_ffmpeg_exe()

    _safe_progress(progress_callback, 0.01, f"音声ファイルを解析中…（プリセット：{speed_preset}）")

    original_sec = _probe_duration_seconds(ffmpeg, audio_path)
    original_min = original_sec / 60.0
    logger.info(f"元音声長: {original_min:.1f}分")

    # 音声入力に対応している既知の安定モデル（lite は除外）
    fallback_models = [
        "models/gemini-flash-latest",
        "models/gemini-2.5-flash",
    ]

    def _fmt(sec: float) -> str:
        mm = int(sec // 60)
        ss = int(sec % 60)
        return f"{mm:02d}:{ss:02d}"

    # Windows では with tempfile.TemporaryDirectory が cleanup で
    # PermissionError を投げることがあるため、手動管理にする。
    tmpdir = tempfile.mkdtemp(prefix="audio_chunks_")
    tmpdir_path = Path(tmpdir)

    try:
        # ===== ステップ0: 音声全体を最適化 =====
        preprocessed_path = tmpdir_path / "preprocessed.mp3"
        preprocess_msg = []
        if silence_remove:
            preprocess_msg.append("無音除去")
        if abs(atempo - 1.0) > 0.01:
            preprocess_msg.append(f"{atempo}倍速化")
        preprocess_msg.append("48kbpsモノラル化")
        _safe_progress(
            progress_callback, 0.03,
            f"音声前処理中…（{' + '.join(preprocess_msg)}）",
        )

        _preprocess_full_audio(
            ffmpeg, audio_path, preprocessed_path,
            atempo=atempo, silence_remove=silence_remove,
        )

        processed_sec = _probe_duration_seconds(ffmpeg, preprocessed_path)
        processed_min = processed_sec / 60.0
        compression_ratio = (
            (1 - processed_sec / original_sec) * 100 if original_sec > 0 else 0
        )
        logger.info(f"前処理後: {processed_min:.1f}分 ({compression_ratio:.0f}%短縮)")

        chunk_sec = chunk_minutes * 60
        num_chunks = max(1, int((processed_sec + chunk_sec - 1) // chunk_sec))
        logger.info(f"チャンク数: {num_chunks}, 並列数: {max_parallel}")

        _safe_progress(
            progress_callback, 0.10,
            f"前処理完了（{original_min:.1f}分 → {processed_min:.1f}分、{compression_ratio:.0f}%短縮）"
            f" / {num_chunks}チャンク × {max_parallel}並列",
        )

        # ===== ステップ1: チャンク仕様を準備 =====
        chunk_meta = []
        for i in range(num_chunks):
            start_sec = i * chunk_sec
            duration_sec = min(chunk_sec, processed_sec - start_sec)
            if duration_sec <= 0:
                continue
            time_range = f"{_fmt(start_sec)}〜{_fmt(start_sec + duration_sec)}"
            chunk_meta.append((i, start_sec, duration_sec, time_range))

        results: dict[int, str] = {}
        failed: dict[int, str] = {}
        state_lock = threading.Lock()
        completed = {"split": 0, "transcribe": 0}

        # ===== ステップ2: パイプライン実行 =====
        ready_queue: Queue = Queue()
        SENTINEL = object()

        def _split_worker(meta):
            idx, start, dur, trange = meta
            chunk_path = tmpdir_path / f"chunk_{idx:03d}.mp3"
            try:
                _extract_chunk(ffmpeg, preprocessed_path, chunk_path, start, dur)
                size_kb = chunk_path.stat().st_size / 1024
                logger.info(f"チャンク {idx + 1} 切り出し完了 ({trange}, {size_kb:.0f}KB)")
                ready_queue.put((idx, chunk_path, trange))
                with state_lock:
                    completed["split"] += 1
            except Exception as e:
                logger.error(f"チャンク {idx + 1} 切り出し失敗: {_format_exc(e)}")
                with state_lock:
                    failed[idx] = f"切り出し失敗: {_format_exc(e)}"
                ready_queue.put((idx, None, trange))

        def _transcribe_worker(item):
            idx, chunk_path, trange = item
            chunk_label = f"チャンク{idx + 1}({trange})"
            if chunk_path is None:
                return
            try:
                text = _transcribe_chunk_with_fallback(
                    client=client,
                    chunk_path=chunk_path,
                    primary_model=primary_model,
                    fallback_models=fallback_models,
                    chunk_label=chunk_label,
                )
                with state_lock:
                    results[idx] = text.strip()
                logger.info(f"{chunk_label} 文字起こし成功 ({len(text)}文字)")
            except Exception as e:
                err_detail = _format_exc(e)
                logger.error(f"{chunk_label} 全試行失敗: {err_detail}")
                logger.error(traceback.format_exc())
                with state_lock:
                    failed[idx] = err_detail
            finally:
                with state_lock:
                    completed["transcribe"] += 1
                    sp = completed["split"]
                    tc = completed["transcribe"]
                    fl = len(failed)
                _safe_progress(
                    progress_callback,
                    0.10 + (tc / num_chunks) * 0.88,
                    f"切り出し {sp}/{num_chunks}・文字起こし {tc}/{num_chunks}"
                    + (f"（残り {num_chunks - tc} 並列処理中）" if tc < num_chunks else "")
                    + (f" ※失敗 {fl}" if fl else ""),
                )

        split_executor = ThreadPoolExecutor(
            max_workers=min(num_chunks, 4), thread_name_prefix="split"
        )
        split_futures = [split_executor.submit(_split_worker, m) for m in chunk_meta]

        def _watcher():
            for fut in as_completed(split_futures):
                pass
            for _ in range(max_parallel):
                ready_queue.put(SENTINEL)

        watcher_thread = threading.Thread(target=_watcher, daemon=True)
        watcher_thread.start()

        def _consumer_loop():
            while True:
                item = ready_queue.get()
                if item is SENTINEL:
                    break
                _transcribe_worker(item)

        with ThreadPoolExecutor(
            max_workers=max_parallel, thread_name_prefix="transcribe"
        ) as consumer_executor:
            consumer_futures = [
                consumer_executor.submit(_consumer_loop)
                for _ in range(max_parallel)
            ]
            for f in consumer_futures:
                f.result()

        split_executor.shutdown(wait=True)
        watcher_thread.join(timeout=5)

        # ===== ステップ3: 時系列順に結合 =====
        transcripts: list[str] = []
        for idx, _start, _dur, trange in chunk_meta:
            if idx in results:
                transcripts.append(results[idx])
            else:
                err = failed.get(idx, "原因不明")
                transcripts.append(
                    f"[{trange} のセグメントは文字起こしに失敗しました：{err}]"
                )

        final_text = "\n\n".join(t for t in transcripts if t)

        # ===== ステップ4: 必ずバックアップを残す =====
        # Streamlit セッション切断や cleanup 例外で transcript が消えても、
        # ここに保存されていれば失われない。
        try:
            backup_dir = Path(__file__).parent.parent / "output" / "_transcripts"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stem = audio_path.stem[:60]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"{timestamp}_{stem}.txt"
            backup_path.write_text(final_text, encoding="utf-8")
            logger.info(f"文字起こしバックアップ保存: {backup_path}")
        except Exception as e:
            logger.warning(f"バックアップ保存失敗（無視）: {_format_exc(e)}")

        if failed:
            failed_list = ",".join(map(str, sorted(k + 1 for k in failed.keys())))
            _safe_progress(
                progress_callback, 1.0,
                f"完了（{len(failed)}チャンク失敗：No.{failed_list}）",
            )
        else:
            _safe_progress(progress_callback, 1.0, "文字起こしが完了しました")

        logger.info(
            f"=== 文字起こし終了: 成功 {len(results)}/{len(chunk_meta)}、"
            f"合計 {len(final_text)} 文字 ==="
        )
        return final_text

    finally:
        # 一時ディレクトリは Windows でロックされていることがあるため、
        # 削除に失敗してもエラーにしない（最悪 OS の Temp クリーナーが回収する）。
        try:
            shutil.rmtree(tmpdir_path, ignore_errors=True)
            logger.info(f"一時ディレクトリ削除: {tmpdir_path}")
        except Exception as e:
            logger.warning(f"一時ディレクトリ削除失敗（無視）: {_format_exc(e)}")


def transcribe_audio(
    audio_path: Path,
    model_size: str = "medium",
    device: str = "cpu",
    compute_type: str = "int8",
    progress_callback: Optional[Callable[[float, str], None]] = None,
) -> str:
    """faster-whisper で音声をローカル文字起こしする（オフライン可・CPUだと遅い）。"""
    from faster_whisper import WhisperModel

    if progress_callback:
        progress_callback(0.0, f"Whisperモデル（{model_size}）を読み込み中…")

    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    if progress_callback:
        progress_callback(0.1, "音声解析を開始します…")

    segments, info = model.transcribe(
        str(audio_path),
        language="ja",
        beam_size=3,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    total_duration = info.duration if info and info.duration else 0.0

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text.strip())

        if progress_callback and total_duration > 0:
            ratio = min(segment.end / total_duration, 1.0)
            progress_callback(
                0.1 + ratio * 0.9,
                f"文字起こし中… {segment.end:.1f}秒 / {total_duration:.1f}秒",
            )

    if progress_callback:
        progress_callback(1.0, "文字起こしが完了しました")

    return "\n".join(text_parts)
