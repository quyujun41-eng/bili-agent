"""tests/test_memory.py — 会话记忆单元测试"""
import time
import pytest
import importlib, sys

# 确保每次测试用干净的 memory 模块状态
def _fresh_memory():
    if "memory" in sys.modules:
        del sys.modules["memory"]
    import memory
    return memory


def test_add_and_get_history():
    mem = _fresh_memory()
    mem.add_turn("s1", "问题1", "回答1")
    mem.add_turn("s1", "问题2", "回答2")
    h = mem.get_history("s1")
    assert len(h) == 4                    # 2 turns × 2 messages
    assert h[0]["role"] == "user"
    assert h[0]["content"] == "问题1"
    assert h[1]["role"] == "assistant"
    assert h[1]["content"] == "回答1"


def test_max_turns_enforced():
    mem = _fresh_memory()
    for i in range(10):
        mem.add_turn("s2", f"q{i}", f"a{i}")
    h = mem.get_history("s2")
    # MAX_TURNS=6 → 最多 12 条消息
    assert len(h) <= 12


def test_clear_session():
    mem = _fresh_memory()
    mem.add_turn("s3", "q", "a")
    mem.clear_session("s3")
    assert mem.get_history("s3") == []


def test_session_info():
    mem = _fresh_memory()
    mem.add_turn("s4", "q", "a")
    info = mem.session_info("s4")
    assert "turns" in info
    assert info["turns"] >= 1


def test_empty_session_returns_empty_list():
    mem = _fresh_memory()
    assert mem.get_history("no_such_session") == []
