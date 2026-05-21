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

SYSTEM = (
    "你是「B站数据AI分析助手」。"
    "后端已连接真实 SQLite 数据库，你只需生成 SQL 文本，Python 自动执行。"
    "普通聊天用中文简短回答。"
)

SCHEMA_SHORT = (
    "表名 HuiZong，列：id,作者,标题,简介,链接,播放量,弹幕量,收藏量,"
    "点赞,评论,转发,投币,粉丝数,时长(秒),分区,投稿时间,data_year(年份)"
)

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
        'series': [{'type': chart_type, 'data': y_data, 'itemStyle': {'color': '#4a90e2'}}]
    }


def _route_and_sql(question: str, history: list) -> dict:
    prompt = (f"{SCHEMA_SHORT}\n用户：{question}\n"
              "输出 JSON（只输出 JSON）：\n"
              f'数据库问题→{{"type":"sql","sql":"SELECT...","chart":{{"type":"bar/line/pie","x_col":"列名","y_col":"列名","title":"标题"}}或null}}\n'
              f'普通聊天→{{"type":"chat"}}')
    messages = history + [{'role': 'user', 'content': prompt}]
    resp = client.messages.create(
        model=MODEL, max_tokens=200, system=SYSTEM,
        messages=messages
    )
    try:
        return _extract_json(resp.content[0].text.strip())
    except Exception:
        return {'type': 'chat'}


def sql_agent_stream(question: str, history: list = []):
    """流式生成器，yield SSE 数据块"""
    routed = _route_and_sql(question, history)

    if routed.get('type') == 'chat':
        # 聊天模式：流式输出回答（带历史）
        with client.messages.stream(
            model=MODEL, max_tokens=300, system=SYSTEM,
            messages=history + [{'role': 'user', 'content': question}]
        ) as stream:
            for text in stream.text_stream:
                yield {'type': 'text', 'text': text}
        yield {'type': 'done', 'sql': '', 'columns': [], 'rows': [], 'total': 0,
               'chart': {'should_chart': False}}
        return

    # 数据库模式
    sql = routed.get('sql', '')
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
                fix = client.messages.create(
                    model=MODEL, max_tokens=150, system=SYSTEM,
                    messages=[{'role': 'user', 'content':
                        f"SQL报错：{error}\nSQL：{sql}\n{SCHEMA_SHORT}\n只输出修正SQL。"}]
                )
                sql = _extract_sql(fix.content[0].text)

    if error and cols is None:
        yield {'type': 'error', 'error': f'查询失败：{error}', 'sql': sql}
        return

    if not rows:
        yield {'type': 'text', 'text': '数据库中没有符合条件的数据。'}
        yield {'type': 'done', 'sql': sql, 'columns': [], 'rows': [], 'total': 0,
               'chart': {'should_chart': False}}
        return

    # 流式输出解读
    preview = [dict(zip(cols, r)) for r in rows[:10]]
    interp_prompt = (f"用户问：{question}\n列名：{cols}\n"
                     f"数据（前{len(preview)}条/共{len(rows)}条）："
                     f"{json.dumps(preview, ensure_ascii=False)}\n"
                     "用1-2句中文回答，带具体数字，不要废话。")
    with client.messages.stream(
        model=MODEL, max_tokens=150, system=SYSTEM,
        messages=[{'role': 'user', 'content': interp_prompt}]
    ) as stream:
        for text in stream.text_stream:
            yield {'type': 'text', 'text': text}

    chart_option = _build_chart_option(routed.get('chart'), cols, rows)
    chart = {'should_chart': True, 'option': chart_option} if chart_option else {'should_chart': False}
    yield {'type': 'done', 'sql': sql, 'columns': cols,
           'rows': rows[:50], 'total': len(rows), 'chart': chart}
