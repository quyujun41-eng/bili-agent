import os

ANTHROPIC_API_KEY  = os.getenv('ANTHROPIC_API_KEY', '')
ANTHROPIC_BASE_URL = os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'bilibili.db')
PORT    = int(os.getenv('PORT', 5001))
