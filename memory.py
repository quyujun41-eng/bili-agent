"""
memory.py —— 会话历史管理（Redis 存储，内存兜底）

• 优先写 Redis（TTL 2h）
• Redis 不可用时自动降级为进程内 dict（不跨进程）
• 会话历史格式：[{"role": "user"|"assistant", "content": "..."}, ...]
"""
import json
import time
import logging
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────────────
_SESSION_TTL  = 7200        # Redis key 过期时间（秒），2h
_MAX_TURNS    = 20          # 每个会话最多保留多少轮对话

# ── 降级：进程内 fallback ──────────────────────────────────────────────────────
_mem_store: dict[str, list] = {}

# ── Redis 客户端（懒加载）──────────────────────────────────────────────────────
_redis = None
_redis_ok = True   # 如果连续失败则置 False，停止尝试

def _get_redis():
    global _redis, _redis_ok
    if not _redis_ok:
        return None
    if _redis is None:
        try:
            import redis as _r
            _redis = _r.from_url(
                config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _redis.ping()
            logger.info("Memory: Redis 连接成功")
        except Exception as e:
            logger.warning(f"Memory: Redis 不可用，降级到内存: {e}")
            _redis_ok = False
            _redis = None
    return _redis


def _redis_key(session_id: str) -> str:
    return f"session:{session_id}"


# ══════════════════════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════════════════════

def get_history(session_id: str) -> list[dict]:
    """获取会话历史，返回 [{"role": ..., "content": ...}, ...]"""
    if not session_id:
        return []
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(_redis_key(session_id))
            if raw:
                return json.loads(raw)
            return []
        except Exception as e:
            logger.warning(f"Redis get_history 失败: {e}")

    # 降级
    return list(_mem_store.get(session_id, []))


def add_turn(session_id: str, question: str, answer: str) -> None:
    """追加一轮对话到历史"""
    if not session_id:
        return

    history = get_history(session_id)
    history.append({"role": "user",      "content": question})
    history.append({"role": "assistant", "content": answer})

    # 超出最大轮数时截断最旧的
    if len(history) > _MAX_TURNS * 2:
        history = history[-(  _MAX_TURNS * 2):]

    r = _get_redis()
    if r is not None:
        try:
            r.setex(_redis_key(session_id), _SESSION_TTL, json.dumps(history, ensure_ascii=False))
            return
        except Exception as e:
            logger.warning(f"Redis add_turn 失败，降级内存: {e}")

    # 降级
    _mem_store[session_id] = history


def clear_session(session_id: str) -> None:
    """清空指定会话历史"""
    if not session_id:
        return
    r = _get_redis()
    if r is not None:
        try:
            r.delete(_redis_key(session_id))
            return
        except Exception as e:
            logger.warning(f"Redis clear_session 失败: {e}")

    _mem_store.pop(session_id, None)


def ping() -> bool:
    """检查 Redis 是否可用（健康检查用）"""
    r = _get_redis()
    if r is None:
        return False
    try:
        r.ping()
        return True
    except Exception:
        return False
