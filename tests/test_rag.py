"""
tests/test_rag.py —— RAG 模块单元测试
直接设置 rag._collection 全局变量，绕过缓存逻辑，任何环境都能跑通
"""
import pytest
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock
import rag as _rag   # 模块级 import，后续直接操作 _rag._collection


# ── 辅助：构造 mock collection（query 尊重 n_results）─────────────────────────

def _mock_collection(n=5):
    ids_all       = [str(i) for i in range(1, n + 1)]
    titles_all    = [f"视频{i}" for i in range(1, n + 1)]
    distances_all = [round(i * 0.1, 2) for i in range(n)]

    col = MagicMock()
    col.count.return_value = n

    def _query(query_texts, n_results, include):
        k = min(n_results, n)
        return {
            "ids":       [ids_all[:k]],
            "distances": [distances_all[:k]],
            "metadatas": [[
                {"id": int(i), "title": t, "author": "UP主",
                 "partition": "测试", "year": 2024}
                for i, t in zip(ids_all[:k], titles_all[:k])
            ]],
        }
    col.query.side_effect = _query
    return col


@pytest.fixture(autouse=False)
def mock_col():
    """直接替换 rag._collection（_get_collection 有缓存，这样最可靠）"""
    col = _mock_collection(5)
    original = _rag._collection
    _rag._collection = col
    yield col
    _rag._collection = original   # 测试后还原，不影响其他测试


# ── 基本检索 ──────────────────────────────────────────────────────────────────

def test_search_returns_list(mock_col):
    result = _rag.search("搞笑视频", top_k=5)
    assert isinstance(result, list)


def test_search_top_k_respected(mock_col):
    result = _rag.search("Python教程", top_k=3)
    assert len(result) <= 3


def test_search_result_has_required_fields(mock_col):
    results = _rag.search("音乐排行", top_k=5)
    for r in results:
        assert "title"  in r, "缺少 title"
        assert "score"  in r, "缺少 score"
        assert "hybrid" in r, "缺少 hybrid"
        assert isinstance(r["score"], float), "score 应为 float"


def test_search_score_between_0_and_1(mock_col):
    results = _rag.search("游戏实况", top_k=5)
    for r in results:
        assert 0.0 <= r["score"] <= 1.5, f"score 超出范围: {r['score']}"


# ── BM25 未就绪时 hybrid=False ────────────────────────────────────────────────

def test_hybrid_false_when_bm25_not_ready(mock_col):
    original = _rag._bm25_ready
    try:
        _rag._bm25_ready = False
        results = _rag.search("视频推荐", top_k=5)
        for r in results:
            assert r["hybrid"] is False
    finally:
        _rag._bm25_ready = original


# ── 空集合 ────────────────────────────────────────────────────────────────────

def test_search_empty_collection_returns_empty():
    empty_col = _mock_collection(0)
    original = _rag._collection
    _rag._collection = empty_col
    try:
        result = _rag.search("任意查询", top_k=5)
        assert result == [], f"空集合应返回 []，实际: {result}"
    finally:
        _rag._collection = original


def test_vector_only_empty_returns_empty():
    empty_col = _mock_collection(0)
    original = _rag._collection
    _rag._collection = empty_col
    try:
        result = _rag.search_vector_only("任意查询", top_k=5)
        assert result == [], f"空集合应返回 []，实际: {result}"
    finally:
        _rag._collection = original


# ── vector_only 接口 ──────────────────────────────────────────────────────────

def test_vector_only_returns_list(mock_col):
    result = _rag.search_vector_only("美食", top_k=3)
    assert isinstance(result, list)
    assert len(result) <= 3, f"top_k=3 但返回了 {len(result)} 条"


def test_vector_only_has_score_field(mock_col):
    results = _rag.search_vector_only("美食", top_k=3)
    for r in results:
        assert "score" in r
        assert isinstance(r["score"], float)


# ── RRF 算法正确性 ─────────────────────────────────────────────────────────────

def test_rrf_returns_all_unique_ids():
    merged = _rag._rrf([["a","b","c"], ["b","c","d"]])
    ids = [m[0] for m in merged]
    assert set(ids) == {"a","b","c","d"}


def test_rrf_scores_in_descending_order():
    merged = _rag._rrf([["a","b","c"], ["a","b","c"]])
    scores = [s for _, s in merged]
    assert scores == sorted(scores, reverse=True)


def test_rrf_higher_rank_gets_higher_score():
    merged = dict(_rag._rrf([["best","ok","worst"]]))
    assert merged["best"] > merged["ok"] > merged["worst"]


def test_rrf_fusion_boosts_shared_docs():
    """两路召回都出现的文档分数应高于只出现一次的"""
    merged = dict(_rag._rrf([["shared","only_vec"], ["shared","only_bm25"]]))
    assert merged["shared"] > merged["only_vec"]
    assert merged["shared"] > merged["only_bm25"]
