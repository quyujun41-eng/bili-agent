"""
memory.py —— 会话记忆（Redis 存储，fallback 进程内 dict）
"""
import json, logging
import config

logger = logging.getLogger(__name__)

_SESSION_TTL = 7200   # 2h
_MAX_TURNS   = 20

_redis_client = None
_mem_store: dict = {}


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis as _r
            c = _r.from_url(
                config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            c.ping()
            _redis_client = c
        except Exception as e:
            logger.warning(f"Redis 不可用，会话使用内存存储: {e}")
    return _redis_client


def get_history(session_id: str) -> list:
    r = _get_redis()
    if r:
        try:
            raw = r.get(f"session:{session_id}")
            return json.loads(raw) if raw else []
        except Exception:
            pass
    return list(_mem_store.get(session_id, []))


def add_turn(session_id: str, role: str, content: str):
    history = get_history(session_id)
    history.append({"role": role, "content": content})
    if len(history) > _MAX_TURNS * 2:
        history = history[-_MAX_TURNS * 2:]
    r = _get_redis()
    if r:
        try:
            r.setex(f"session:{session_id}", _SESSION_TTL, json.dumps(history, ensure_ascii=False))
            return
        except Exception:
            pass
    _mem_store[session_id] = history


def clear_session(session_id: str):
    r = _get_redis()
    if r:
        try:
            r.delete(f"session:{session_id}")
        except Exception:
            pass
    _mem_store.pop(session_id, None)


def ping() -> bool:
    r = _get_redis()
    if r:
        try:
            return bool(r.ping())
        except Exception:
            pass
    return False
