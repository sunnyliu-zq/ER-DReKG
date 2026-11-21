from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite:///./diseasegraph.db"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-5.3-flash"

    # AI-assisted drug interpretation. DeepSeek Flash only; key stays in backend.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout_seconds: int = 120
    deepseek_max_tokens: int = 8192
    graph_source: str = "csv"
    graph_data_dir: str = "experiments/platform_phase_b"
    max_graph_nodes: int = 200
    max_graph_depth: int = 2
    data_upload_dir: str = "data/raw/uploads"
    max_upload_bytes: int = 250_000_000

    # Collab center (isolated storage; does not touch the formal graph bundle).
    collab_upload_dir: str = "data/collab_uploads"
    collab_secret_key: str = ""
    collab_token_ttl_minutes: int = 720
    collab_doc_max_bytes: int = 10_000_000
    collab_data_max_bytes: int = 10_000_000
    collab_figure_max_bytes: int = 20_000_000

    # Resolve the project .env from this module instead of the process CWD.
    # Otherwise CLI scripts launched from the repository root silently fall
    # back to SQLite and can inspect or update the wrong database.
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")


settings = Settings()
