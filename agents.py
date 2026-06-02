"""
agents.py —— SQL / RAG / Chat 三路由 Agent + 流式输出
"""
import sqlite3, json, re, time, logging
from typing import Optional, Iterator

import config
import memory as mem
import rag

logger = logging.getLogger(__name__)

# ── Redis 查询缓存 ─────────────────────────────────────────────────────────────
_CACHE_PFX = "qcache:"
_CACHE_TTL = 1800
_mem_cache: dict = {}

def _cache_get(question: str):
    key = _CACHE_PFX + question.strip().lower()
    try:
        import redis as _r
        r = _r.from_url(config.REDIS_URL, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2)
        raw = r.get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    entry = _mem_cache.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry
    return None

def _cache_set(question: str, data: dict):
    key = _CACHE_PFX + question.strip().lower()
    data["ts"] = time.time()
    try:
        import redis as _r
        r = _r.from_url(config.REDIS_URL, decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2)
        r.setex(key, _CACHE_TTL, json.dumps(data, ensure_ascii=False))
        return
    except Exception:
        pass
    _mem_cache[key] = data


# ── SQL 工具 ──────────────────────────────────────────────────────────────────
_SQL_SCHEMA = """你是 SQLite 专家。数据库只有一张表 HuiZong（B站视频数据），字段说明：
id INTEGER 主键, 标题 TEXT 视频标题, 作者 TEXT UP主名字, 简介 TEXT 视频简介,
链接 TEXT 视频URL, 播放量 FLOAT 播放次数, 弹幕量 FLOAT 弹幕数量,
收藏量 FLOAT 收藏数, 点赞 FLOAT 点赞数, 评论 FLOAT 评论数,
转发 FLOAT 转发数, 投币 FLOAT 投币数, 粉丝数 FLOAT UP主粉丝数,
时长 FLOAT 视频时长（秒）, 分区 TEXT 视频分类（如：搞笑、美食制作、游戏、音乐、科技等）,
投稿时间 DATETIME 发布时间, data_year INTEGER 数据年份（2023/2024/2025/2026）

业务词汇：
  "最受欢迎"/"最火" = 播放量最高
  "互动最好" = 点赞+评论+转发+投币 之和最高
  "涨粉潜力" = 粉丝数/播放量 比值最高

只输出 SQL，不要任何解释。"""

def _run_sql(sql: str):
    uri  = f"file:{config.DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows
    finally:
        conn.close()

def _extract_sql(text: str) -> str:
    m = re.search(r'```sql\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'(SELECT\s+.*?(?:;|$))', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()

def _auto_chart(sql: str, cols: list, rows: list) -> dict:
    if "GROUP BY" not in sql.upper() or len(cols) < 2 or len(rows) < 2:
        return {"should_chart": False}
    x_data = [str(r[0]) for r in rows[:20]]
    y_data = [round(float(r[1]), 2) if r[1] is not None else 0 for r in rows[:20]]
    option = {
        "title":   {"text": "数据分析"},
        "tooltip": {"trigger": "axis"},
        "toolbox": {"feature": {"saveAsImage": {"title": "保存", "pixelRatio": 2}}},
        "grid":    {"bottom": "20%"},
        "xAxis":   {"type": "category", "data": x_data, "axisLabel": {"rotate": 30}},
        "yAxis":   {"type": "value"},
        "series":  [{"type": "bar", "data": y_data, "itemStyle": {"color": "#4a90e2"}}],
    }
    return {"should_chart": True, "option": option}


# ── LLM 流式输出 ──────────────────────────────────────────────────────────────
def _stream_llm(system: str, messages: list, max_tokens: int = 400) -> Iterator[str]:
    if config.LLM_PROVIDER == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY,
                                     base_url=config.ANTHROPIC_BASE_URL)
        with client.messages.stream(model=config.CLAUDE_MODEL, max_tokens=max_tokens,
                                    system=system, messages=messages) as s:
            for text in s.text_stream:
                yield text
    else:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)
        msgs   = [{"role": "system", "content": system}] + messages
        stream = client.chat.completions.create(model=config.OPENAI_MODEL,
                                                max_tokens=max_tokens,
                                                messages=msgs, stream=True)
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is not None and delta.content:
                yield delta.content

def _llm_once(system: str, messages: list, max_tokens: int = 300) -> str:
    return "".join(_stream_llm(system, messages, max_tokens))


# ── 路由判断 ──────────────────────────────────────────────────────────────────
_DATA_KW = [
    '播放', '分区', '作者', 'UP', '点赞', '弹幕', '收藏',
    '排行', '排名', '最高', '最多', '最少', '平均', '统计', '对比',
    '数据', '查询', '多少', '哪个', '哪些', '投币', '评论', '转发',
    '粉丝', '时长', '投稿', '年份', 'top', 'Top', 'TOP',
]
_RAG_KW = ['推荐', '找', '搜', '相关', '类似', '关于', '介绍', '内容', '什么视频']

def _route(question: str) -> str:
    if question.startswith(('你', '您', '我想', '能不能', '可以')):
        return "chat"
    if any(kw in question for kw in _RAG_KW):
        return "rag"
    if any(kw in question for kw in _DATA_KW):
        return "sql"
    return "chat"


# ── Agent 主入口 ──────────────────────────────────────────────────────────────
def sql_agent_stream(question: str, session_id: str = "default",
                     history: list = None) -> Iterator[dict]:
    if history is None:
        history = mem.get_history(session_id)

    if not history:
        cached = _cache_get(question)
        if cached:
            yield {"type": "text",  "text": cached.get("answer", "")}
            yield {"type": "agent", "agent": cached.get("agent", "sql")}
            yield {"type": "done",  "sql": cached.get("sql", ""),
                   "columns": cached.get("columns", []),
                   "rows":    cached.get("rows", []),
                   "total":   cached.get("total", 0),
                   "chart":   cached.get("chart", {"should_chart": False})}
            return

    agent = _route(question)
    yield {"type": "agent", "agent": agent}

    # ── SQL agent ─────────────────────────────────────────────────────────────
    if agent == "sql":
        sql_text = _llm_once(
            _SQL_SCHEMA,
            history + [{"role": "user", "content": question}],
            max_tokens=300,
        )
        sql = _extract_sql(sql_text)
        if not sql:
            yield {"type": "error", "error": "无法生成 SQL"}
            return

        cols, rows, err = None, None, None
        for attempt in range(2):
            try:
                cols, rows = _run_sql(sql)
                break
            except Exception as e:
                err = str(e)
                if attempt == 0:
                    fix = _llm_once(
                        _SQL_SCHEMA,
                        [{"role": "user", "content": f"修正 SQL（错误：{err}）：\n{sql}"}],
                        max_tokens=300,
                    )
                    sql = _extract_sql(fix)

        if err and cols is None:
            yield {"type": "error", "error": f"查询失败：{err}", "sql": sql}
            return

        if not rows:
            yield {"type": "text", "text": "数据库中没有符合条件的数据。"}
            yield {"type": "done", "sql": sql, "columns": [], "rows": [], "total": 0,
                   "chart": {"should_chart": False}}
            return

        preview = [dict(zip(cols, r)) for r in rows[:10]]
        answer  = ""
        for text in _stream_llm(
            "你是B站数据分析助手，用1-2句中文回答，带具体数字。",
            [{"role": "user", "content":
              f"问题：{question}\n查到{len(rows)}条，前{len(preview)}条：\n"
              f"{json.dumps(preview, ensure_ascii=False)}"}],
            max_tokens=200,
        ):
            answer += text
            yield {"type": "text", "text": text}

        chart = _auto_chart(sql, cols, rows)
        if not history:
            _cache_set(question, {"agent": "sql", "answer": answer, "sql": sql,
                                  "columns": cols, "rows": rows[:50],
                                  "total": len(rows), "chart": chart})
        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": sql, "columns": cols,
               "rows": rows[:50], "total": len(rows), "chart": chart}

    # ── RAG agent ─────────────────────────────────────────────────────────────
    elif agent == "rag":
        results = rag.search(question, top_k=5)
        if not results:
            yield {"type": "text", "text": "暂时没有找到相关视频，请换个关键词试试。"}
            yield {"type": "done", "sql": "", "columns": [], "rows": [], "total": 0,
                   "chart": {"should_chart": False}}
            return

        ctx    = "\n".join([f"- 《{r['title']}》 作者：{r['author']} 分区：{r['partition']}"
                            for r in results])
        answer = ""
        for text in _stream_llm(
            "你是B站视频推荐助手，根据检索结果推荐视频，简洁回答。",
            history + [{"role": "user", "content": f"问题：{question}\n\n相关视频：\n{ctx}"}],
            max_tokens=300,
        ):
            answer += text
            yield {"type": "text", "text": text}

        rows = [[r["id"], r["title"], r["author"], r["partition"]] for r in results]
        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": "", "columns": ["id", "标题", "作者", "分区"],
               "rows": rows, "total": len(rows), "chart": {"should_chart": False}}

    # ── Chat agent ────────────────────────────────────────────────────────────
    else:
        answer = ""
        for text in _stream_llm(
            "你是B站数据AI分析助手，可以正常聊天回答各种问题。",
            history + [{"role": "user", "content": question}],
            max_tokens=300,
        ):
            answer += text
            yield {"type": "text", "text": text}

        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": "", "columns": [], "rows": [], "total": 0,
               "chart": {"should_chart": False}}
