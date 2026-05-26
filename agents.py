import json, time, config, rag, memory as mem
from llm_client import get_client, get_model, get_anthropic_client
from tools import sql_query_tool
import graph as ag

_cache = {}
_CACHE_TTL = 1800

def _cache_get(key):
    e = _cache.get(key.strip().lower())
    if e and time.time() - e["ts"] < _CACHE_TTL:
        return e
    return None

def _cache_set(key, data):
    _cache[key.strip().lower()] = {"ts": time.time(), **data}

def _auto_chart(sql, cols, rows):
    if "GROUP BY" not in sql.upper() or len(cols) < 2 or len(rows) < 2:
        return {"should_chart": False}
    num_cols = [i for i, c in enumerate(cols) if i > 0 and rows and all(
        isinstance(r[i], (int, float)) for r in rows[:5] if r[i] is not None)]
    x_data = [str(r[0]) for r in rows[:20]]
    if len(num_cols) >= 2:
        colors = ["#4a90e2","#e2904a","#4ae2a0","#e24a90"]
        series = [{"name":str(cols[ci]),"type":"bar","data":[round(float(r[ci]),2) if r[ci] is not None else 0 for r in rows[:20]],"itemStyle":{"color":colors[idx%len(colors)]}} for idx,ci in enumerate(num_cols[:4])]
        return {"should_chart":True,"option":{"title":{"text":"数据对比分析"},"tooltip":{"trigger":"axis"},"legend":{"data":[str(cols[ci]) for ci in num_cols[:4]]},"toolbox":{"feature":{"saveAsImage":{"title":"保存"}}},"grid":{"bottom":"25%"},"xAxis":{"type":"category","data":x_data,"axisLabel":{"rotate":30}},"yAxis":{"type":"value"},"series":series}}
    y_data = [round(float(r[1]),2) if r[1] is not None else 0 for r in rows[:20]]
    return {"should_chart":True,"option":{"title":{"text":"数据分析"},"tooltip":{"trigger":"axis"},"toolbox":{"feature":{"saveAsImage":{"title":"保存"}}},"grid":{"bottom":"20%"},"xAxis":{"type":"category","data":x_data,"axisLabel":{"rotate":30}},"yAxis":{"type":"value"},"series":[{"type":"bar","data":y_data,"itemStyle":{"color":"#4a90e2"}}]}}

def _stream_llm(prompt, history, system=None, max_tokens=300):
    msgs = list(history) + [{"role":"user","content":prompt}]
    if config.LLM_PROVIDER == "claude":
        ac = get_anthropic_client()
        kwargs = dict(model=config.CLAUDE_MODEL, max_tokens=max_tokens, messages=msgs)
        if system: kwargs["system"] = system
        with ac.messages.stream(**kwargs) as s:
            for text in s.text_stream: yield text
    else:
        if system: msgs = [{"role":"system","content":system}] + msgs
        client = get_client()
        stream = client.chat.completions.create(model=get_model(),max_tokens=max_tokens,stream=True,messages=msgs)
        for chunk in stream:
            text = chunk.choices[0].delta.content or ""
            if text: yield text


def _rewrite_query(question: str, history: list) -> str:
    """多轮上下文感知查询改写：将含隐含上下文的追问改写为独立检索词"""
    if not history:
        return question
    recent = history[-4:]
    prompt = (
        "根据对话历史，将用户最新问题改写为独立的检索查询（不超过25字，无代词）。"
        "只输出改写后的查询：\n"
        f"历史：{json.dumps(recent, ensure_ascii=False)}\n"
        f"问题：{question}"
    )
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=get_model(), max_tokens=60,
            messages=[{"role":"user","content":prompt}]
        )
        r = resp.choices[0].message.content.strip()
        return r if r else question
    except Exception:
        return question


def _sql_agent_stream(question, history):
    result = json.loads(sql_query_tool.invoke(question))
    if "error" in result:
        yield {"type":"error","error":result["error"]}
        return
    cols=result.get("columns",[]);rows=result.get("rows",[]);sql=result.get("sql","");total=result.get("total",0)
    rows_raw = [[v for v in r.values()] for r in rows[:50]] if rows else []
    if not rows:
        yield {"type":"text","text":"数据库中没有符合条件的数据。"}
        yield {"type":"done","sql":sql,"columns":[],"rows":[],"total":0,"chart":{"should_chart":False},"agent":"sql_agent"}
        return
    if total <= 20:
        prompt = "用户问："+question+"\n查到"+str(total)+"条数据：\n"+json.dumps(rows,ensure_ascii=False)+"\n逐条列出关键字段（排名/标题/作者/核心数值），简洁："
        max_tok = 1500
    else:
        prompt = "用户问："+question+"\n共"+str(total)+"条，前30条：\n"+json.dumps(rows[:30],ensure_ascii=False)+"\n分析趋势，列出最重要前10条（带数字），最后1-2句总结："
        max_tok = 800
    for text in _stream_llm(prompt, history, max_tokens=max_tok): yield {"type":"text","text":text}
    yield {"type":"done","sql":sql,"columns":cols,"rows":rows_raw,"total":total,"chart":_auto_chart(sql,cols,rows_raw),"agent":"sql_agent"}


def _rag_agent_stream(question, history):
    # Step 1: 查询改写（上下文感知）
    search_query = _rewrite_query(question, history)
    if search_query != question:
        yield {"type":"rewrite","original":question,"rewritten":search_query}

    # Step 2: 混合检索
    results = rag.search(search_query, top_k=8)
    if not results:
        yield {"type":"text","text":"没有找到相关视频。"}
        yield {"type":"done","sql":"","columns":[],"rows":[],"total":0,"chart":{"should_chart":False},"agent":"rag_agent"}
        return

    hybrid_flag = results[0].get("hybrid", False)
    mode_note = "（混合检索）" if hybrid_flag else "（语义检索）"
    rewrite_note = f"（查询已改写：{search_query}）" if search_query != question else ""

    # Step 3: 用原始问题生成回答（保留用户意图）
    prompt = ("用户问："+question+rewrite_note+"\n搜索结果"+mode_note+"：\n"
              +json.dumps(results,ensure_ascii=False)+"\n用中文推荐最相关视频，说明理由：")
    for text in _stream_llm(prompt, history): yield {"type":"text","text":text}

    rows = [[r.get("id",""),r.get("title",""),r.get("author",""),r.get("partition",""),r.get("year",""),round(r.get("score",0)*100,1)] for r in results]
    cols = ["id","标题","作者","分区","年份","相似度(%)"]
    yield {"type":"done","sql":"（RAG"+mode_note+"）","columns":cols,"rows":rows,"total":len(rows),"chart":{"should_chart":False},"agent":"rag_agent"}


def _chat_agent_stream(question, history):
    system = "你是B站数据AI分析助手，用中文回答各种问题。"
    for text in _stream_llm(question, history, system=system): yield {"type":"text","text":text}
    yield {"type":"done","sql":"","columns":[],"rows":[],"total":0,"chart":{"should_chart":False},"agent":"chat_agent"}


def sql_agent_stream(question: str, session_id: str = ""):
    history = mem.get_history(session_id) if session_id else []
    if not history:
        cached = _cache_get(question)
        if cached:
            yield {"type":"agent","agent":cached.get("agent","sql_agent")}
            yield {"type":"memory","turns":0}
            yield {"type":"text","text":cached["answer"]}
            yield {"type":"done",**{k:cached[k] for k in ["sql","columns","rows","total","chart","agent"]}}
            return
    agent_name = ag.route(question, history)
    yield {"type":"agent","agent":agent_name}
    yield {"type":"memory","turns":len(history)//2}
    gen_map = {"sql_agent":_sql_agent_stream,"rag_agent":_rag_agent_stream,"chat_agent":_chat_agent_stream}
    answer_text = "";final_done = None;had_error = False
    for chunk in gen_map[agent_name](question, history):
        if chunk["type"] == "text": answer_text += chunk["text"]
        elif chunk["type"] == "done": final_done = chunk
        elif chunk["type"] == "error":
            yield chunk  # 先把 error 推给前端
            had_error = True
            fallback = ag.route(question, history, error=chunk["error"])
            yield {"type":"agent","agent":fallback}
            answer_text = ""
            for fc in gen_map[fallback](question, history):
                if fc["type"] == "text": answer_text += fc["text"]
                elif fc["type"] == "done": final_done = fc
                yield fc
            break
        yield chunk
    if final_done and session_id and answer_text:
        mem.add_turn(session_id, question, answer_text)
    if final_done and not history and not had_error:
        _cache_set(question, {"answer":answer_text,"sql":final_done.get("sql",""),"columns":final_done.get("columns",[]),"rows":final_done.get("rows",[]),"total":final_done.get("total",0),"chart":final_done.get("chart",{"should_chart":False}),"agent":agent_name})
