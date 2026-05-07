"""コンテンツハッシュベースのキャッシュ管理。

無料APIクォータ（特にGemini RPD=20/日）を最大限活用するため、
同じ入力に対する API 呼び出しを徹底的に省く。

格納場所:
- output/_cache/transcripts/{hash}.txt   …音声 + speed_preset → 文字起こしテキスト
- output/_cache/minutes/{hash}.txt       …transcript + template + preset → 議事録Markdown
- output/_cache/index.json               …キャッシュメタ（作成日時・元ファイル名等）
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class CacheEntry:
    """キャッシュ1件のメタ情報。"""
    kind: str            # "transcript" or "minutes"
    key: str             # ハッシュ
    created_at: str
    char_count: int
    source_label: str = ""    # 元音声ファイル名 / 議事録の説明


class CacheManager:
    """スレッドセーフな単純ファイルキャッシュ。"""

    _lock = threading.Lock()

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.transcripts_dir = self.base_dir / "transcripts"
        self.minutes_dir = self.base_dir / "minutes"
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.minutes_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "index.json"

    # =========================================================
    # ハッシュ計算
    # =========================================================

    @staticmethod
    def hash_audio(audio_bytes: bytes, speed_preset: str, model: str) -> str:
        """音声ファイル＋設定のハッシュ（同じ音声＋同じ条件なら同じキー）"""
        h = hashlib.sha256()
        h.update(audio_bytes)
        h.update(b"|preset:")
        h.update(speed_preset.encode("utf-8"))
        h.update(b"|model:")
        h.update(model.encode("utf-8"))
        return h.hexdigest()[:24]

    @staticmethod
    def hash_minutes(
        transcript: str,
        template_content: str,
        quality_preset: str,
        meeting_meta: str = "",
        custom_glossary: str = "",
        project_names: str = "",
    ) -> str:
        """議事録生成入力のハッシュ。"""
        h = hashlib.sha256()
        for s in (
            transcript,
            template_content,
            quality_preset,
            meeting_meta,
            custom_glossary,
            project_names,
        ):
            h.update(b"|")
            h.update(s.encode("utf-8"))
        return h.hexdigest()[:24]

    # =========================================================
    # 文字起こしキャッシュ
    # =========================================================

    def get_transcript(self, key: str) -> Optional[str]:
        path = self.transcripts_dir / f"{key}.txt"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def save_transcript(self, key: str, text: str, source_label: str = "") -> None:
        with CacheManager._lock:
            path = self.transcripts_dir / f"{key}.txt"
            path.write_text(text, encoding="utf-8")
            self._append_index(CacheEntry(
                kind="transcript",
                key=key,
                created_at=datetime.now().isoformat(timespec="seconds"),
                char_count=len(text),
                source_label=source_label[:120],
            ))

    # =========================================================
    # 議事録キャッシュ
    # =========================================================

    def get_minutes(self, key: str) -> Optional[str]:
        path = self.minutes_dir / f"{key}.txt"
        if path.exists():
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def save_minutes(self, key: str, text: str, source_label: str = "") -> None:
        with CacheManager._lock:
            path = self.minutes_dir / f"{key}.txt"
            path.write_text(text, encoding="utf-8")
            self._append_index(CacheEntry(
                kind="minutes",
                key=key,
                created_at=datetime.now().isoformat(timespec="seconds"),
                char_count=len(text),
                source_label=source_label[:120],
            ))

    # =========================================================
    # インデックス・統計
    # =========================================================

    def _append_index(self, entry: CacheEntry) -> None:
        idx = self._read_index()
        idx.append(asdict(entry))
        try:
            self.index_path.write_text(
                json.dumps(idx, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _read_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def stats(self) -> dict:
        """キャッシュ統計。"""
        tx_count = len(list(self.transcripts_dir.glob("*.txt")))
        min_count = len(list(self.minutes_dir.glob("*.txt")))
        tx_size = sum(p.stat().st_size for p in self.transcripts_dir.glob("*.txt"))
        min_size = sum(p.stat().st_size for p in self.minutes_dir.glob("*.txt"))
        return {
            "transcript_count": tx_count,
            "minutes_count": min_count,
            "transcript_size_kb": tx_size / 1024,
            "minutes_size_kb": min_size / 1024,
            "total_size_kb": (tx_size + min_size) / 1024,
        }

    def clear(self) -> tuple[int, int]:
        """キャッシュ全削除。"""
        with CacheManager._lock:
            tx = 0
            for p in self.transcripts_dir.glob("*.txt"):
                try:
                    p.unlink()
                    tx += 1
                except Exception:
                    pass
            mn = 0
            for p in self.minutes_dir.glob("*.txt"):
                try:
                    p.unlink()
                    mn += 1
                except Exception:
                    pass
            try:
                self.index_path.unlink(missing_ok=True)
            except Exception:
                pass
            return tx, mn


# 既定のグローバルマネージャ
_default_cache: Optional[CacheManager] = None
_default_lock = threading.Lock()


def get_default_cache() -> CacheManager:
    global _default_cache
    if _default_cache is None:
        with _default_lock:
            if _default_cache is None:
                project_root = Path(__file__).parent.parent
                _default_cache = CacheManager(project_root / "output" / "_cache")
    return _default_cache
