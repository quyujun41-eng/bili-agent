"""
memory.py —— 会话记忆（Redis 存储，fallback 进程内 dict）
升级：新增 session 列表与预览
"""
import json, time, logging
import config

logger = logging.getLogger(__name__)

_SESSION_TTL = 7200   # 2h
_MAX_TURNS   = 20
_SESSION_IDX = "session_idx"   # Redis sorted set，score = 时间戳

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
            r.setex(f"session:{session_id}", _SESSION_TTL,
                    json.dumps(history, ensure_ascii=False))
            # 更新索引和预览
            if role == "user":
                _update_meta(r, session_id, content)
            return
        except Exception:
            pass
    _mem_store[session_id] = history


def _update_meta(r, session_id: str, preview: str):
    """更新会话元数据（预览文本 + 时间戳）"""
    try:
        now = time.time()
        r.zadd(_SESSION_IDX, {session_id: now})
        r.expire(_SESSION_IDX, _SESSION_TTL * 10)
        meta_key = f"session_meta:{session_id}"
        raw = r.get(meta_key)
        meta = json.loads(raw) if raw else {"id": session_id, "created_at": int(now)}
        if "preview" not in meta:       # 只存第一条消息作为预览
            meta["preview"] = preview[:60]
        meta["updated_at"] = int(now)
        r.setex(meta_key, _SESSION_TTL, json.dumps(meta, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"_update_meta: {e}")


def list_sessions(limit: int = 30) -> list:
    """返回最近的 session 列表（按最后活跃时间倒序）"""
    r = _get_redis()
    if not r:
        return []
    try:
        ids = r.zrevrangebyscore(_SESSION_IDX, "+inf", "-inf",
                                 start=0, num=limit)
        result = []
        for sid in ids:
            raw = r.get(f"session_meta:{sid}")
            if raw:
                result.append(json.loads(raw))
            else:
                result.append({"id": sid, "preview": ""})
        return result
    except Exception as e:
        logger.debug(f"list_sessions: {e}")
        return []


def clear_session(session_id: str):
    r = _get_redis()
    if r:
        try:
            r.delete(f"session:{session_id}")
            r.delete(f"session_meta:{session_id}")
            r.zrem(_SESSION_IDX, session_id)
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
