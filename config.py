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

    # 向量数据库（Qdrant）
    QDRANT_URL: str        = Field(default="http://localhost:6333")
    QDRANT_COLLECTION: str = Field(default="bilibili_videos")

    # 缓存 & 会话（Redis）
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    # Reranker（Cohere API）
    COHERE_API_KEY: str = Field(default="")
    COHERE_RERANK_MODEL: str = Field(default="rerank-multilingual-v3.0")

    # Embedding 模型（本地 sentence-transformers）
    EMBED_MODEL: str = Field(
        default="BAAI/bge-small-zh-v1.5",
        description="sentence-transformers 模型名称"
    )
    EMBED_CACHE_TTL: int = Field(
        default=86400,
        description="Embedding Redis 缓存 TTL（秒），默认 24h"
    )

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

# 新增配置项
QDRANT_URL         = _settings.QDRANT_URL
QDRANT_COLLECTION  = _settings.QDRANT_COLLECTION
REDIS_URL          = _settings.REDIS_URL
COHERE_API_KEY     = _settings.COHERE_API_KEY
COHERE_RERANK_MODEL = _settings.COHERE_RERANK_MODEL
EMBED_MODEL        = _settings.EMBED_MODEL
EMBED_CACHE_TTL    = _settings.EMBED_CACHE_TTL

DB_PATH = _settings.DB_PATH or os.path.join(
    os.path.dirname(__file__), "..", "bilibili.db"
)
PORT = _settings.PORT
