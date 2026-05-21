import sqlite3
import json
import re
import anthropic
import config

client = anthropic.Anthropic(
    api_key=config.ANTHROPIC_API_KEY,
    base_url=config.ANTHROPIC_BASE_URL,
)

MODEL = 'claude-haiku-4-5-20251001'

SCHEMA = """
数据库：SQLite，表名：HuiZong（B站视频数据）

列名（全部中文，SQL中必须原样使用）：
  id          INTEGER  主键
  作者         TEXT     UP主名字
  标题         TEXT     视频标题
  简介         TEXT     视频简介
  链接         TEXT     视频URL
  播放量        FLOAT    播放次数
  弹幕量        FLOAT    弹幕数量
  收藏量        FLOAT    收藏数
  点赞         FLOAT    点赞数
  评论         FLOAT    评论数
  转发         FLOAT    转发数
  投币         FLOAT    投币数
  粉丝数        FLOAT    UP主粉丝数
  时长         FLOAT    视频时长（秒）
  分区         TEXT     视频分类，如：搞笑、美食制作、游戏、音乐、科技等
  投稿时间      DATETIME 发布时间
  data_year    INTEGER  数据年份（2023/2024/2025/2026）

业务词汇：
  "最受欢迎"/"最火" = 播放量最高
  "互动最好" = 点赞+评论+转发+投币 之和最高
  "涨粉潜力" = 粉丝数/播放量 比值最高
"""


def _run_sql(sql: str):
    conn = sqlite3.connect(config.DB_PATH)
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


def _extract_json(text: str) -> dict:
    m = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
    raw = m.group(1) if m else text
    raw = re.sub(r'//.*', '', raw)
    return json.loads(raw)


def _route_and_sql(question: str) -> dict:
    """第一次调用：判断是否需要查数据库，若需要则同时生成 SQL"""
    system = (
        "你是「B站数据AI分析助手」，一个部署在服务器上的智能助手。"
        "你的后端已经连接了真实的 SQLite 数据库，你只需要输出 SQL 语句，"
        "Python 程序会自动执行并把结果返回给用户。"
        "你不需要自己执行任何查询，只需生成正确的 SQL 文本即可。"
        "对于普通聊天，用中文友好回答。"
    )
    prompt = f"""{SCHEMA}

用户说：{question}

判断用户意图并输出 JSON：
- 如果用户在问 B站数据相关问题（需要查数据库），输出：
  {{"type": "sql", "sql": "SELECT ..."}}
- 如果是普通聊天、闲聊、问你是谁、问其他知识等，输出：
  {{"type": "chat", "answer": "你的中文回答"}}

只输出 JSON，不要其他内容。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=system,
        messages=[{'role': 'user', 'content': prompt}]
    )
    try:
        return _extract_json(resp.content[0].text.strip())
    except Exception:
        return {'type': 'chat', 'answer': resp.content[0].text.strip()}


def _build_chart_option(chart_spec: dict, cols: list, rows: list) -> dict | None:
    """根据 Claude 给的图表规格，用真实数据在 Python 侧组装 ECharts option"""
    if not chart_spec:
        return None
    chart_type = chart_spec.get('type', 'bar')
    x_col = chart_spec.get('x_col')
    y_col = chart_spec.get('y_col')
    title = chart_spec.get('title', '')
    if x_col not in cols or y_col not in cols:
        return None
    xi = cols.index(x_col)
    yi = cols.index(y_col)
    x_data = [str(r[xi]) for r in rows[:20]]
    y_data = [round(float(r[yi]), 2) if r[yi] is not None else 0 for r in rows[:20]]
    if chart_type == 'pie':
        return {
            'title': {'text': title, 'left': 'center'},
            'tooltip': {'trigger': 'item', 'formatter': '{b}: {c} ({d}%)'},
            'series': [{'type': 'pie', 'radius': '60%',
                        'data': [{'name': x, 'value': y} for x, y in zip(x_data, y_data)]}]
        }
    return {
        'title': {'text': title},
        'tooltip': {'trigger': 'axis'},
        'grid': {'bottom': '20%'},
        'xAxis': {'type': 'category', 'data': x_data, 'axisLabel': {'rotate': 30}},
        'yAxis': {'type': 'value'},
        'series': [{'type': chart_type, 'data': y_data,
                    'itemStyle': {'color': '#4a90e2'}}]
    }


def _interpret_and_chart(question: str, cols: list, rows: list) -> dict:
    """第二次调用（仅数据库问题）：解读结果 + 给出图表规格"""
    preview = [dict(zip(cols, r)) for r in rows[:20]]
    prompt = f"""用户问：{question}
查询列名：{cols}
数据（前{len(preview)}条/共{len(rows)}条）：{json.dumps(preview, ensure_ascii=False)}

请输出一个 JSON：
{{
  "answer": "用1~3句中文回答用户问题，要有具体数字",
  "chart": {{
    "type": "bar 或 line 或 pie",
    "title": "图表标题",
    "x_col": "X轴用哪列（必须是列名原文）",
    "y_col": "Y轴用哪列（必须是列名原文）"
  }} 或 null（若不适合图表）
}}
只输出 JSON。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{'role': 'user', 'content': prompt}]
    )
    try:
        result = _extract_json(resp.content[0].text.strip())
        chart_option = _build_chart_option(result.get('chart'), cols, rows)
        return {'answer': result.get('answer', ''), 'chart_option': chart_option}
    except Exception:
        return {'answer': resp.content[0].text.strip(), 'chart_option': None}


def sql_agent(question: str) -> dict:
    routed = _route_and_sql(question)

    # 普通聊天，直接返回
    if routed.get('type') == 'chat':
        return {
            'status': 'ok',
            'mode': 'chat',
            'answer': routed.get('answer', ''),
            'sql': '',
            'columns': [],
            'rows': [],
            'total': 0,
            'chart': {'should_chart': False},
        }

    # 数据库查询
    sql = routed.get('sql', '')
    if not sql:
        return {'status': 'error', 'error': '无法生成 SQL', 'sql': ''}

    cols, rows, error = None, None, None
    for attempt in range(2):
        try:
            cols, rows = _run_sql(sql)
            break
        except Exception as e:
            error = str(e)
            if attempt == 0:
                fix_resp = client.messages.create(
                    model=MODEL,
                    max_tokens=256,
                    messages=[{'role': 'user', 'content':
                        f"SQL报错：{error}\nSQL：{sql}\n{SCHEMA}\n修正SQL，只输出SQL语句。"}]
                )
                sql = _extract_sql(fix_resp.content[0].text)

    if error and cols is None:
        return {'status': 'error', 'error': error, 'sql': sql}

    if not rows:
        result = {'answer': '数据库中没有符合条件的数据。', 'chart_option': None}
    else:
        result = _interpret_and_chart(question, cols, rows)

    chart_option = result.get('chart_option')
    chart = {'should_chart': bool(chart_option), 'option': chart_option} if chart_option else {'should_chart': False}

    return {
        'status': 'ok',
        'mode': 'sql',
        'sql': sql,
        'columns': cols,
        'rows': rows[:50],
        'total': len(rows),
        'answer': result.get('answer', ''),
        'chart': chart,
    }
