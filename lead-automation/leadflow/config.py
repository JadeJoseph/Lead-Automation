"""Configuration. Everything comes from environment variables (optionally a .env file).

No secrets live in the repo. See .env.example for the full list.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Tiny .env loader (KEY=VALUE per line). Real environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    existing_csv: Path
    incoming_json: Path
    out_dir: Path
    company_name: str
    default_region: str
    llm_provider: str  # stub | anthropic | openai_compat
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    slack_webhook: str
    telegram_token: str
    telegram_chat_id: str
    webhook_secret: str

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        load_dotenv()
        env = os.environ.get
        provider = env("LLM_PROVIDER", "stub").strip().lower()
        api_key = env("LLM_API_KEY") or (env("ANTHROPIC_API_KEY") if provider == "anthropic" else "") or ""
        default_model = "claude-haiku-5-5" if provider == "anthropic" else ""
        cfg = cls(
            existing_csv=Path(env("EXISTING_LEADS_CSV", ROOT / "data" / "existing_leads.csv")),
            incoming_json=Path(env("INCOMING_LEADS_JSON", ROOT / "data" / "incoming_leads.json")),
            out_dir=Path(env("OUT_DIR", ROOT / "out")),
            company_name=env("COMPANY_NAME", "Jeevit"),
            default_region=env("DEFAULT_PHONE_REGION", "GB").upper(),
            llm_provider=provider,
            llm_api_key=api_key,
            llm_model=env("LLM_MODEL", default_model),
            llm_base_url=env("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            slack_webhook=env("SLACK_WEBHOOK_URL", ""),
            telegram_token=env("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=env("TELEGRAM_CHAT_ID", ""),
            webhook_secret=env("WEBHOOK_SECRET", ""),
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return cfg
