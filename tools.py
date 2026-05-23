"""
LangChain Tools —— 把 SQL查询 和 RAG语义搜索 封装成标准工具
供多Agent调度器调用
"""
import json
import sqlite3
import re
from langchain_core.tools import tool
import config
import rag


# ── SQL 工具 ─────────────────────────────────────────────────────────────────

SCHEMA_SHORT = (
    "表名 HuiZong，列：id,作者,标题,简介,链接,播放量,弹幕量,收藏量,"
    "点赞,评论,转发,投币,粉丝数,时长,分区,投稿时间,data_year(年份)"
)


def _run_sql(sql: str):
    uri  = f'file:{config.DB_PATH}?mode=ro'
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


@tool
def sql_query_tool(question: str) -> str:
    """
    用于精确数据查询：统计、排行、数量、平均值、对比等需要数字的问题。
    输入：自然语言问题。输出：查询结果的JSON字符串。
    """
    from llm_client import get_client, get_model
    client = get_client()
    model  = get_model()

    sql_prompt = (
        f"你是SQLite专家。根据用户问题生成查询语句。\n"
        f"{SCHEMA_SHORT}\n"
        f"用户问题：{question}\n\n"
        f"只输出SQL语句，不要任何其他文字："
    )
    resp = client.chat.completions.create(
        model=model, max_tokens=300,
        messages=[{'role': 'user', 'content': sql_prompt}]
    )
    sql_text = resp.choices[0].message.content
    sql      = _extract_sql(sql_text)

    if not sql:
        return json.dumps({'error': '无法生成SQL', 'rows': []}, ensure_ascii=False)

    # 执行（含一次自动修正）
    for attempt in range(2):
        try:
            cols, rows = _run_sql(sql)
            preview = [dict(zip(cols, r)) for r in rows[:20]]
            return json.dumps({
                'sql': sql, 'total': len(rows),
                'columns': cols, 'rows': preview
            }, ensure_ascii=False, default=str)
        except Exception as e:
            if attempt == 1:
                return json.dumps({'error': str(e), 'sql': sql}, ensure_ascii=False)
            fix_prompt = (
                f"以下SQL有错误，请修正：\nSQL: {sql}\n错误: {e}\n"
                f"{SCHEMA_SHORT}\n只输出修正后的SQL："
            )
            fix_resp = client.chat.completions.create(
                model=model, max_tokens=200,
                messages=[{'role': 'user', 'content': fix_prompt}]
            )
            sql = _extract_sql(fix_resp.choices[0].message.content)

    return json.dumps({'error': '查询失败'}, ensure_ascii=False)


# ── RAG 语义搜索工具 ──────────────────────────────────────────────────────────

@tool
def rag_search_tool(query: str) -> str:
    """
    用于语义搜索：视频推荐、相似内容、模糊描述查找等问题。
    输入：搜索关键词或描述。输出：最相关视频列表的JSON字符串。
    """
    results = rag.search(query, top_k=8)
    return json.dumps(results, ensure_ascii=False, default=str)


# 工具列表（供 orchestrator 注册）
ALL_TOOLS = [sql_query_tool, rag_search_tool]
