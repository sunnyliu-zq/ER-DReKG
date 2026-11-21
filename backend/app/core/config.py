from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]

_PUBLIC_DEV_COLLAB_SECRET = "dev-only-insecure-collab-secret-change-in-production"
# Keep in sync with collab_auth._DEV_SECRET. config cannot import collab_auth
# here without a circular import at module initialization time.


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


def validate_production_settings(s: Settings) -> None:
    """Fail fast at startup when production lacks a usable Collab signing key.

    collab_auth._get_secret() already refuses the public development fallback
    when APP_ENV=production, but only lazily at the first login/verification.
    This guard runs at import time so every entry point (API server, CLI
    scripts, seed) fails immediately instead of serving requests. The key
    value is never printed, logged, or embedded in the error message.
    """
    if s.app_env.strip().lower() != "production":
        return
    secret = s.collab_secret_key.strip()
    if not secret:
        raise RuntimeError(
            "APP_ENV=production requires a non-empty COLLAB_SECRET_KEY "
            "(strong random value) in the repository-root .env or the process "
            "environment. Refusing to start: the public development fallback "
            "must never sign production tokens."
        )
    if secret == _PUBLIC_DEV_COLLAB_SECRET:
        raise RuntimeError(
            "APP_ENV=production must not use the public development "
            "COLLAB_SECRET_KEY. Set a strong random value in the "
            "repository-root .env or the process environment."
        )


settings = Settings()
validate_production_settings(settings)
