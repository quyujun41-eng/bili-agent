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

SYSTEM = (
    "你是「B站数据AI分析助手」，可以查询B站视频数据，也可以正常聊天。"
    "数据库已连接。重要规则："
    "1. 遇到任何数据/视频/统计/排行相关问题，必须立即调用 execute_sql 工具，"
    "绝对不能用文字说'我来查询'或'我将查询'，要直接调用工具执行。"
    "2. 纯聊天（问候、闲聊、非数据问题）直接文字回答，不调用工具。"
)

# 数据类问题关键词，命中则强制工具调用
_DATA_KEYWORDS = [
    '播放', '视频', '分区', '作者', 'UP', '点赞', '弹幕', '收藏',
    '排行', '排名', '最高', '最多', '最少', '平均', '统计', '对比',
    '数据', '查询', '多少', '哪个', '哪些', '投币', '评论', '转发',
    '粉丝', '时长', '投稿', '年份', 'top', 'Top', 'TOP',
]

SCHEMA_SHORT = (
    "表名 HuiZong，列：id,作者,标题,简介,链接,播放量,弹幕量,收藏量,"
    "点赞,评论,转发,投币,粉丝数,时长(秒),分区,投稿时间,data_year(年份)"
)

# Claude 原生 Tool Use 定义
TOOLS = [
    {
        "name": "execute_sql",
        "description": f"查询B站视频数据库。{SCHEMA_SHORT}",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "要执行的 SELECT SQL 语句"
                },
                "chart": {
                    "type": "object",
                    "description": "图表规格（可选，适合可视化时提供）",
                    "properties": {
                        "type":  {"type": "string", "enum": ["bar", "line", "pie"]},
                        "x_col": {"type": "string", "description": "X轴列名（原文）"},
                        "y_col": {"type": "string", "description": "Y轴列名（原文）"},
                        "title": {"type": "string", "description": "图表标题"}
                    },
                    "required": ["type", "x_col", "y_col", "title"]
                }
            },
            "required": ["sql"]
        }
    }
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
    m = re.search(r'(SELECT\s+.*?;)', text, re.DOTALL | re.IGNORECASE)
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


def sql_agent_stream(question: str, history: list = []):
    """原生 Tool Use 流式生成器"""

    # 缓存命中
    if not history:
        cached = _cache_get(question)
        if cached:
            yield {'type': 'text', 'text': cached['answer']}
            yield {'type': 'done', 'sql': cached['sql'], 'columns': cached['columns'],
                   'rows': cached['rows'], 'total': cached['total'], 'chart': cached['chart']}
            return

    messages = history + [{'role': 'user', 'content': question}]

    # 数据类问题强制调用工具，纯聊天让 Claude 自行判断
    is_data_q = any(kw in question for kw in _DATA_KEYWORDS)
    tool_choice = {"type": "any"} if is_data_q else {"type": "auto"}

    # Call 1：让 Claude 决定是聊天还是调用 SQL 工具
    resp = client.messages.create(
        model=MODEL, max_tokens=300,
        system=SYSTEM, tools=TOOLS,
        tool_choice=tool_choice,
        messages=messages
    )

    # 纯聊天且没调用工具时重试一次（保底）
    if resp.stop_reason != 'tool_use' and is_data_q:
        retry_messages = history + [{'role': 'user', 'content': f"用execute_sql工具查询：{question}"}]
        resp = client.messages.create(
            model=MODEL, max_tokens=300,
            system=SYSTEM, tools=TOOLS,
            tool_choice={"type": "any"},
            messages=retry_messages
        )

    # 纯聊天：Claude 没有调用工具，直接流式输出回答
    if resp.stop_reason != 'tool_use':
        answer = ''.join(
            b.text for b in resp.content if hasattr(b, 'text')
        )
        # 模拟流式（字符分块输出）
        chunk_size = 4
        for i in range(0, len(answer), chunk_size):
            yield {'type': 'text', 'text': answer[i:i+chunk_size]}
        yield {'type': 'done', 'sql': '', 'columns': [], 'rows': [], 'total': 0,
               'chart': {'should_chart': False}}
        return

    # 找到 tool_use 块
    tool_use_block = next(b for b in resp.content if b.type == 'tool_use')
    tool_input = tool_use_block.input
    sql = tool_input.get('sql', '')
    chart_spec = tool_input.get('chart')

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
                # 把错误反馈给 Claude，让它修正（标准 tool_result 格式）
                fix_messages = messages + [
                    {'role': 'assistant', 'content': resp.content},
                    {'role': 'user', 'content': [{
                        'type': 'tool_result',
                        'tool_use_id': tool_use_block.id,
                        'content': f'SQL执行报错：{error}，请修正SQL后重新调用工具。',
                        'is_error': True
                    }]}
                ]
                fix_resp = client.messages.create(
                    model=MODEL, max_tokens=200,
                    system=SYSTEM, tools=TOOLS,
                    messages=fix_messages
                )
                if fix_resp.stop_reason == 'tool_use':
                    fix_block = next(b for b in fix_resp.content if b.type == 'tool_use')
                    sql = fix_block.input.get('sql', sql)
                    tool_use_block = fix_block
                    resp = fix_resp

    if error and cols is None:
        yield {'type': 'error', 'error': f'查询失败：{error}', 'sql': sql}
        return

    if not rows:
        yield {'type': 'text', 'text': '数据库中没有符合条件的数据。'}
        yield {'type': 'done', 'sql': sql, 'columns': [], 'rows': [], 'total': 0,
               'chart': {'should_chart': False}}
        return

    # Call 2：把 tool_result 发回给 Claude，流式输出解读
    preview = [dict(zip(cols, r)) for r in rows[:10]]
    tool_result_messages = messages + [
        {'role': 'assistant', 'content': resp.content},
        {'role': 'user', 'content': [{
            'type': 'tool_result',
            'tool_use_id': tool_use_block.id,
            'content': (f"查询成功，列名：{cols}，"
                        f"共{len(rows)}条，前{len(preview)}条数据："
                        f"{json.dumps(preview, ensure_ascii=False)}\n"
                        f"请用1-2句中文回答用户，带具体数字。")
        }]}
    ]

    answer_text = ''
    with client.messages.stream(
        model=MODEL, max_tokens=150, system=SYSTEM,
        messages=tool_result_messages
    ) as stream:
        for text in stream.text_stream:
            answer_text += text
            yield {'type': 'text', 'text': text}

    chart_option = _build_chart_option(chart_spec, cols, rows)
    chart = {'should_chart': True, 'option': chart_option} if chart_option else {'should_chart': False}

    if not history:
        _cache_set(question, answer_text, sql, cols, rows[:50], len(rows), chart)

    yield {'type': 'done', 'sql': sql, 'columns': cols,
           'rows': rows[:50], 'total': len(rows), 'chart': chart}
