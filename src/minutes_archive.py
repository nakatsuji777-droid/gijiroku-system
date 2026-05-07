"""生成済み議事録の管理・検索モジュール（100%版：全文検索+ZIP+タグ）"""
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class MinutesEntry:
    """議事録ファイル1件のメタ情報とコンテンツキャッシュ"""

    path: Path
    filename: str
    template_id: str
    meeting_date: str
    created_at: datetime
    size_kb: float
    _cached_content: Optional[str] = field(default=None, repr=False)

    def get_content(self) -> str:
        """ファイル内容を取得（初回読込後はキャッシュ）"""
        if self._cached_content is None:
            self._cached_content = self.path.read_text(encoding="utf-8")
        return self._cached_content


def _parse_filename(file_path: Path) -> tuple[str, str]:
    """
    議事録ファイル名から (template_id, meeting_date) を抽出する。

    期待するファイル名: 議事録_[A-E]_YYYY-MM-DD[_HHmm].md
    """
    stem_parts = file_path.stem.split("_")
    template_id = stem_parts[1] if len(stem_parts) >= 2 else "?"
    meeting_date = stem_parts[2] if len(stem_parts) >= 3 else ""
    return template_id, meeting_date


def list_minutes(output_dir: Path) -> list[MinutesEntry]:
    """
    output/ 配下の議事録ファイルを新しい順に一覧化する。

    Args:
        output_dir: 議事録保管ディレクトリ

    Returns:
        MinutesEntry のリスト（作成日時降順）
    """
    entries: list[MinutesEntry] = []

    if not output_dir.exists():
        return entries

    for file_path in output_dir.glob("*.md"):
        try:
            template_id, meeting_date = _parse_filename(file_path)
            stat = file_path.stat()

            entries.append(
                MinutesEntry(
                    path=file_path,
                    filename=file_path.name,
                    template_id=template_id,
                    meeting_date=meeting_date,
                    created_at=datetime.fromtimestamp(stat.st_mtime),
                    size_kb=stat.st_size / 1024,
                )
            )
        except Exception:
            # 壊れたファイルはスキップ
            continue

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def filter_minutes(
    entries: list[MinutesEntry],
    template_id: Optional[str] = None,
    keyword: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> list[MinutesEntry]:
    """
    条件に合致する議事録のみを返す。

    Args:
        entries: 元のリスト
        template_id: "A"〜"E" または "すべて"
        keyword: ファイル名・本文を部分一致検索
        date_from: YYYY-MM-DD（以上）
        date_to: YYYY-MM-DD（以下）
    """
    result = entries

    if template_id and template_id != "すべて":
        result = [e for e in result if e.template_id == template_id]

    if date_from:
        result = [e for e in result if e.meeting_date and e.meeting_date >= date_from]

    if date_to:
        result = [e for e in result if e.meeting_date and e.meeting_date <= date_to]

    if keyword:
        kw_lower = keyword.lower()

        def matches(entry: MinutesEntry) -> bool:
            if kw_lower in entry.filename.lower():
                return True
            try:
                return kw_lower in entry.get_content().lower()
            except Exception:
                return False

        result = [e for e in result if matches(e)]

    return result


def generate_output_filename(template_id: str, meeting_date: str) -> str:
    """
    衝突回避のため時刻付きのユニークなファイル名を生成する。

    形式: 議事録_[template_id]_[meeting_date]_[HHmm].md
    例:   議事録_A_2026-04-12_1423.md
    """
    timestamp = datetime.now().strftime("%H%M")
    return f"議事録_{template_id}_{meeting_date}_{timestamp}.md"


# ===========================================================================
# 全文検索（ハイライト付き）
# ===========================================================================

@dataclass
class SearchHit:
    """検索結果1件分のヒット情報。"""
    entry: MinutesEntry
    snippets: list[str] = field(default_factory=list)
    hit_count: int = 0


def search_minutes(
    entries: list[MinutesEntry],
    keyword: str,
    snippet_chars: int = 60,
    max_snippets: int = 3,
) -> list[SearchHit]:
    """
    議事録を本文ベースで全文検索し、ヒット箇所のスニペットを返す。

    Args:
        entries: 検索対象
        keyword: 検索キーワード
        snippet_chars: ヒット箇所の前後何文字を切り出すか
        max_snippets: 1議事録あたり最大何件のスニペットを表示するか

    Returns:
        ヒット件数の多い順にソートされた SearchHit のリスト
    """
    if not keyword.strip():
        return []
    kw = keyword.strip()
    kw_lower = kw.lower()
    hits: list[SearchHit] = []

    for entry in entries:
        try:
            content = entry.get_content()
        except Exception:
            continue

        text_lower = content.lower()
        if kw_lower not in text_lower and kw_lower not in entry.filename.lower():
            continue

        # ヒット位置を全部見つける
        positions = [m.start() for m in re.finditer(re.escape(kw), content, re.IGNORECASE)]
        snippets: list[str] = []
        for pos in positions[:max_snippets]:
            start = max(0, pos - snippet_chars)
            end = min(len(content), pos + len(kw) + snippet_chars)
            snippet = content[start:end].replace("\n", " ")
            if start > 0:
                snippet = "…" + snippet
            if end < len(content):
                snippet = snippet + "…"
            snippets.append(snippet)

        hits.append(SearchHit(
            entry=entry,
            snippets=snippets,
            hit_count=len(positions),
        ))

    hits.sort(key=lambda h: h.hit_count, reverse=True)
    return hits


def highlight_snippet(snippet: str, keyword: str) -> str:
    """スニペット内のキーワードを Markdown の **太字** でハイライトする。"""
    if not keyword:
        return snippet
    pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    return pattern.sub(lambda m: f"**{m.group(0)}**", snippet)


# ===========================================================================
# 一括ZIPエクスポート
# ===========================================================================

def export_minutes_as_zip(
    entries: list[MinutesEntry],
    include_word: bool = False,
    include_excel: bool = False,
) -> bytes:
    """
    議事録ファイルをZIPアーカイブとしてバイト列で返す。

    Args:
        entries: ZIPに含める議事録一覧
        include_word: True なら .docx も同梱（src.exporters を使う）
        include_excel: True なら .xlsx も同梱
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            try:
                content = entry.get_content()
                # 元のMarkdown
                zf.writestr(entry.filename, content.encode("utf-8"))

                if include_word or include_excel:
                    from .exporters import MeetingMeta, to_docx_bytes, to_xlsx_bytes
                    meta = MeetingMeta(
                        meeting_date=entry.meeting_date,
                        template_name=entry.template_id,
                    )
                    title = f"議事録（{entry.template_id} / {entry.meeting_date}）"
                    stem = entry.filename.rsplit(".", 1)[0]
                    if include_word:
                        try:
                            zf.writestr(
                                f"{stem}.docx",
                                to_docx_bytes(content, title=title, meta=meta),
                            )
                        except Exception:
                            pass
                    if include_excel:
                        try:
                            zf.writestr(
                                f"{stem}.xlsx",
                                to_xlsx_bytes(content, title=title, meta=meta),
                            )
                        except Exception:
                            pass
            except Exception:
                continue
    return buf.getvalue()


# ===========================================================================
# タグ・お気に入り管理（サイドカーJSONで永続化）
# ===========================================================================

@dataclass
class MinutesMetadata:
    """議事録に紐づく追加メタデータ（タグ・お気に入り・メモ）。"""
    tags: list[str] = field(default_factory=list)
    favorite: bool = False
    memo: str = ""


def _meta_dir(output_dir: Path) -> Path:
    p = output_dir / "_meta"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_metadata(output_dir: Path, filename: str) -> MinutesMetadata:
    """指定議事録のサイドカーJSONを読み込む。なければ空のメタを返す。"""
    meta_path = _meta_dir(output_dir) / f"{filename}.json"
    if not meta_path.exists():
        return MinutesMetadata()
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return MinutesMetadata(
            tags=list(data.get("tags", [])),
            favorite=bool(data.get("favorite", False)),
            memo=str(data.get("memo", "")),
        )
    except Exception:
        return MinutesMetadata()


def save_metadata(output_dir: Path, filename: str, meta: MinutesMetadata) -> None:
    """サイドカーJSONに保存する。"""
    meta_path = _meta_dir(output_dir) / f"{filename}.json"
    meta_path.write_text(
        json.dumps(
            {
                "tags": meta.tags,
                "favorite": meta.favorite,
                "memo": meta.memo,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def collect_all_tags(output_dir: Path, entries: list[MinutesEntry]) -> list[str]:
    """全議事録のタグを集計し、出現頻度順に返す。"""
    from collections import Counter
    counter: Counter = Counter()
    for entry in entries:
        meta = load_metadata(output_dir, entry.filename)
        for tag in meta.tags:
            counter[tag] += 1
    return [tag for tag, _ in counter.most_common()]
