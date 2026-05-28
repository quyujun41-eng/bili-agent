"""
统一 LLM 客户端 —— 支持 Claude / OpenAI 一键切换
通过环境变量 LLM_PROVIDER=claude 或 LLM_PROVIDER=openai 控制
"""
import config
from openai import OpenAI
import anthropic as _anthropic


def _openai_base_url(url: str) -> str:
    """OpenAI SDK 需要 base_url 以 /v1 结尾"""
    url = url.rstrip('/')
    if not url.endswith('/v1'):
        url = url + '/v1'
    return url


def _anthropic_base_url(url: str) -> str:
    """Anthropic SDK 会自动拼 /v1，所以不能带 /v1"""
    url = url.rstrip('/')
    if url.endswith('/v1'):
        url = url[:-3]
    return url


def get_client():
    """返回 OpenAI 兼容客户端（Claude 和 OpenAI 均可用）"""
    if config.LLM_PROVIDER == 'openai':
        return OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    # Claude 通过 OpenAI 兼容接口调用，base_url 需含 /v1
    return OpenAI(
        api_key=config.ANTHROPIC_API_KEY,
        base_url=_openai_base_url(config.ANTHROPIC_BASE_URL),
    )


def get_model() -> str:
    if config.LLM_PROVIDER == 'openai':
        return config.OPENAI_MODEL
    return config.CLAUDE_MODEL


def get_anthropic_client():
    """返回原生 Anthropic 客户端（用于流式输出），base_url 不含 /v1"""
    return _anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        base_url=_anthropic_base_url(config.ANTHROPIC_BASE_URL),
    )
