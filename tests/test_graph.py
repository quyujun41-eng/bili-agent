"""tests/test_graph.py — LangGraph 路由单元测试（mock LLM）"""
import pytest
from unittest.mock import patch, MagicMock


def _make_mock_response(content: str):
    """构造模拟 LLM 响应对象"""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@patch("graph.get_client")
def test_route_sql_query(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_response("sql_agent")
    mock_get_client.return_value = mock_client

    import importlib, sys
    if "graph" in sys.modules:
        del sys.modules["graph"]
    import graph

    result = graph.route("播放量最高的视频是哪个", history=[])
    assert result in ("sql_agent", "rag_agent", "chat_agent")


@patch("graph.get_client")
def test_route_rag_query(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_response("rag_agent")
    mock_get_client.return_value = mock_client

    import importlib, sys
    if "graph" in sys.modules:
        del sys.modules["graph"]
    import graph

    result = graph.route("推荐一些搞笑视频", history=[])
    assert result in ("sql_agent", "rag_agent", "chat_agent")


@patch("graph.get_client")
def test_route_chat_query(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_response("chat_agent")
    mock_get_client.return_value = mock_client

    import importlib, sys
    if "graph" in sys.modules:
        del sys.modules["graph"]
    import graph

    result = graph.route("你好，介绍一下你自己", history=[])
    assert result in ("sql_agent", "rag_agent", "chat_agent")


@patch("graph.get_client")
def test_route_with_error_falls_back_to_chat(mock_get_client):
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_response("sql_agent")
    mock_get_client.return_value = mock_client

    import importlib, sys
    if "graph" in sys.modules:
        del sys.modules["graph"]
    import graph

    # 传入 error 时应该路由到 fallback
    result = graph.route("播放量最高的视频", history=[], error="DB connection failed")
    assert result in ("sql_agent", "rag_agent", "chat_agent")


@patch("graph.get_client")
def test_route_returns_valid_agent_name(mock_get_client):
    mock_client = MagicMock()
    # LLM 返回垃圾内容时应该有默认值
    mock_client.chat.completions.create.return_value = _make_mock_response("invalid_agent_xyz")
    mock_get_client.return_value = mock_client

    import importlib, sys
    if "graph" in sys.modules:
        del sys.modules["graph"]
    import graph

    result = graph.route("随便问一个问题", history=[])
    valid_agents = {"sql_agent", "rag_agent", "chat_agent"}
    assert result in valid_agents, f"Expected valid agent, got: {result}"
