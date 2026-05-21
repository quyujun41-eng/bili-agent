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


# 第一次调用：生成 SQL
def _get_sql(question: str) -> str:
    prompt = f"""{SCHEMA}

用户问题：{question}

只输出一个 JSON，格式：
{{"sql": "SELECT ..."}}
不要输出其他任何内容。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{'role': 'user', 'content': prompt}]
    )
    raw = resp.content[0].text.strip()
    try:
        return json.loads(raw)['sql']
    except Exception:
        return _extract_sql(raw)


# 第二次调用：解读结果 + 生成图表配置（合并为一次）
def _interpret_and_chart(question: str, cols: list, rows: list) -> dict:
    preview = [dict(zip(cols, r)) for r in rows[:20]]
    prompt = f"""用户问：{question}
查询列名：{cols}
数据（前{len(preview)}条/共{len(rows)}条）：{json.dumps(preview, ensure_ascii=False)}

请输出一个 JSON，包含两个字段：
1. "answer": 用1~3句自然语言回答用户问题，要有具体数字
2. "chart": 若数据适合图表（柱状图/折线图/饼图），输出标准 ECharts option 对象；若不适合则输出 null

格式：
{{
  "answer": "...",
  "chart": {{ECharts option}} 或 null
}}
只输出 JSON，不要其他内容。"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{'role': 'user', 'content': prompt}]
    )
    try:
        return _extract_json(resp.content[0].text.strip())
    except Exception:
        return {'answer': resp.content[0].text.strip(), 'chart': None}


def sql_agent(question: str) -> dict:
    sql = _get_sql(question)

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
        result = {'answer': '数据库中没有符合条件的数据。', 'chart': None}
    else:
        result = _interpret_and_chart(question, cols, rows)

    chart_option = result.get('chart')
    chart = {'should_chart': bool(chart_option), 'option': chart_option} if chart_option else {'should_chart': False}

    return {
        'status': 'ok',
        'sql': sql,
        'columns': cols,
        'rows': rows[:50],
        'total': len(rows),
        'answer': result.get('answer', ''),
        'chart': chart,
    }
