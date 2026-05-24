"""
会话记忆模块 —— 基于 session_id 的多轮对话历史
每个会话最多保留最近 MAX_TURNS 轮，超时自动过期
"""
import time
from threading import Lock
from collections import defaultdict

MAX_TURNS   = 6      # 每个 session 保留最近 6 轮（12条消息）
SESSION_TTL = 3600   # 1小时无活动则过期

_sessions: dict = defaultdict(lambda: {'turns': [], 'ts': time.time()})
_lock = Lock()


def get_history(session_id: str) -> list:
    """返回当前会话的对话历史（OpenAI message 格式）"""
    with _lock:
        s = _sessions[session_id]
        s['ts'] = time.time()
        return list(s['turns'])


def add_turn(session_id: str, question: str, answer: str):
    """追加一轮 user/assistant 对话到会话历史"""
    with _lock:
        turns = _sessions[session_id]['turns']
        turns.append({'role': 'user',      'content': question})
        turns.append({'role': 'assistant', 'content': answer})
        # 超出上限时从头裁剪（保持偶数对）
        if len(turns) > MAX_TURNS * 2:
            _sessions[session_id]['turns'] = turns[-(MAX_TURNS * 2):]
        _sessions[session_id]['ts'] = time.time()


def clear_session(session_id: str):
    """清空指定会话的历史（用于"新对话"按钮）"""
    with _lock:
        _sessions.pop(session_id, None)


def cleanup_expired() -> int:
    """清除过期会话，返回清除数量（可定期调用）"""
    now = time.time()
    with _lock:
        expired = [sid for sid, s in _sessions.items()
                   if now - s['ts'] > SESSION_TTL]
        for sid in expired:
            del _sessions[sid]
    return len(expired)


def session_info(session_id: str) -> dict:
    """返回会话摘要信息"""
    with _lock:
        s = _sessions.get(session_id)
        if not s:
            return {'turns': 0, 'age_sec': 0}
        return {
            'turns':   len(s['turns']) // 2,
            'age_sec': int(time.time() - s['ts'])
        }
