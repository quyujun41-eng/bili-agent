"""
monitor.py —— 查询日志、统计、反馈（Redis 存储）
"""
import json, time, logging
import config

logger = logging.getLogger(__name__)

_KEY      = "monitor:queries"
_FB_KEY   = "monitor:feedback"
_MAX      = 500


def log(question: str, agent: str, ms: float, ok: bool, err: str = ""):
    entry = {"ts": int(time.time()), "q": question[:80],
             "agent": agent, "ms": round(ms), "ok": ok, "err": err[:80]}
    try:
        import redis as _r
        r = _r.from_url(config.REDIS_URL, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2)
        r.lpush(_KEY, json.dumps(entry, ensure_ascii=False))
        r.ltrim(_KEY, 0, _MAX - 1)
    except Exception as e:
        logger.debug(f"monitor.log: {e}")


def log_feedback(session_id: str, question: str, rating: str):
    entry = {"ts": int(time.time()), "session": session_id[:20],
             "q": question[:80], "rating": rating}
    try:
        import redis as _r
        r = _r.from_url(config.REDIS_URL, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2)
        r.lpush(_FB_KEY, json.dumps(entry, ensure_ascii=False))
        r.ltrim(_FB_KEY, 0, 499)
    except Exception as e:
        logger.debug(f"monitor.feedback: {e}")


def stats(n: int = 200) -> dict:
    try:
        import redis as _r
        r = _r.from_url(config.REDIS_URL, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2)
        rows = [json.loads(x) for x in r.lrange(_KEY, 0, n - 1)]
        fb   = [json.loads(x) for x in r.lrange(_FB_KEY, 0, 199)]
    except Exception:
        return {"total": 0}

    if not rows:
        return {"total": 0, "by_agent": {}, "success_rate": 100,
                "avg_ms": 0, "feedback": {"total": 0, "like_rate": 0}}

    by_agent: dict = {}
    total_ms = 0
    ok_count = 0
    for row in rows:
        by_agent[row["agent"]] = by_agent.get(row["agent"], 0) + 1
        total_ms += row.get("ms", 0)
        if row.get("ok"):
            ok_count += 1

    likes = sum(1 for f in fb if f.get("rating") == "up")
    fb_rate = round(likes / len(fb) * 100, 1) if fb else 0

    return {
        "total":        len(rows),
        "by_agent":     by_agent,
        "success_rate": round(ok_count / len(rows) * 100, 1),
        "avg_ms":       round(total_ms / len(rows)),
        "recent":       rows[:5],
        "feedback": {
            "total":     len(fb),
            "likes":     likes,
            "like_rate": fb_rate,
        },
    }
