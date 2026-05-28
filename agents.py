"""
agents.py —— LangGraph 多 Agent 路由 + 流式输出

流程：
  用户问题
    → 路由（sql_agent / rag_agent / chat_agent）
    → 执行 agent（流式生成）
    → 写会话记忆
    → yield done chunk

Chunk 类型：
  {"type":"agent",   "agent": "sql_agent|rag_agent|chat_agent"}
  {"type":"memory",  "turns": N}
  {"type":"rewrite", "original": "...", "rewritten": "..."}
  {"type":"text",    "text": "..."}
  {"type":"error",   "error": "..."}
  {"type":"done",    "agent":..., "sql":..., "columns":..., "rows":..., "total":..., "chart":...}
"""
import json, time, logging
from typing import Optional, Iterator

import redis as _redis_lib

import config
import memory as mem
import rag

logger = logging.getLogger(__name__)

# ── Redis 查询缓存（代替原来的进程内 dict）───────────────────────────────────
_CACHE_TTL  = 1800    # 30 分钟
_CACHE_PFX  = "qcache:"
_redis_cache = None
_redis_cache_ok = True

def _get_cache_redis():
    global _redis_cache, _redis_cache_ok
    if not _redis_cache_ok:
        return None
    if _redis_cache is None:
        try:
            _redis_cache = _redis_lib.from_url(
                config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _redis_cache.ping()
        except Exception as e:
            logger.warning(f"agents: Redis 缓存不可用，回退内存: {e}")
            _redis_cache_ok = False
    return _redis_cache

# 进程内备用缓存
_mem_cache: dict = {}

def _cache_get(key: str) -> Optional[dict]:
    k = _CACHE_PFX + key.strip().lower()
    r = _get_cache_redis()
    if r is not None:
        try:
            raw = r.get(k)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    # fallback
    e = _mem_cache.get(k)
    if e and time.time() - e["ts"] < _CACHE_TTL:
        return e
    return None

def _cache_set(key: str, data: dict):
    k = _CACHE_PFX + key.strip().lower()
    payload = json.dumps({"ts": time.time(), **data}, ensure_ascii=False)
    r = _get_cache_redis()
    if r is not None:
        try:
            r.setex(k, _CACHE_TTL, payload)
            return
        except Exception:
            pass
    # fallback
    _mem_cache[k] = {"ts": time.time(), **data}


# ── LangGraph 路由 ─────────────────────────────────────────────────────────
from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage

def _make_router():
    from langchain_anthropic import ChatAnthropic
    from langchain_openai   import ChatOpenAI

    if config.LLM_PROVIDER == "claude":
        llm = ChatAnthropic(
            model=config.CLAUDE_MODEL,
            api_key=config.ANTHROPIC_API_KEY,
            base_url=config.ANTHROPIC_BASE_URL,
            max_tokens=64,
        )
    else:
        llm = ChatOpenAI(
            model=config.OPENAI_MODEL,
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
            max_tokens=64,
        )

    SYSTEM = (
        "你是一个意图路由器。根据用户问题，只输出一个单词：\n"
        "- sql_agent：统计/排行/数量/播放量/时间范围等结构化查询\n"
        "- rag_agent：视频推荐/找视频/相关内容等语义检索\n"
        "- chat_agent：闲聊/平台介绍/其他一般问题\n"
        "只输出这三个词之一，不要有任何其他内容。"
    )

    class RouterState(dict):
        pass

    def route_node(state):
        q = state["question"]
        resp = llm.invoke([
            {"role": "system",  "content": SYSTEM},
            {"role": "user",    "content": q},
        ])
        agent = resp.content.strip().lower()
        if agent not in {"sql_agent", "rag_agent", "chat_agent"}:
            agent = "chat_agent"
        return {"agent": agent}

    g = StateGraph(RouterState)
    g.add_node("route", route_node)
    g.set_entry_point("route")
    g.add_edge("route", END)
    return g.compile()

_router = None
def _get_router():
    global _router
    if _router is None:
        _router = _make_router()
    return _router

# ── LLM 流式调用 ───────────────────────────────────────────────────────────
def _stream_llm(messages: list, max_tokens: int = 1024) -> Iterator[str]:
    if config.LLM_PROVIDER == "claude":
        from anthropic import Anthropic
        client = Anthropic(
            api_key=config.ANTHROPIC_API_KEY,
            base_url=config.ANTHROPIC_BASE_URL,
        )
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msgs  = [m for m in messages if m["role"] != "system"]
        with client.messages.stream(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system_msg,
            messages=user_msgs,
        ) as stream:
            for text in stream.text_stream:
                yield text
    else:
        from openai import OpenAI
        client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
        stream = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            max_tokens=max_tokens,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content


# ── SQL 工具（LangChain Tool）─────────────────────────────────────────────
from langchain.tools import tool

@tool
def sql_query_tool(question: str) -> str:
    """根据用户问题生成并执行 SQL，返回 JSON 字符串"""
    import sqlite3

    gen_prompt = [
        {"role": "system", "content": (
            "你是 SQLite 专家。数据库只有一张表 HuiZong，字段：\n"
            "id, title, author, partition, year, play, danmaku, comment, coin, "
            "collect, share, like_count, description, duration\n"
            "只输出 SQL，不要解释，不要 markdown 代码块。"
        )},
        {"role": "user", "content": f"问题：{question}"},
    ]
    sql = "".join(_stream_llm(gen_prompt, max_tokens=256)).strip()
    sql = sql.replace("```sql", "").replace("```", "").strip()

    try:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchmany(200)]
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.close()
        return json.dumps({
            "sql":     sql,
            "columns": cols,
            "rows":    rows,
            "total":   len(rows),
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "sql": sql})


# ── 查询改写（RAG 多轮对话）──────────────────────────────────────────────────
def _rewrite_query(question: str, history: list[dict]) -> str:
    """利用对话历史改写查询，使其独立、含义完整"""
    if not history:
        return question
    history_text = "\n".join(
        f"{m['role']}: {m['content'][:100]}" for m in history[-6:]
    )
    prompt = [
        {"role": "system", "content": (
            "根据对话历史，把用户最新问题改写为一个独立完整的搜索查询（中文）。"
            "只输出改写后的查询，不要任何解释。"
        )},
        {"role": "user", "content": f"历史：\n{history_text}\n\n最新问题：{question}"},
    ]
    try:
        return "".join(_stream_llm(prompt, max_tokens=64)).strip() or question
    except Exception:
        return question


# ══════════════════════════════════════════════════════════════════════════════
# SQL Agent
# ══════════════════════════════════════════════════════════════════════════════

def _sql_agent_stream(question: str, history: list[dict]) -> Iterator[dict]:
    # 检查缓存
    cached = _cache_get(question)
    if cached:
        yield {"type": "text", "text": cached.get("answer", "")}
        yield {"type": "done", **cached, "cached": True}
        return

    result_raw = sql_query_tool.invoke(question)
    result = json.loads(result_raw)

    if "error" in result:
        yield {"type": "error", "error": result["error"], "sql": result.get("sql", "")}
        return

    rows    = result.get("rows", [])
    cols    = result.get("columns", [])
    sql     = result.get("sql", "")
    total   = result.get("total", 0)

    if total == 0:
        text = "没有找到符合条件的数据（查询结果为空）。"
        yield {"type": "text", "text": text}
        yield {"type": "done", "agent": "sql_agent", "sql": sql,
               "columns": cols, "rows": rows, "total": 0, "chart": None}
        return

    # 构建摘要 prompt
    rows_preview = json.dumps(rows[:10], ensure_ascii=False)
    summary_prompt = [
        {"role": "system", "content": (
            "你是 B 站数据分析师，根据 SQL 查询结果给出简洁的中文分析摘要（不超过 150 字）。"
        )},
        {"role": "user", "content": (
            f"问题：{question}\nSQL：{sql}\n结果（前10行）：{rows_preview}"
        )},
    ]

    answer_parts = []
    for token in _stream_llm(summary_prompt):
        answer_parts.append(token)
        yield {"type": "text", "text": token}
    answer = "".join(answer_parts)

    # 简单图表类型推断
    chart = None
    if any(w in question for w in ["排行", "排名", "最多", "最少", "前"]):
        chart = "bar"
    elif any(w in question for w in ["趋势", "变化", "年", "月", "增长"]):
        chart = "line"
    elif any(w in question for w in ["占比", "比例", "分布"]):
        chart = "pie"

    done_data = {
        "agent": "sql_agent", "sql": sql,
        "columns": cols, "rows": rows, "total": total, "chart": chart,
    }
    _cache_set(question, {**done_data, "answer": answer})
    yield {"type": "done", **done_data}


# ══════════════════════════════════════════════════════════════════════════════
# RAG Agent
# ══════════════════════════════════════════════════════════════════════════════

def _rag_agent_stream(question: str, history: list[dict]) -> Iterator[dict]:
    # 查询改写
    rewritten = _rewrite_query(question, history)
    if rewritten != question:
        yield {"type": "rewrite", "original": question, "rewritten": rewritten}

    # RAG 检索
    results = rag.search(rewritten, top_k=8)
    if not results:
        yield {"type": "text", "text": "抱歉，没有找到相关视频推荐。"}
        yield {"type": "done", "agent": "rag_agent", "sql": "（RAG检索，无结果）",
               "columns": [], "rows": [], "total": 0, "chart": None}
        return

    context = "\n".join(
        f"- 《{r['title']}》作者：{r.get('author','未知')} "
        f"分区：{r.get('partition','未知')} 年份：{r.get('year','')} "
        f"分数：{r.get('rerank_score', r.get('score',0)):.3f}"
        for r in results[:6]
    )

    rec_prompt = [
        {"role": "system", "content": (
            "你是 B 站内容推荐助手，根据检索到的视频给出个性化推荐理由（中文，150字内）。"
        )},
        {"role": "user", "content": f"用户问题：{rewritten}\n\n候选视频：\n{context}"},
    ]

    answer_parts = []
    for token in _stream_llm(rec_prompt):
        answer_parts.append(token)
        yield {"type": "text", "text": token}

    rows = [
        {k: v for k, v in r.items() if k not in ("hybrid",)}
        for r in results
    ]
    yield {
        "type": "done", "agent": "rag_agent",
        "sql": f"（RAG 混合检索，query='{rewritten}'）",
        "columns": ["title", "author", "partition", "year", "score"],
        "rows": rows, "total": len(rows), "chart": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Chat Agent
# ══════════════════════════════════════════════════════════════════════════════

def _chat_agent_stream(question: str, history: list[dict]) -> Iterator[dict]:
    messages = [
        {"role": "system", "content": (
            "你是 B 站数据分析 AI 助手，友好、专业地回答用户关于 B 站数据的问题。"
        )},
    ]
    for turn in history[-6:]:
        messages.append(turn)
    messages.append({"role": "user", "content": question})

    answer_parts = []
    for token in _stream_llm(messages):
        answer_parts.append(token)
        yield {"type": "text", "text": token}

    yield {
        "type": "done", "agent": "chat_agent",
        "sql": "", "columns": [], "rows": [], "total": 0, "chart": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 统一入口
# ══════════════════════════════════════════════════════════════════════════════

def sql_agent_stream(
    question: str,
    session_id: str = "",
) -> Iterator[dict]:
    """
    主入口：路由 → 执行 agent → 写记忆
    所有 agent 统一从这里调用（保持和 app.py 的接口兼容）
    """
    # 获取历史
    history = mem.get_history(session_id) if session_id else []
    yield {"type": "memory", "turns": len(history) // 2}

    # 路由
    try:
        router = _get_router()
        state  = router.invoke({"question": question})
        agent  = state.get("agent", "chat_agent")
    except Exception as e:
        logger.warning(f"路由失败，降级 chat_agent: {e}")
        agent = "chat_agent"

    yield {"type": "agent", "agent": agent}

    # 执行 agent
    answer_parts = []
    had_error    = False
    done_chunk   = None

    _agent_fn = {
        "sql_agent":  _sql_agent_stream,
        "rag_agent":  _rag_agent_stream,
        "chat_agent": _chat_agent_stream,
    }.get(agent, _chat_agent_stream)

    try:
        for chunk in _agent_fn(question, history):
            if chunk["type"] == "text":
                answer_parts.append(chunk.get("text", ""))
            elif chunk["type"] == "error":
                had_error = True
                yield chunk
                # 错误后降级 chat_agent
                fallback_parts = []
                for fc in _chat_agent_stream(question, history):
                    if fc["type"] == "text":
                        fallback_parts.append(fc.get("text", ""))
                    if fc["type"] == "done":
                        done_chunk = fc
                    yield fc
                answer_parts = fallback_parts
                break
            elif chunk["type"] == "done":
                done_chunk = chunk
            else:
                yield chunk

        if done_chunk is None:
            done_chunk = {
                "type": "done", "agent": agent,
                "sql": "", "columns": [], "rows": [], "total": 0, "chart": None,
            }
        yield done_chunk

    except Exception as e:
        logger.error(f"agent 执行异常: {e}", exc_info=True)
        yield {"type": "error", "error": str(e)}
        yield {"type": "done", "agent": agent,
               "sql": "", "columns": [], "rows": [], "total": 0, "chart": None}
        had_error = True

    # 写记忆
    if session_id and not had_error:
        answer = "".join(answer_parts)
        try:
            mem.add_turn(session_id, question, answer)
        except Exception as e:
            logger.warning(f"写记忆失败: {e}")
