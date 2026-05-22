import sqlite3
import json
import re
import time
import anthropic
import config

client = anthropic.Anthropic(
    api_key=config.ANTHROPIC_API_KEY,
    base_url=config.ANTHROPIC_BASE_URL,
)

MODEL = 'claude-haiku-4-5-20251001'

SCHEMA_SHORT = (
    "表名 HuiZong，列：id,作者,标题,简介,链接,播放量,弹幕量,收藏量,"
    "点赞,评论,转发,投币,粉丝数,时长(秒),分区,投稿时间,data_year(年份)"
)

# 数据类问题关键词
_DATA_KEYWORDS = [
    '播放', '视频', '分区', '作者', 'UP', '点赞', '弹幕', '收藏',
    '排行', '排名', '最高', '最多', '最少', '平均', '统计', '对比',
    '数据', '查询', '多少', '哪个', '哪些', '投币', '评论', '转发',
    '粉丝', '时长', '投稿', '年份', 'top', 'Top', 'TOP',
]

# 查询缓存
_cache: dict = {}
_CACHE_TTL = 1800


def _cache_get(question: str):
    entry = _cache.get(question.strip().lower())
    if entry and time.time() - entry['ts'] < _CACHE_TTL:
        return entry
    return None


def _cache_set(question: str, answer: str, sql: str, columns, rows, total, chart):
    _cache[question.strip().lower()] = {
        'ts': time.time(), 'answer': answer, 'sql': sql,
        'columns': columns, 'rows': rows, 'total': total, 'chart': chart,
    }


def _run_sql(sql: str):
    uri = f'file:{config.DB_PATH}?mode=ro'
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


def _build_chart_option(chart_spec: dict, cols: list, rows: list):
    if not chart_spec:
        return None
    chart_type = chart_spec.get('type', 'bar')
    x_col = chart_spec.get('x_col')
    y_col = chart_spec.get('y_col')
    title = chart_spec.get('title', '')
    if not x_col or not y_col or x_col not in cols or y_col not in cols:
        return None
    xi, yi = cols.index(x_col), cols.index(y_col)
    x_data = [str(r[xi]) for r in rows[:20]]
    y_data = [round(float(r[yi]), 2) if r[yi] is not None else 0 for r in rows[:20]]
    toolbox = {'feature': {'saveAsImage': {'title': '保存图片', 'pixelRatio': 2}}}
    if chart_type == 'pie':
        return {
            'title': {'text': title, 'left': 'center'},
            'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
            'toolbox': toolbox,
            'series': [{'type': 'pie', 'radius': '60%',
                        'data': [{'name': x, 'value': y} for x, y in zip(x_data, y_data)]}]
        }
    return {
        'title': {'text': title},
        'tooltip': {'trigger': 'axis'},
        'toolbox': toolbox,
        'grid': {'bottom': '20%'},
        'xAxis': {'type': 'category', 'data': x_data, 'axisLabel': {'rotate': 30}},
        'yAxis': {'type': 'value'},
        'series': [{'type': chart_type, 'data': y_data, 'itemStyle': {'color': '#4a90e2'}}]
    }


def _auto_chart(sql: str, cols: list, rows: list):
    """GROUP BY 类查询自动加图表"""
    if 'GROUP BY' not in sql.upper() or len(cols) < 2 or len(rows) < 2:
        return {'should_chart': False}
    spec = {'type': 'bar', 'x_col': cols[0], 'y_col': cols[1], 'title': '数据分析'}
    option = _build_chart_option(spec, cols, rows)
    return {'should_chart': True, 'option': option} if option else {'should_chart': False}


def sql_agent_stream(question: str, history: list = []):
    """流式生成器：数据问题直接生成 SQL，普通问题直接聊天"""

    # 缓存命中
    if not history:
        cached = _cache_get(question)
        if cached:
            yield {'type': 'text', 'text': cached['answer']}
            yield {'type': 'done', 'sql': cached['sql'], 'columns': cached['columns'],
                   'rows': cached['rows'], 'total': cached['total'], 'chart': cached['chart']}
            return

    is_data_q = any(kw in question for kw in _DATA_KEYWORDS)

    # ── 普通聊天 ──────────────────────────────────────────────────────────────
    if not is_data_q:
        messages = history + [{'role': 'user', 'content': question}]
        resp = client.messages.create(
            model=MODEL, max_tokens=300,
            system="你是B站数据AI分析助手，可以正常聊天回答各种问题。",
            messages=messages
        )
        answer = ''.join(b.text for b in resp.content if hasattr(b, 'text'))
        for i in range(0, len(answer), 4):
            yield {'type': 'text', 'text': answer[i:i+4]}
        yield {'type': 'done', 'sql': '', 'columns': [], 'rows': [], 'total': 0,
               'chart': {'should_chart': False}}
        return

    # ── 数据查询：直接让 Claude 生成 SQL 文本 ──────────────────────────────────
    sql_prompt = (
        f"你是SQLite专家。根据用户问题生成查询语句。\n"
        f"{SCHEMA_SHORT}\n"
        f"用户问题：{question}\n\n"
        f"只输出SQL语句，不要任何其他文字："
    )
    sql_resp = client.messages.create(
        model=MODEL, max_tokens=200,
        messages=[{'role': 'user', 'content': sql_prompt}]
    )
    sql_text = ''.join(b.text for b in sql_resp.content if hasattr(b, 'text'))
    sql = _extract_sql(sql_text)

    if not sql:
        yield {'type': 'error', 'error': '无法生成 SQL'}
        return

    # 执行 SQL（含一次自动修正）
    cols, rows, error = None, None, None
    for attempt in range(2):
        try:
            cols, rows = _run_sql(sql)
            break
        except Exception as e:
            error = str(e)
            if attempt == 0:
                fix_prompt = (
                    f"以下SQL有错误，请修正：\nSQL: {sql}\n错误: {error}\n"
                    f"{SCHEMA_SHORT}\n只输出修正后的SQL："
                )
                fix_resp = client.messages.create(
                    model=MODEL, max_tokens=200,
                    messages=[{'role': 'user', 'content': fix_prompt}]
                )
                sql_text = ''.join(b.text for b in fix_resp.content if hasattr(b, 'text'))
                sql = _extract_sql(sql_text)

    if error and cols is None:
        yield {'type': 'error', 'error': f'查询失败：{error}', 'sql': sql}
        return

    if not rows:
        yield {'type': 'text', 'text': '数据库中没有符合条件的数据。'}
        yield {'type': 'done', 'sql': sql, 'columns': [], 'rows': [], 'total': 0,
               'chart': {'should_chart': False}}
        return

    # 让 Claude 流式解读结果
    preview = [dict(zip(cols, r)) for r in rows[:10]]
    interpret_prompt = (
        f"用户问：{question}\n"
        f"查询到{len(rows)}条数据，前{len(preview)}条：\n"
        f"{json.dumps(preview, ensure_ascii=False)}\n"
        f"用1-2句中文回答，带具体数字："
    )
    answer_text = ''
    with client.messages.stream(
        model=MODEL, max_tokens=150,
        messages=[{'role': 'user', 'content': interpret_prompt}]
    ) as stream:
        for text in stream.text_stream:
            answer_text += text
            yield {'type': 'text', 'text': text}

    chart = _auto_chart(sql, cols, rows)

    if not history:
        _cache_set(question, answer_text, sql, cols, rows[:50], len(rows), chart)

    yield {'type': 'done', 'sql': sql, 'columns': cols,
           'rows': rows[:50], 'total': len(rows), 'chart': chart}
