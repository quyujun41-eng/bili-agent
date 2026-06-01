from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    LLM_PROVIDER:        str = "openai"
    ANTHROPIC_API_KEY:   str = ""
    ANTHROPIC_BASE_URL:  str = ""
    CLAUDE_MODEL:        str = "claude-haiku-4-5-20251001"
    OPENAI_API_KEY:      str = ""
    OPENAI_BASE_URL:     str = ""
    OPENAI_MODEL:        str = "gpt-4o-mini"

    # 数据库
    DB_PATH:       str = "/bilibili.db"
    PORT:          int = 5001
    ACCESS_TOKEN:  str = "bili2026"

    # Qdrant
    QDRANT_URL:        str = "http://qdrant:6333"
    QDRANT_COLLECTION: str = "bilibili_videos"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # Cohere Rerank
    COHERE_API_KEY:      str = ""
    COHERE_RERANK_MODEL: str = "rerank-multilingual-v3.0"

    # Embedding
    EMBED_MODEL:     str = "BAAI/bge-small-zh-v1.5"
    EMBED_CACHE_TTL: int = 86400   # 24h

_s = Settings()

LLM_PROVIDER       = _s.LLM_PROVIDER
ANTHROPIC_API_KEY  = _s.ANTHROPIC_API_KEY
ANTHROPIC_BASE_URL = _s.ANTHROPIC_BASE_URL
CLAUDE_MODEL       = _s.CLAUDE_MODEL
OPENAI_API_KEY     = _s.OPENAI_API_KEY
OPENAI_BASE_URL    = _s.OPENAI_BASE_URL
OPENAI_MODEL       = _s.OPENAI_MODEL
DB_PATH            = _s.DB_PATH
PORT               = _s.PORT
ACCESS_TOKEN       = _s.ACCESS_TOKEN
QDRANT_URL         = _s.QDRANT_URL
QDRANT_COLLECTION  = _s.QDRANT_COLLECTION
REDIS_URL          = _s.REDIS_URL
COHERE_API_KEY     = _s.COHERE_API_KEY
COHERE_RERANK_MODEL= _s.COHERE_RERANK_MODEL
EMBED_MODEL        = _s.EMBED_MODEL
EMBED_CACHE_TTL    = _s.EMBED_CACHE_TTL
