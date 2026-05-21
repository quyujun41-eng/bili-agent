import sqlite3
import json
import re
import anthropic
import config

client = anthropic.Anthropic(
    api_key=config.ANTHROPIC_API_KEY,
    base_url=config.ANTHROPIC_BASE_URL,
)

MODEL = 'claude-sonnet-4-6'

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


def sql_agent(question: str) -> dict:
    """Agent 1：自然语言 → SQL → 执行 → 自然语言解读"""
    sql_prompt = f"""{SCHEMA}

用户问题：{question}

只输出一个 JSON，格式：
{{"sql": "SELECT ...", "explanation": "我在查什么"}}
不要输出其他任何内容。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{'role': 'user', 'content': sql_prompt}]
    )
    raw = resp.content[0].text.strip()

    try:
        parsed = json.loads(raw)
        sql = parsed['sql']
        explanation = parsed.get('explanation', '')
    except Exception:
        sql = _extract_sql(raw)
        explanation = ''

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
        answer = '数据库中没有符合条件的数据。'
    else:
        preview = rows[:10]
        interp_resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{'role': 'user', 'content':
                f"用户问：{question}\n查询结果列名：{cols}\n数据（前{len(preview)}条/共{len(rows)}条）：{preview}\n"
                f"用1~3句自然语言回答，要有具体数字，不要照抄原始数据。"}]
        )
        answer = interp_resp.content[0].text.strip()

    return {
        'status': 'ok',
        'sql': sql,
        'explanation': explanation,
        'columns': cols,
        'rows': rows[:50],
        'total': len(rows),
        'answer': answer,
    }


def chart_agent(question: str, columns: list, rows: list) -> dict:
    """Agent 2：根据查询结果决定是否画图，并生成 ECharts 配置"""
    if not rows or not columns:
        return {'should_chart': False}

    data_preview = [dict(zip(columns, r)) for r in rows[:20]]

    chart_prompt = f"""用户问题：{question}
查询结果列名：{columns}
数据示例（前{len(data_preview)}条）：{json.dumps(data_preview, ensure_ascii=False)}

判断是否适合用图表展示（柱状图/折线图/饼图）。
若适合，输出 JSON：
{{
  "should_chart": true,
  "chart_type": "bar/line/pie",
  "title": "图表标题",
  "x_col": "用哪列做X轴或饼图label（列名原文）",
  "y_col": "用哪列做Y轴或饼图value（列名原文）"
}}
若不适合（纯文本结果、只有1行等），输出：
{{"should_chart": false}}
只输出 JSON，不要其他内容。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=256,
        messages=[{'role': 'user', 'content': chart_prompt}]
    )
    try:
        spec = json.loads(resp.content[0].text.strip())
    except Exception:
        return {'should_chart': False}

    if not spec.get('should_chart'):
        return {'should_chart': False}

    x_col = spec.get('x_col', columns[0])
    y_col = spec.get('y_col', columns[-1])
    chart_type = spec.get('chart_type', 'bar')
    title = spec.get('title', question[:20])

    x_data = [str(r[columns.index(x_col)]) if x_col in columns else '' for r in rows[:20]]
    y_data = [r[columns.index(y_col)] if y_col in columns else 0 for r in rows[:20]]

    if chart_type == 'pie':
        option = {
            'title': {'text': title, 'left': 'center'},
            'tooltip': {'trigger': 'item'},
            'series': [{'type': 'pie', 'radius': '60%',
                        'data': [{'name': x, 'value': y} for x, y in zip(x_data, y_data)]}]
        }
    else:
        option = {
            'title': {'text': title},
            'tooltip': {'trigger': 'axis'},
            'grid': {'bottom': '20%'},
            'xAxis': {'type': 'category', 'data': x_data, 'axisLabel': {'rotate': 30}},
            'yAxis': {'type': 'value'},
            'series': [{'type': chart_type, 'data': y_data,
                        'itemStyle': {'color': '#4a90e2'}}]
        }

    return {'should_chart': True, 'chart_type': chart_type, 'option': option}
