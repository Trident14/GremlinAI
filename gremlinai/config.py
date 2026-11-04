"""
GremlinAI — configuration via pydantic-settings.
All values read from environment / .env file.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── LLM provider ──────────────────────────────────────────────────────────
    llm_provider: str = "mock"           # gemini | meta | mock

    # Gemini
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model_fast: str = "gemini-flash-lite-latest"
    gemini_model_vision: str = "gemini-flash-lite-latest"

    # Meta
    meta_api_key: str = ""
    meta_api_base: str = ""
    meta_model_fast: str = ""
    meta_model_vision: str = ""

    # Per-role overrides (optional — leave blank to use llm_provider for both)
    llm_fast_provider: str = ""
    llm_vision_provider: str = ""

    # Safety rails
    llm_max_rpm: int = 10
    max_cost_usd_per_run: float = 2.00

    # GitHub export
    github_token: str = ""
    github_repo: str = ""

    # Auth
    keep_auth_state: bool = False

    # Agent limits
    max_actions_per_step: int = 6
    max_total_actions: int = 40
    max_run_minutes: int = 20
    step_timeout_s: int = 240          # a single step is marked stuck after this many seconds

    # LLM request behaviour (important for slow reasoning models such as Meta Muse Spark)
    llm_timeout_s: float = 90.0        # per LLM request
    llm_reasoning_effort_fast: str = "minimal"   # meta only: minimal|low|medium|high|xhigh
    llm_reasoning_effort_vision: str = "low"
    llm_max_output_tokens: int = 4096  # meta only; reasoning tokens count against this

    # Demo app bug flags
    bug_login_redirect: bool = False
    bug_cart_total: bool = False
    bug_email_validation: bool = False
    bug_submit_disabled: bool = False


settings = Settings()
