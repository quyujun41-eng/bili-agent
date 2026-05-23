import os

# LLM 提供商：claude 或 openai（通过环境变量切换）
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'claude')

# Claude 配置
ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY', '')
ANTHROPIC_BASE_URL = os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
CLAUDE_MODEL       = os.getenv('CLAUDE_MODEL', 'claude-haiku-4-5-20251001')

# OpenAI 配置
OPENAI_API_KEY  = os.getenv('OPENAI_API_KEY', '')
OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
OPENAI_MODEL    = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')

# 数据库 & 端口
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'bilibili.db')
PORT    = int(os.getenv('PORT', 5001))
