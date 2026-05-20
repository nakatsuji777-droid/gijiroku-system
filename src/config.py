"""プロジェクトパス・設定の一元管理"""
from pathlib import Path


class ProjectConfig:
    """プロジェクト全体のパスと設定を保持する"""

    def __init__(self):
        # プロジェクトルート（このファイルから2階層上）
        self.project_root = Path(__file__).parent.parent

        # 設定系ディレクトリ
        self.config_dir = self.project_root / "config"
        self.templates_dir = self.config_dir / "templates"
        self.templates_config_path = self.config_dir / "templates.json"

        # 入出力ディレクトリ
        self.input_dir = self.project_root / "input"
        self.output_dir = self.project_root / "output"

        # ランタイムで作成
        self.input_dir.mkdir(exist_ok=True)
        self.output_dir.mkdir(exist_ok=True)

        # ===== 音声認識エンジン設定 =====
        # "gemini"  : Google Gemini Files API（クラウド高速・1時間音声が1〜2分）← 推奨
        # "whisper" : faster-whisper（ローカル・オフライン可・CPUだと非常に遅い）
        self.transcription_engine = "gemini"

        # Gemini音声認識設定（transcription_engine = "gemini" のとき使用）
        self.gemini_transcription_model = "models/gemini-2.5-flash"

        # 文字起こし速度プリセット
        # "fastest"  : 最速（1.5倍速・無音除去・30分チャンク）
        # "balanced" : バランス（1.2倍速・無音除去・25分チャンク）← 推奨
        # "quality"  : 品質最優先（等倍・前処理なし・20分チャンク）
        self.transcription_speed_preset = "balanced"

        # Whisperモデル設定（transcription_engine = "whisper" のとき使用）
        # tiny / base / small / medium / large-v3
        self.whisper_model_size = "medium"
        self.whisper_device = "cpu"
        self.whisper_compute_type = "int8"

        # ===== 生成エンジン設定 =====
        # "gemini"  : Google Gemini API（無料枠あり・NotebookLMと同品質）← 推奨
        # "claude"  : Anthropic Claude API（有料・最高品質）
        # "ollama"  : ローカルLLM（無料・APIキー不要・品質低め）
        self.generation_engine = "gemini"

        # ===== 議事録生成 品質プリセット =====
        # "fast"     : 一発生成・思考なし・gemini-2.5-flash（速度優先・無料枠節約）
        # "balanced" : 二段階生成・軽い思考・gemini-2.5-flash（無料枠OK・推奨）← デフォルト
        # "best"     : 二段階生成・深い思考・gemini-2.5-pro（最高品質・有料枠推奨）
        self.minutes_quality_preset = "balanced"

        # Gemini設定（generation_engine = "gemini" のとき使用）
        self.gemini_model = "models/gemini-2.5-flash"  # 高速・高品質・無料枠あり

        # Claude設定（generation_engine = "claude" のとき使用）
        self.claude_model = "claude-sonnet-4-6"
        self.claude_max_tokens = 32000

        # Ollama設定（generation_engine = "ollama" のとき使用）
        self.ollama_model = "qwen2.5:7b"
        self.ollama_base_url = "http://localhost:11434"
        self.ollama_num_predict = 8000
