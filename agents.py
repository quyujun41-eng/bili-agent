import json, re, time, config, rag
from llm_client import get_client, get_model, get_anthropic_client
from tools import sql_query_tool, rag_search_tool

_cache = {}
_CACHE_TTL = 1800

def _cache_get(key):
    entry = _cache.get(key.strip().lower())
    if entry and time.time() - entry['ts'] < _CACHE_TTL:
        return entry
    return None

def _cache_set(key, data):
    _cache[key.strip().lower()] = {'ts': time.time(), **data}

def _auto_chart(sql, cols, rows):
    if 'GROUP BY' not in sql.upper() or len(cols) < 2 or len(rows) < 2:
        return {'should_chart': False}
    x_data = [str(r[0]) for r in rows[:20]]
    y_data = [round(float(r[1]), 2) if r[1] is not None else 0 for r in rows[:20]]
    return {'should_chart': True, 'option': {
        'title': {'text': '数据分析'},
        'tooltip': {'trigger': 'axis'},
        'toolbox': {'feature': {'saveAsImage': {'title': '保存'}}},
        'grid': {'bottom': '20%'},
        'xAxis': {'type': 'category', 'data': x_data, 'axisLabel': {'rotate': 30}},
        'yAxis': {'type': 'value'},
        'series': [{'type': 'bar', 'data': y_data, 'itemStyle': {'color': '#4a90e2'}}]
    }}

ROUTE_SYSTEM = '\n'.join([
    '你是智能路由器，判断用户问题由哪个Agent处理。',
    '可用Agent：',
    '- sql_agent：排行、统计、数量、平均值、对比数字等精确查询',
    '- rag_agent：推荐视频、找相关内容、语义搜索',
    '- chat_agent：闲聊、解释、建议',
    '只输出Agent名称。'
])

def _route(question):
    client = get_client()
    resp = client.chat.completions.create(
        model=get_model(), max_tokens=20,
        messages=[{'role': 'system', 'content': ROUTE_SYSTEM},
                  {'role': 'user', 'content': question}]
    )
    agent = resp.choices[0].message.content.strip().lower()
    if 'rag' in agent: return 'rag_agent'
    if 'sql' in agent: return 'sql_agent'
    return 'chat_agent'

def _stream_llm(prompt, history, system=None, max_tokens=300):
    msgs = []
    if system and config.LLM_PROVIDER != 'claude':
        msgs.append({'role': 'system', 'content': system})
    msgs += history + [{'role': 'user', 'content': prompt}]
    if config.LLM_PROVIDER == 'claude':
        ac = get_anthropic_client()
        kwargs = dict(model=config.CLAUDE_MODEL, max_tokens=max_tokens, messages=msgs)
        if system:
            kwargs['system'] = system
        with ac.messages.stream(**kwargs) as s:
            for text in s.text_stream:
                yield text
    else:
        client = get_client()
        stream = client.chat.completions.create(
            model=get_model(), max_tokens=max_tokens, stream=True, messages=msgs
        )
        for chunk in stream:
            text = chunk.choices[0].delta.content or ''
            if text: yield text

def _sql_agent_stream(question, history):
    result = json.loads(sql_query_tool.invoke(question))
    if 'error' in result:
        yield {'type': 'error', 'error': result['error']}
        return
    cols  = result.get('columns', [])
    rows  = result.get('rows', [])
    sql   = result.get('sql', '')
    total = result.get('total', 0)
    if not rows:
        yield {'type': 'text', 'text': '数据库中没有符合条件的数据。'}
        yield {'type': 'done', 'sql': sql, 'columns': [], 'rows': [],
               'total': 0, 'chart': {'should_chart': False}, 'agent': 'sql_agent'}
        return
    prompt = ('用户问：' + question + '\n找到' + str(total) + '条，前' +
              str(len(rows[:10])) + '条：' +
              json.dumps(rows[:10], ensure_ascii=False) +
              '\n用中文回答，带具体数字：')
    answer = ''
    for text in _stream_llm(prompt, []):
        answer += text
        yield {'type': 'text', 'text': text}
    rows_raw = [[v for v in r.values()] for r in rows[:50]] if rows else []
    chart = _auto_chart(sql, cols, rows_raw)
    yield {'type': 'done', 'sql': sql, 'columns': cols, 'rows': rows[:50],
           'total': total, 'chart': chart, 'agent': 'sql_agent'}

def _rag_agent_stream(question, history):
    results = json.loads(rag_search_tool.invoke(question))
    if not results:
        yield {'type': 'text', 'text': '没有找到相关视频。'}
        yield {'type': 'done', 'sql': '', 'columns': [], 'rows': [],
               'total': 0, 'chart': {'should_chart': False}, 'agent': 'rag_agent'}
        return
    prompt = ('用户问：' + question + '\n语义搜索结果（分数越高越相关）：\n' +
              json.dumps(results, ensure_ascii=False) +
              '\n用中文推荐最相关的视频，说明推荐原因：')
    answer = ''
    for text in _stream_llm(prompt, history):
        answer += text
        yield {'type': 'text', 'text': text}
    rows = [[r['id'], r['title'], r['author'], r['partition'],
             r['year'], round(r['score']*100, 1)] for r in results]
    cols = ['id', '标题', '作者', '分区', '年份', '相似度(%)']
    yield {'type': 'done', 'sql': '（RAG语义搜索）', 'columns': cols,
           'rows': rows, 'total': len(rows),
           'chart': {'should_chart': False}, 'agent': 'rag_agent'}

def _chat_agent_stream(question, history):
    system = '你是B站数据AI分析助手，用中文回答各种问题。'
    answer = ''
    for text in _stream_llm(question, history, system=system):
        answer += text
        yield {'type': 'text', 'text': text}
    yield {'type': 'done', 'sql': '', 'columns': [], 'rows': [],
           'total': 0, 'chart': {'should_chart': False}, 'agent': 'chat_agent'}

def sql_agent_stream(question, history=[]):
    if not history:
        cached = _cache_get(question)
        if cached:
            yield {'type': 'agent', 'agent': cached.get('agent', 'sql_agent')}
            yield {'type': 'text', 'text': cached['answer']}
            yield {'type': 'done', **{k: cached[k] for k in
                   ['sql', 'columns', 'rows', 'total', 'chart', 'agent']}}
            return
    agent_name = _route(question)
    yield {'type': 'agent', 'agent': agent_name}
    gen_map = {
        'sql_agent':  _sql_agent_stream,
        'rag_agent':  _rag_agent_stream,
        'chat_agent': _chat_agent_stream,
    }
    answer_text = ''
    final_done = None
    for chunk in gen_map[agent_name](question, history):
        if chunk['type'] == 'text': answer_text += chunk['text']
        elif chunk['type'] == 'done': final_done = chunk
        yield chunk
    if final_done and not history:
        _cache_set(question, {
            'answer':  answer_text,
            'sql':     final_done.get('sql', ''),
            'columns': final_done.get('columns', []),
            'rows':    final_done.get('rows', []),
            'total':   final_done.get('total', 0),
            'chart':   final_done.get('chart', {'should_chart': False}),
            'agent':   agent_name,
        })
