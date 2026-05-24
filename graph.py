"""
LangGraph 多智能体编排图
Route → [sql_agent | rag_agent | chat_agent] → END
支持：历史感知路由 + SQL 错误自动降级为 chat_agent
"""
from __future__ import annotations
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from llm_client import get_client, get_model

# ── 状态定义 ──────────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    question:    str
    history:     list          # 多轮对话历史（OpenAI message 格式）
    agent_name:  str           # 路由决定的目标 Agent
    retry_count: int           # 降级重试次数
    error:       Optional[str] # 上一步的错误信息（触发降级）

# ── 路由提示词 ─────────────────────────────────────────────────────────────────
_ROUTE_SYSTEM = (
    "你是智能路由器，判断用户问题应由哪个Agent处理。\n"
    "可用Agent：\n"
    "- sql_agent：排行榜、统计数字、播放量/收藏量对比、分区数量等精确查询\n"
    "- rag_agent：推荐视频、找相关内容、语义搜索、'有没有关于XX的视频'\n"
    "- chat_agent：闲聊、解释概念、问候、与数据库无关的对话\n"
    "结合对话历史理解追问意图。只输出Agent名称，不要其他文字。"
)

# ── 节点：路由决策 ─────────────────────────────────────────────────────────────
def router_node(state: AgentState) -> AgentState:
    """
    根据用户问题 + 对话历史，调用 LLM 决定使用哪个 Agent。
    如果处于降级模式（error != None），直接路由到 chat_agent。
    """
    # 有错误且已重试过 → 强制 chat
    if state.get('error') and state.get('retry_count', 0) >= 1:
        return {**state, 'agent_name': 'chat_agent'}

    client = get_client()
    # 构造带历史的消息列表
    messages = (state.get('history') or []) + [
        {'role': 'user', 'content': state['question']}
    ]
    resp = client.chat.completions.create(
        model    = get_model(),
        max_tokens = 20,
        messages = [{'role': 'system', 'content': _ROUTE_SYSTEM}] + messages
    )
    raw = resp.choices[0].message.content.strip().lower()

    if   'rag'  in raw: agent = 'rag_agent'
    elif 'sql'  in raw: agent = 'sql_agent'
    else:               agent = 'chat_agent'

    return {**state, 'agent_name': agent}

# ── 节点：SQL 降级处理 ─────────────────────────────────────────────────────────
def fallback_node(state: AgentState) -> AgentState:
    """SQL 查询失败时，将 agent 改为 chat_agent 并增加重试计数"""
    return {**state, 'agent_name': 'chat_agent', 'retry_count': state.get('retry_count', 0) + 1}

# ── 构建并编译图 ───────────────────────────────────────────────────────────────
def _build() -> object:
    wf = StateGraph(AgentState)

    # 节点注册
    wf.add_node('router',   router_node)
    wf.add_node('fallback', fallback_node)

    # 入口
    wf.set_entry_point('router')

    # router → 三条出路（条件边）
    wf.add_conditional_edges(
        'router',
        lambda s: s['agent_name'],
        {
            'sql_agent':  END,
            'rag_agent':  END,
            'chat_agent': END,
        }
    )

    # fallback → 重新路由
    wf.add_edge('fallback', 'router')

    return wf.compile()

# 全局编译一次，后续直接调用
agent_graph = _build()


# ── 对外接口 ──────────────────────────────────────────────────────────────────
def route(question: str, history: list, error: str = None) -> str:
    """
    路由入口：输入问题 + 历史（+ 可选错误），返回 agent 名称。
    error 非空时触发降级逻辑。
    """
    init_state: AgentState = {
        'question':    question,
        'history':     history or [],
        'agent_name':  '',
        'retry_count': 0,
        'error':       error,
    }
    final = agent_graph.invoke(init_state)
    return final['agent_name']
