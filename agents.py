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


SYSTEM = (
    "你是「B站数据AI分析助手」。"
    "后端已连接真实 SQLite 数据库，你只需生成 SQL 文本，Python 自动执行。"
    "普通聊天用中文简短回答。"
)

# 精简版 schema，只保留列名
SCHEMA_SHORT = (
    "表名 HuiZong，列：id,作者,标题,简介,链接,播放量,弹幕量,收藏量,"
    "点赞,评论,转发,投币,粉丝数,时长(秒),分区,投稿时间,data_year(年份)"
)


def _route_and_sql(question: str) -> dict:
    """第一次调用：路由 + SQL + 预判图表规格（全部合并，压缩 token）"""
    prompt = f"""{SCHEMA_SHORT}
用户：{question}
输出 JSON（只输出 JSON）：
数据库问题→{{"type":"sql","sql":"SELECT...","chart":{{"type":"bar/line/pie","x_col":"列名","y_col":"列名","title":"标题"}} 或 null}}
普通聊天→{{"type":"chat","answer":"中文回答"}}"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=200,
        system=SYSTEM,
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


def _interpret(question: str, cols: list, rows: list) -> str:
    """第二次调用：只做一件事——用 1-2 句话解读查询结果"""
    preview = [dict(zip(cols, r)) for r in rows[:10]]
    prompt = (f"用户问：{question}\n列名：{cols}\n"
              f"数据（前{len(preview)}条/共{len(rows)}条）：{json.dumps(preview, ensure_ascii=False)}\n"
              "用1-2句中文回答，带具体数字，不要废话。")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=150,
        system=SYSTEM,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return resp.content[0].text.strip()


def sql_agent(question: str) -> dict:
    routed = _route_and_sql(question)

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
                    max_tokens=150,
                    system=SYSTEM,
                    messages=[{'role': 'user', 'content':
                        f"SQL报错：{error}\nSQL：{sql}\n{SCHEMA_SHORT}\n只输出修正后的SQL语句。"}]
                )
                sql = _extract_sql(fix_resp.content[0].text)

    if error and cols is None:
        return {'status': 'error', 'error': error, 'sql': sql}

    if not rows:
        answer = '数据库中没有符合条件的数据。'
        chart = {'should_chart': False}
    else:
        answer = _interpret(question, cols, rows)
        chart_option = _build_chart_option(routed.get('chart'), cols, rows)
        chart = {'should_chart': bool(chart_option), 'option': chart_option} if chart_option else {'should_chart': False}

    return {
        'status': 'ok',
        'mode': 'sql',
        'sql': sql,
        'columns': cols,
        'rows': rows[:50] if rows else [],
        'total': len(rows) if rows else 0,
        'answer': answer,
        'chart': chart,
    }
