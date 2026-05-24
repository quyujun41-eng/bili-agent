"""tests/test_rag.py — RAG 检索单元测试"""
import pytest
import os

# 没有数据库时跳过集成测试
CHROMA_DB = os.path.join(os.path.dirname(__file__), "..", "chroma_db")
SKIP_INTEGRATION = not os.path.exists(CHROMA_DB)


@pytest.mark.skipif(SKIP_INTEGRATION, reason="chroma_db not found, skip integration")
def test_search_returns_results():
    import rag
    results = rag.search("搞笑视频", top_k=5)
    assert isinstance(results, list)
    assert len(results) > 0


@pytest.mark.skipif(SKIP_INTEGRATION, reason="chroma_db not found, skip integration")
def test_search_result_fields():
    import rag
    results = rag.search("Python教程", top_k=3)
    for r in results:
        assert "title" in r
        assert "score" in r
        assert isinstance(r["score"], float)


@pytest.mark.skipif(SKIP_INTEGRATION, reason="chroma_db not found, skip integration")
def test_search_top_k_respected():
    import rag
    results = rag.search("游戏", top_k=4)
    assert len(results) <= 4


@pytest.mark.skipif(SKIP_INTEGRATION, reason="chroma_db not found, skip integration")
def test_hybrid_flag_present():
    import rag
    results = rag.search("音乐排行", top_k=5)
    # 至少第一条应该有 hybrid 字段
    if results:
        assert "hybrid" in results[0]


@pytest.mark.skipif(SKIP_INTEGRATION, reason="chroma_db not found, skip integration")
def test_vector_only_search():
    import rag
    results = rag.search_vector_only("美食", top_k=3)
    assert isinstance(results, list)
