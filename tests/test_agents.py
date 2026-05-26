"""
tests/test_agents.py —— agents 模块单元测试
mock 全部外部依赖（LLM、SQL、RAG、graph、memory），不发任何真实请求
"""
import json
import pytest
from unittest.mock import patch, MagicMock, call


# ── 辅助构造假数据 ────────────────────────────────────────────────────────────

def _sql_ok(n=3):
    """构造 sql_query_tool 成功返回的 JSON 字符串"""
    rows = [{"id": i, "title": f"视频{i}", "play": i * 1000} for i in range(1, n+1)]
    return json.dumps({
        "sql":     "SELECT * FROM HuiZong LIMIT 3",
        "columns": ["id", "title", "play"],
        "rows":    rows,
        "total":   n,
    })

def _sql_error(msg="DB连接失败"):
    return json.dumps({"error": msg})

def _rag_results(n=3):
    return [
        {"id": i, "title": f"推荐视频{i}", "author": "UP主",
         "partition": "搞笑", "year": 2024, "score": 0.9 - i*0.1}
        for i in range(n)
    ]


# ── SQL Agent ─────────────────────────────────────────────────────────────────

@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="sql_agent")
@patch("agents.sql_query_tool")
def test_sql_agent_chunk_sequence(mock_tool, mock_route, mock_hist, mock_add):
    """sql_agent 应按 agent → memory → text… → done 顺序输出"""
    mock_tool.invoke.return_value = _sql_ok()
    with patch("agents._stream_llm", return_value=iter(["视频A排第一", "，播放量最高"])):
        import agents
        chunks = list(agents.sql_agent_stream("播放量最高的视频", "s1"))

    types = [c["type"] for c in chunks]
    assert types[0] == "agent",  "第一个 chunk 应是 agent"
    assert types[1] == "memory", "第二个 chunk 应是 memory"
    assert "text" in types,      "应有 text chunk"
    assert types[-1] == "done",  "最后一个 chunk 应是 done"


@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="sql_agent")
@patch("agents.sql_query_tool")
def test_sql_agent_done_has_correct_fields(mock_tool, mock_route, mock_hist, mock_add):
    mock_tool.invoke.return_value = _sql_ok(5)
    with patch("agents._stream_llm", return_value=iter(["摘要"])):
        import agents
        chunks = list(agents.sql_agent_stream("数据查询", "s2"))

    done = next(c for c in chunks if c["type"] == "done")
    assert done["agent"]   == "sql_agent"
    assert done["total"]   == 5
    assert "sql"           in done
    assert "columns"       in done
    assert "rows"          in done
    assert "chart"         in done


@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="sql_agent")
@patch("agents.sql_query_tool")
def test_sql_empty_result_returns_no_data_text(mock_tool, mock_route, mock_hist, mock_add):
    """查询结果为空时应输出 text 提示，不调用 LLM"""
    mock_tool.invoke.return_value = json.dumps({
        "sql": "SELECT * FROM HuiZong WHERE 1=0",
        "columns": [], "rows": [], "total": 0
    })
    import agents
    chunks = list(agents.sql_agent_stream("不存在的数据", "s3"))
    text_chunks = [c for c in chunks if c["type"] == "text"]
    assert len(text_chunks) > 0
    combined = "".join(c["text"] for c in text_chunks)
    assert "没有" in combined or "0" in combined


# ── RAG Agent ─────────────────────────────────────────────────────────────────

@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="rag_agent")
@patch("agents.rag.search",      return_value=_rag_results())
def test_rag_agent_done_has_rag_fields(mock_rag, mock_route, mock_hist, mock_add):
    with patch("agents._stream_llm", return_value=iter(["推荐这些视频"])):
        import agents
        chunks = list(agents.sql_agent_stream("推荐搞笑视频", "s4"))

    done = next(c for c in chunks if c["type"] == "done")
    assert done["agent"] == "rag_agent"
    assert done["total"] == len(_rag_results())
    assert done["sql"].startswith("（RAG")


@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="rag_agent")
@patch("agents.rag.search",      return_value=[])
def test_rag_agent_no_results_returns_text(mock_rag, mock_route, mock_hist, mock_add):
    """RAG 检索无结果时，应输出 text 提示"""
    import agents
    chunks = list(agents.sql_agent_stream("查不到的视频主题", "s5"))
    text_chunks = [c for c in chunks if c["type"] == "text"]
    assert len(text_chunks) > 0


# ── Chat Agent ────────────────────────────────────────────────────────────────

@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="chat_agent")
def test_chat_agent_no_sql_no_rows(mock_route, mock_hist, mock_add):
    with patch("agents._stream_llm", return_value=iter(["你好！我是AI助手"])):
        import agents
        chunks = list(agents.sql_agent_stream("你好", "s6"))

    done = next(c for c in chunks if c["type"] == "done")
    assert done["agent"]  == "chat_agent"
    assert done["sql"]    == ""
    assert done["rows"]   == []
    assert done["total"]  == 0


# ── 错误回退（SQL 报错 → fallback agent）──────────────────────────────────────

@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.sql_query_tool")
def test_sql_error_emits_error_chunk(mock_tool, mock_hist, mock_add):
    """SQL 报错时应先 yield error chunk，再走 fallback"""
    mock_tool.invoke.return_value = _sql_error("表不存在")

    # 第一次 route → sql_agent；第二次（fallback）→ chat_agent
    with patch("agents.ag.route", side_effect=["sql_agent", "chat_agent"]):
        with patch("agents._stream_llm", return_value=iter(["抱歉出错了"])):
            import agents
            chunks = list(agents.sql_agent_stream("错误查询", "s7"))

    assert any(c["type"] == "error" for c in chunks), "应包含 error chunk"


@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.sql_query_tool")
def test_sql_error_fallback_still_returns_done(mock_tool, mock_hist, mock_add):
    """fallback 之后流应正常结束（有 done chunk）"""
    mock_tool.invoke.return_value = _sql_error()

    with patch("agents.ag.route", side_effect=["sql_agent", "chat_agent"]):
        with patch("agents._stream_llm", return_value=iter(["降级回答"])):
            import agents
            chunks = list(agents.sql_agent_stream("错误查询2", "s8"))

    done_chunks = [c for c in chunks if c["type"] == "done"]
    assert len(done_chunks) >= 1, "fallback 后应有 done chunk"


# ── 会话记忆写入 ──────────────────────────────────────────────────────────────

@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="chat_agent")
def test_memory_add_turn_called_on_success(mock_route, mock_hist, mock_add):
    """成功回答后，应调用 mem.add_turn 保存历史"""
    with patch("agents._stream_llm", return_value=iter(["回答内容"])):
        import agents
        list(agents.sql_agent_stream("问题", "sess_mem"))

    mock_add.assert_called_once()
    args = mock_add.call_args[0]
    assert args[0] == "sess_mem"   # session_id
    assert args[1] == "问题"        # question
    assert "回答内容" in args[2]    # answer


@patch("agents.mem.add_turn")
@patch("agents.mem.get_history", return_value=[])
@patch("agents.ag.route",        return_value="chat_agent")
def test_no_session_id_skips_memory(mock_route, mock_hist, mock_add):
    """不传 session_id 时，不应调用 mem.add_turn"""
    with patch("agents._stream_llm", return_value=iter(["回答"])):
        import agents
        list(agents.sql_agent_stream("问题"))   # 不传 session_id

    mock_add.assert_not_called()
