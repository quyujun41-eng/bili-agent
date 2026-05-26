"""
config.py —— 配置管理（Pydantic Settings）
• 自动从环境变量 / .env 文件读取
• 启动时校验必填字段、枚举值，非法配置直接报错，不留到运行时才发现
"""
import os
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM 提供商
    LLM_PROVIDER: str = Field(default="claude", description="claude 或 openai")

    # Claude 配置
    ANTHROPIC_API_KEY: str  = Field(default="")
    ANTHROPIC_BASE_URL: str = Field(default="https://api.anthropic.com")
    CLAUDE_MODEL: str       = Field(default="claude-haiku-4-5-20251001")

    # OpenAI 配置
    OPENAI_API_KEY: str  = Field(default="")
    OPENAI_BASE_URL: str = Field(default="https://api.openai.com/v1")
    OPENAI_MODEL: str    = Field(default="gpt-4o-mini")

    # 数据库 & 端口
    DB_PATH: str = Field(default="")
    PORT: int    = Field(default=5001, ge=1, le=65535)

    # ── 校验器 ────────────────────────────────────────────────────────────────
    @field_validator("LLM_PROVIDER")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {"claude", "openai"}
        if v not in allowed:
            raise ValueError(
                f"LLM_PROVIDER='{v}' 无效，必须是 {allowed}"
            )
        return v

    @field_validator("PORT", mode="before")
    @classmethod
    def coerce_port(cls, v):
        return int(v)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,   # 环境变量大小写敏感
        "extra": "ignore",        # 忽略 .env 里的非模型字段（如 HF_ENDPOINT）
    }


# ── 单例（模块级）──────────────────────────────────────────────────────────────
_settings = Settings()

# 保持向后兼容：其他模块可以直接 `import config; config.LLM_PROVIDER`
LLM_PROVIDER       = _settings.LLM_PROVIDER
ANTHROPIC_API_KEY  = _settings.ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL = _settings.ANTHROPIC_BASE_URL
CLAUDE_MODEL       = _settings.CLAUDE_MODEL
OPENAI_API_KEY     = _settings.OPENAI_API_KEY
OPENAI_BASE_URL    = _settings.OPENAI_BASE_URL
OPENAI_MODEL       = _settings.OPENAI_MODEL
DB_PATH            = _settings.DB_PATH or os.path.join(
    os.path.dirname(__file__), "..", "bilibili.db"
)
PORT = _settings.PORT
