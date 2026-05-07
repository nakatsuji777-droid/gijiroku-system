"""実行ログ管理 — 文字起こし・議事録生成の履歴をJSONLで永続化し、統計集計する。

ログファイル: output/_runlog/runlog.jsonl
"""
from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class RunRecord:
    timestamp: str          # ISO形式
    kind: str               # "transcribe" | "generate"
    success: bool
    duration_sec: float
    # 任意（kind 別）
    audio_filename: str = ""
    audio_duration_min: float = 0.0
    chunk_count: int = 0
    failed_chunks: int = 0
    transcript_chars: int = 0
    speed_preset: str = ""
    template_id: str = ""
    quality_preset: str = ""
    minutes_chars: int = 0
    model: str = ""
    error: str = ""


class RunLogger:
    """JSON Lines形式のログをスレッドセーフに追記する。"""

    _lock = threading.Lock()

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / "runlog.jsonl"

    def append(self, record: RunRecord) -> None:
        with RunLogger._lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def read_all(self) -> list[RunRecord]:
        if not self.log_path.exists():
            return []
        records: list[RunRecord] = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    records.append(RunRecord(**data))
                except Exception:
                    continue
        return records

    def stats(self) -> dict:
        """統計サマリを計算する。"""
        records = self.read_all()
        total = len(records)
        if total == 0:
            return {
                "total": 0,
                "transcribe_total": 0,
                "generate_total": 0,
                "transcribe_success": 0,
                "generate_success": 0,
                "avg_transcribe_sec": 0,
                "avg_generate_sec": 0,
                "total_audio_minutes": 0,
                "preset_usage": {},
                "template_usage": {},
                "model_usage": {},
                "by_day": {},
            }

        transcribe = [r for r in records if r.kind == "transcribe"]
        generate = [r for r in records if r.kind == "generate"]

        def avg(seq):
            seq = [s for s in seq if s > 0]
            return sum(seq) / len(seq) if seq else 0

        # 日別集計
        by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"transcribe": 0, "generate": 0})
        for r in records:
            day = r.timestamp[:10]
            by_day[day][r.kind] += 1

        return {
            "total": total,
            "transcribe_total": len(transcribe),
            "generate_total": len(generate),
            "transcribe_success": sum(1 for r in transcribe if r.success),
            "generate_success": sum(1 for r in generate if r.success),
            "avg_transcribe_sec": avg([r.duration_sec for r in transcribe]),
            "avg_generate_sec": avg([r.duration_sec for r in generate]),
            "total_audio_minutes": sum(r.audio_duration_min for r in transcribe),
            "preset_usage": dict(Counter(
                r.speed_preset for r in transcribe if r.speed_preset
            ).most_common()),
            "template_usage": dict(Counter(
                r.template_id for r in generate if r.template_id
            ).most_common()),
            "model_usage": dict(Counter(
                r.model for r in records if r.model
            ).most_common()),
            "quality_usage": dict(Counter(
                r.quality_preset for r in generate if r.quality_preset
            ).most_common()),
            "by_day": dict(sorted(by_day.items())),
        }


# 既定のグローバルロガー（プロジェクト直下の output/_runlog）
_default_logger: Optional[RunLogger] = None
_default_lock = threading.Lock()


def get_default_logger() -> RunLogger:
    global _default_logger
    if _default_logger is None:
        with _default_lock:
            if _default_logger is None:
                project_root = Path(__file__).parent.parent
                _default_logger = RunLogger(project_root / "output" / "_runlog")
    return _default_logger


def log_transcribe(
    success: bool,
    duration_sec: float,
    audio_filename: str = "",
    audio_duration_min: float = 0.0,
    chunk_count: int = 0,
    failed_chunks: int = 0,
    transcript_chars: int = 0,
    speed_preset: str = "",
    model: str = "",
    error: str = "",
) -> None:
    """文字起こし1回の実行記録を追加する。"""
    try:
        rec = RunRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            kind="transcribe",
            success=success,
            duration_sec=round(duration_sec, 2),
            audio_filename=audio_filename,
            audio_duration_min=round(audio_duration_min, 1),
            chunk_count=chunk_count,
            failed_chunks=failed_chunks,
            transcript_chars=transcript_chars,
            speed_preset=speed_preset,
            model=model,
            error=error[:500] if error else "",
        )
        get_default_logger().append(rec)
    except Exception:
        # ロギング自体は失敗しても本処理は止めない
        pass


def log_generate(
    success: bool,
    duration_sec: float,
    template_id: str = "",
    quality_preset: str = "",
    minutes_chars: int = 0,
    transcript_chars: int = 0,
    model: str = "",
    error: str = "",
) -> None:
    """議事録生成1回の実行記録を追加する。"""
    try:
        rec = RunRecord(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            kind="generate",
            success=success,
            duration_sec=round(duration_sec, 2),
            template_id=template_id,
            quality_preset=quality_preset,
            minutes_chars=minutes_chars,
            transcript_chars=transcript_chars,
            model=model,
            error=error[:500] if error else "",
        )
        get_default_logger().append(rec)
    except Exception:
        pass
