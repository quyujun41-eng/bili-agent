"""
agents.py —— SQL / RAG / Chat / Combined 四路由 Agent
v5.0 升级：问题改写、SQL+RAG协作、主动洞察
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

示例：
Q: 播放量最高的10个视频
A: SELECT id, 标题, 作者, 播放量 FROM HuiZong ORDER BY 播放量 DESC LIMIT 10;

Q: 各分区平均播放量对比
A: SELECT 分区, AVG(播放量) as 平均播放量, COUNT(*) as 视频数 FROM HuiZong GROUP BY 分区 ORDER BY 平均播放量 DESC;

Q: 互动最好的前5个视频
A: SELECT 标题, 作者, (点赞+评论+转发+投币) as 互动总量 FROM HuiZong ORDER BY 互动总量 DESC LIMIT 5;

Q: 2025年和2026年各分区视频数量对比
A: SELECT data_year, 分区, COUNT(*) as 数量 FROM HuiZong WHERE data_year IN (2025,2026) GROUP BY data_year, 分区 ORDER BY data_year, 数量 DESC;

Q: 粉丝超过100万的UP主
A: SELECT DISTINCT 作者, MAX(粉丝数) as 粉丝数 FROM HuiZong WHERE 粉丝数 > 1000000 GROUP BY 作者 ORDER BY 粉丝数 DESC;

Q: 涨粉潜力最高的视频
A: SELECT 标题, 作者, 粉丝数, 播放量, ROUND(粉丝数*1.0/播放量,4) as 涨粉比 FROM HuiZong WHERE 播放量 > 10000 ORDER BY 涨粉比 DESC LIMIT 10;

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


# ── LLM ──────────────────────────────────────────────────────────────────────
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


# ── 1. 问题改写（解决多轮上下文理解）────────────────────────────────────────
_REWRITE_PROMPT = """根据对话历史，将用户最新问题改写为完整独立的问题（让没看过对话的人也能理解）。
如果问题本身已完整清晰，直接返回原问题。只输出问题本身，不要任何解释。"""

def _rewrite_query(question: str, history: list) -> str:
    if not history:
        return question
    # 只有问题明显依赖上下文时才调用 LLM
    ref_words = ['那', '这个', '它', '的呢', '呢', '再', '还有', '另外', '同样', '换', '也']
    needs = len(question) < 12 or any(w in question for w in ref_words)
    if not needs:
        return question
    try:
        ctx = "\n".join([
            f"{m['role']}: {m['content'][:80]}"
            for m in history[-4:]
        ])
        result = _llm_once(
            _REWRITE_PROMPT,
            [{"role": "user", "content": f"历史对话：\n{ctx}\n\n最新问题：{question}"}],
            max_tokens=80
        ).strip()
        return result if result else question
    except Exception:
        return question


# ── 2. 主动洞察（SQL 结果中找规律）────────────────────────────────────────────
def _generate_insight(question: str, cols: list, rows: list) -> str:
    if len(rows) < 3:
        return ""
    try:
        preview = [dict(zip(cols, r)) for r in rows[:15]]
        insight = _llm_once(
            "你是数据洞察专家。从数据中找出1个最有价值的规律或异常，用1句简洁中文说明，以💡开头。如无明显规律返回空字符串。",
            [{"role": "user",
              "content": f"问题：{question}\n数据：{json.dumps(preview, ensure_ascii=False)}"}],
            max_tokens=80
        ).strip()
        return insight if insight.startswith("💡") else ""
    except Exception:
        return ""


# ── 3. 是否需要 SQL+RAG 协作 ────────────────────────────────────────────────
_STAT_KW    = ['播放量', '热门', '爆款', '最火', '高播', '受欢迎', '流行', '最受欢迎']
_CONTENT_KW = ['推荐', '搞笑', '好看', '有趣', '美食', '游戏', '音乐', '科技', '动漫', '运动', '萌']

def _needs_combined(question: str) -> bool:
    return (any(k in question for k in _STAT_KW) and
            any(k in question for k in _CONTENT_KW))


# ── 语义路由 ──────────────────────────────────────────────────────────────────
_ROUTE_CACHE: dict = {}
_ROUTE_CACHE_TTL = 3600

_ROUTE_PROMPT = """将用户问题分类为以下意图之一：
- sql：数据统计查询（排行、数量、均值、对比、最高最低等数值分析）
- rag：视频语义搜索推荐（找视频、推荐某类内容）
- chat：闲聊、个人问题或其他

只输出分类名称：sql、rag 或 chat，不要其他内容。"""

def _route(question: str) -> str:
    if question.startswith(('你', '您', '我想聊', '帮我解释', '什么是')):
        return "chat"

    # 协作检测优先
    if _needs_combined(question):
        return "combined"

    cache_key = question.strip().lower()[:60]
    entry = _ROUTE_CACHE.get(cache_key)
    if entry and time.time() - entry["ts"] < _ROUTE_CACHE_TTL:
        return entry["agent"]

    try:
        result = _llm_once(_ROUTE_PROMPT,
                           [{"role": "user", "content": question}],
                           max_tokens=5).strip().lower()
        if "sql"  in result: agent = "sql"
        elif "rag" in result: agent = "rag"
        else:                 agent = "chat"
    except Exception:
        _RAG_KW  = ['推荐', '找', '搜', '相关', '类似', '什么视频']
        _DATA_KW = ['播放', '分区', '作者', 'UP', '点赞', '排行', '最高', '最多',
                    '平均', '统计', '对比', '多少', '粉丝', '投币', 'top', 'Top']
        if any(kw in question for kw in _RAG_KW):   agent = "rag"
        elif any(kw in question for kw in _DATA_KW): agent = "sql"
        else:                                        agent = "chat"

    _ROUTE_CACHE[cache_key] = {"agent": agent, "ts": time.time()}
    return agent


# ── Agent 主入口 ──────────────────────────────────────────────────────────────
def sql_agent_stream(question: str, session_id: str = "default",
                     history: list = None) -> Iterator[dict]:
    if history is None:
        history = mem.get_history(session_id)

    # 1. 问题改写（多轮上下文理解）
    rewritten = _rewrite_query(question, history)
    if rewritten != question:
        yield {"type": "rewrite", "original": question, "rewritten": rewritten}
    display_q = rewritten  # 用改写后的问题做后续处理

    # 2. 缓存命中
    if not history:
        cached = _cache_get(display_q)
        if cached:
            yield {"type": "text",  "text": cached.get("answer", "")}
            yield {"type": "agent", "agent": cached.get("agent", "sql")}
            yield {"type": "done",  "sql": cached.get("sql", ""),
                   "columns": cached.get("columns", []),
                   "rows":    cached.get("rows", []),
                   "total":   cached.get("total", 0),
                   "chart":   cached.get("chart", {"should_chart": False}),
                   "insight": cached.get("insight", "")}
            return

    agent = _route(display_q)
    yield {"type": "agent", "agent": agent}

    # ── SQL agent ─────────────────────────────────────────────────────────────
    if agent == "sql":
        sql_text = _llm_once(
            _SQL_SCHEMA,
            history + [{"role": "user", "content": display_q}],
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
                        [{"role": "user",
                          "content": f"修正 SQL（错误：{err}）：\n{sql}"}],
                        max_tokens=300,
                    )
                    sql = _extract_sql(fix)

        if err and cols is None:
            yield {"type": "error", "error": f"查询失败：{err}", "sql": sql}
            return

        if not rows:
            yield {"type": "text", "text": "数据库中没有符合条件的数据。"}
            yield {"type": "done", "sql": sql, "columns": [], "rows": [],
                   "total": 0, "chart": {"should_chart": False}, "insight": ""}
            return

        preview = [dict(zip(cols, r)) for r in rows[:10]]
        answer  = ""
        for text in _stream_llm(
            "你是B站数据分析助手，用1-2句中文回答，带具体数字。",
            [{"role": "user", "content":
              f"问题：{display_q}\n查到{len(rows)}条，前{len(preview)}条：\n"
              f"{json.dumps(preview, ensure_ascii=False)}"}],
            max_tokens=200,
        ):
            answer += text
            yield {"type": "text", "text": text}

        # 3. 主动洞察
        insight = _generate_insight(display_q, cols, rows)

        chart = _auto_chart(sql, cols, rows)
        if not history:
            _cache_set(display_q, {"agent": "sql", "answer": answer, "sql": sql,
                                   "columns": cols, "rows": rows[:50],
                                   "total": len(rows), "chart": chart,
                                   "insight": insight})
        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": sql, "columns": cols,
               "rows": rows[:50], "total": len(rows),
               "chart": chart, "insight": insight}

    # ── RAG agent ─────────────────────────────────────────────────────────────
    elif agent == "rag":
        enriched_query = display_q
        if history:
            recent = [m["content"] for m in history[-6:] if m["role"] == "user"]
            if recent and recent[-1] != display_q:
                enriched_query = " ".join(recent[-2:]) + " " + display_q

        results = rag.search(enriched_query, top_k=5)
        if not results:
            yield {"type": "text", "text": "暂时没有找到相关视频，请换个关键词试试。"}
            yield {"type": "done", "sql": "", "columns": [], "rows": [], "total": 0,
                   "chart": {"should_chart": False}, "insight": ""}
            return

        ctx    = "\n".join([
            f"- 《{r['title']}》 作者：{r['author']} 分区：{r['partition']}"
            for r in results
        ])
        answer = ""
        for text in _stream_llm(
            "你是B站视频推荐助手，根据检索结果推荐视频，简洁回答。",
            history + [{"role": "user",
                        "content": f"问题：{display_q}\n\n相关视频：\n{ctx}"}],
            max_tokens=300,
        ):
            answer += text
            yield {"type": "text", "text": text}

        rows = [[r["id"], r["title"], r["author"], r["partition"]] for r in results]
        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": "", "columns": ["id", "标题", "作者", "分区"],
               "rows": rows, "total": len(rows),
               "chart": {"should_chart": False}, "insight": ""}

    # ── Combined agent（SQL + RAG 协作）──────────────────────────────────────
    elif agent == "combined":
        yield {"type": "status", "text": "正在联合检索…"}

        # Step 1: RAG 语义检索
        rag_results = rag.search(display_q, top_k=15)

        # Step 2: 对检索到的视频补充统计数据
        enriched = []
        if rag_results:
            ids      = ",".join(str(r["id"]) for r in rag_results)
            try:
                cols, rows = _run_sql(
                    f"SELECT id, 播放量, 点赞, 评论 FROM HuiZong WHERE id IN ({ids})"
                )
                stats_map = {str(r[0]): r for r in rows}
                for r in rag_results:
                    s = stats_map.get(str(r["id"]))
                    enriched.append({
                        **r,
                        "plays":  s[1] if s else 0,
                        "likes":  s[2] if s else 0,
                    })
            except Exception:
                enriched = rag_results

        # Step 3: 按播放量排序，取前6
        enriched.sort(key=lambda x: x.get("plays", 0), reverse=True)
        top = enriched[:6]

        if not top:
            yield {"type": "text", "text": "没有找到符合条件的视频。"}
            yield {"type": "done", "sql": "", "columns": [], "rows": [],
                   "total": 0, "chart": {"should_chart": False}, "insight": ""}
            return

        ctx = "\n".join([
            f"- 《{r['title']}》 作者：{r['author']} 分区：{r['partition']}"
            + (f" 播放量：{int(r['plays']):,}" if r.get('plays') else "")
            for r in top
        ])
        answer = ""
        for text in _stream_llm(
            "你是B站视频推荐助手，综合考虑语义相关性和数据表现推荐视频，简洁回答。",
            history + [{"role": "user",
                        "content": f"需求：{display_q}\n\n候选视频（已按播放量排序）：\n{ctx}"}],
            max_tokens=300,
        ):
            answer += text
            yield {"type": "text", "text": text}

        rows_out = [[r["id"], r["title"], r["author"], r["partition"],
                     int(r.get("plays", 0))] for r in top]
        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": "",
               "columns": ["id", "标题", "作者", "分区", "播放量"],
               "rows": rows_out, "total": len(top),
               "chart": {"should_chart": False}, "insight": ""}

    # ── Chat agent ────────────────────────────────────────────────────────────
    else:
        answer = ""
        for text in _stream_llm(
            "你是B站数据AI分析助手，可以正常聊天回答各种问题。",
            history + [{"role": "user", "content": display_q}],
            max_tokens=300,
        ):
            answer += text
            yield {"type": "text", "text": text}

        mem.add_turn(session_id, "user",      question)
        mem.add_turn(session_id, "assistant", answer)
        yield {"type": "done", "sql": "", "columns": [], "rows": [], "total": 0,
               "chart": {"should_chart": False}, "insight": ""}
