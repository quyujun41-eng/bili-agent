"""
统一 LLM 客户端 —— 支持 Claude / OpenAI 一键切换
通过环境变量 LLM_PROVIDER=claude 或 LLM_PROVIDER=openai 控制
"""
import config
from openai import OpenAI
import anthropic as _anthropic


def get_client():
    """返回 OpenAI 兼容客户端（Claude 和 OpenAI 均可用）"""
    if config.LLM_PROVIDER == 'openai':
        return OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    # Claude 通过 OpenAI 兼容接口调用
    return OpenAI(
        api_key=config.ANTHROPIC_API_KEY,
        base_url=config.ANTHROPIC_BASE_URL,
    )


def get_model() -> str:
    if config.LLM_PROVIDER == 'openai':
        return config.OPENAI_MODEL
    return config.CLAUDE_MODEL


def get_anthropic_client():
    """返回原生 Anthropic 客户端（用于流式输出）"""
    return _anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        base_url=config.ANTHROPIC_BASE_URL,
    )
