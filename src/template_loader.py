"""テンプレート読み込みモジュール"""
import json
from pathlib import Path


def load_templates(config_path: Path) -> dict:
    """
    templates.json を読み込み、会議種別テンプレート一覧を返す。

    Returns:
        {"A": {"name": "...", "description": "...", "file": "...", "output_format": "..."}, ...}
    """
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def load_template_content(template_path: Path) -> str:
    """指定されたテンプレートファイルの中身を文字列で返す"""
    return template_path.read_text(encoding="utf-8")


def fill_template(
    template: str,
    transcript: str,
    meeting_date: str = "",
    meeting_location: str = "",
    attendees: str = "",
    project_names: str = "",
) -> str:
    """
    テンプレートのプレースホルダを実データで置換する。

    - {transcript}: 文字起こしテキスト
    - {metadata}: 補足情報（日時・場所・出席者・関連プロジェクト）
    """
    metadata_lines = []
    if meeting_date:
        metadata_lines.append(f"- 開催日：{meeting_date}")
    if meeting_location:
        metadata_lines.append(f"- 開催場所：{meeting_location}")
    if attendees:
        metadata_lines.append(f"- 確定出席者：{attendees}")
    if project_names and project_names.strip():
        # 改行・カンマで区切られた案件名を整形
        names = [n.strip() for n in project_names.replace("、", ",").replace("\n", ",").split(",") if n.strip()]
        if names:
            metadata_lines.append(
                "- 関連プロジェクト・案件名（**正しい固有名詞として扱うこと**）：\n  "
                + "\n  ".join(f"・{n}" for n in names)
            )

    metadata_block = "\n".join(metadata_lines) if metadata_lines else "（補足情報なし）"

    filled = template.replace("{metadata}", metadata_block)
    filled = filled.replace("{transcript}", transcript)
    return filled
