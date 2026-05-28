"""
rag.py —— 混合检索模块（Qdrant + BM25 + RRF + Cohere Rerank）

架构：
  1. 查询向量化（sentence-transformers，带 Redis 缓存）
  2. Qdrant 语义检索（HNSW，带 metadata 过滤）
  3. BM25 关键字检索（rank-bm25 + jieba 分词）
  4. RRF 融合两路结果
  5. Cohere Rerank API 精排（可选）
  6. 返回 top-k 结果
"""
import hashlib, json, time, logging, threading
from typing import Optional

import numpy as np

import config

logger = logging.getLogger(__name__)

# ── 全局单例（懒加载）────────────────────────────────────────────────────────
_qdrant_client  = None
_embed_model    = None
_redis_client   = None
_cohere_client  = None
_bm25_index     = None
_bm25_docs      = []        # [{id, title, author, partition, year}, ...]
_bm25_ready     = False
_lock           = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# 内部：客户端初始化
# ══════════════════════════════════════════════════════════════════════════════

def _get_qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(url=config.QDRANT_URL, timeout=10)
    return _qdrant_client


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer(config.EMBED_MODEL)
    return _embed_model


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            import redis as _redis
            _redis_client = _redis.from_url(
                config.REDIS_URL,
                decode_responses=False,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            _redis_client.ping()
        except Exception as e:
            logger.warning(f"Redis 不可用，Embedding 缓存关闭: {e}")
            _redis_client = None
    return _redis_client


def _get_cohere():
    global _cohere_client
    if _cohere_client is None and config.COHERE_API_KEY:
        import cohere
        _cohere_client = cohere.Client(config.COHERE_API_KEY)
    return _cohere_client


# ══════════════════════════════════════════════════════════════════════════════
# Embedding（带 Redis 缓存）
# ══════════════════════════════════════════════════════════════════════════════

def _embed(text: str) -> list[float]:
    """单条文本向量化，Redis 缓存 24h"""
    key = "emb:" + hashlib.md5(text.encode()).hexdigest()
    r = _get_redis()
    if r is not None:
        try:
            cached = r.get(key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    vec = _get_embed_model().encode(text, normalize_embeddings=True).tolist()

    if r is not None:
        try:
            r.setex(key, config.EMBED_CACHE_TTL, json.dumps(vec))
        except Exception:
            pass
    return vec


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """批量向量化（建索引用），不走 Redis 缓存"""
    model = _get_embed_model()
    vecs = model.encode(texts, normalize_embeddings=True, batch_size=64, show_progress_bar=True)
    return vecs.tolist()


# ══════════════════════════════════════════════════════════════════════════════
# 文本分块
# ══════════════════════════════════════════════════════════════════════════════

def _chunk_text(text: str, chunk_size: int = 300, overlap: int = 50) -> list[str]:
    """按字符数分块，带 overlap"""
    if not text or len(text) <= chunk_size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# Qdrant Collection 初始化 & 索引构建
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_collection() -> bool:
    """确保 Qdrant collection 存在，不存在则创建"""
    from qdrant_client.models import Distance, VectorParams
    client = _get_qdrant()
    try:
        existing = [c.name for c in client.get_collections().collections]
        if config.QDRANT_COLLECTION not in existing:
            dim = len(_embed("测试"))
            client.create_collection(
                collection_name=config.QDRANT_COLLECTION,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
            logger.info(f"已创建 Qdrant collection: {config.QDRANT_COLLECTION} dim={dim}")
            return False   # 需要重建索引
        return True
    except Exception as e:
        logger.error(f"Qdrant collection 初始化失败: {e}")
        raise


def _build_qdrant_index():
    """从 SQLite 读取数据，构建 Qdrant 向量索引"""
    import sqlite3
    from qdrant_client.models import PointStruct

    client = _get_qdrant()
    db_path = config.DB_PATH
    logger.info(f"开始构建 Qdrant 索引，数据库: {db_path}")

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, title, author, partition, year, description FROM HuiZong"
    ).fetchall()
    conn.close()

    if not rows:
        logger.warning("数据库为空，跳过索引构建")
        return

    points = []
    texts  = []
    metas  = []

    for row in rows:
        vid, title, author, partition, year, description = row
        # 用于检索的文本 = 标题 + 描述（截断）
        search_text = title or ""
        if description:
            search_text += " " + description[:200]
        texts.append(search_text.strip())
        metas.append({
            "id": int(vid),
            "title": title or "",
            "author": author or "",
            "partition": partition or "",
            "year": int(year) if year else 0,
        })

    logger.info(f"向量化 {len(texts)} 条数据...")
    vecs = _embed_batch(texts)

    for i, (vec, meta) in enumerate(zip(vecs, metas)):
        points.append(PointStruct(id=meta["id"], vector=vec, payload=meta))
        if len(points) >= 500:
            client.upsert(collection_name=config.QDRANT_COLLECTION, points=points)
            logger.info(f"  已上传 {i+1}/{len(texts)}")
            points = []

    if points:
        client.upsert(collection_name=config.QDRANT_COLLECTION, points=points)

    logger.info(f"Qdrant 索引构建完成，共 {len(texts)} 条")


# ══════════════════════════════════════════════════════════════════════════════
# BM25 索引构建（后台线程）
# ══════════════════════════════════════════════════════════════════════════════

def _build_bm25_index():
    global _bm25_index, _bm25_docs, _bm25_ready
    try:
        import sqlite3, jieba
        from rank_bm25 import BM25Okapi

        conn = sqlite3.connect(config.DB_PATH)
        rows = conn.execute(
            "SELECT id, title, author, partition, year FROM HuiZong"
        ).fetchall()
        conn.close()

        if not rows:
            return

        docs, corpus = [], []
        for vid, title, author, partition, year in rows:
            text = f"{title or ''} {partition or ''}"
            tokens = list(jieba.cut(text))
            corpus.append(tokens)
            docs.append({
                "id": int(vid),
                "title": title or "",
                "author": author or "",
                "partition": partition or "",
                "year": int(year) if year else 0,
            })

        _bm25_index = BM25Okapi(corpus)
        _bm25_docs  = docs
        _bm25_ready = True
        logger.info(f"BM25 索引就绪，共 {len(docs)} 条")
    except Exception as e:
        logger.warning(f"BM25 索引构建失败: {e}")


def _init_background():
    """应用启动时后台初始化：确保 Qdrant collection 存在 + BM25"""
    def _run():
        try:
            already_exists = _ensure_collection()
            if not already_exists:
                _build_qdrant_index()
        except Exception as e:
            logger.error(f"Qdrant 初始化失败: {e}")
        _build_bm25_index()

    t = threading.Thread(target=_run, daemon=True, name="rag-init")
    t.start()


# 模块加载时启动后台初始化
_init_background()


# ══════════════════════════════════════════════════════════════════════════════
# 核心检索函数
# ══════════════════════════════════════════════════════════════════════════════

def _qdrant_search(
    query: str,
    top_k: int = 20,
    year_filter: Optional[int] = None,
    partition_filter: Optional[str] = None,
) -> list[dict]:
    """Qdrant 语义检索，支持 metadata 过滤"""
    from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

    client = _get_qdrant()
    vec = _embed(query)

    # 构造过滤条件
    must = []
    if year_filter:
        must.append(FieldCondition(key="year", range=Range(gte=year_filter)))
    if partition_filter:
        must.append(FieldCondition(
            key="partition",
            match=MatchValue(value=partition_filter)
        ))
    filt = Filter(must=must) if must else None

    try:
        hits = client.search(
            collection_name=config.QDRANT_COLLECTION,
            query_vector=vec,
            limit=top_k,
            query_filter=filt,
            with_payload=True,
        )
        return [
            {"id": str(h.payload["id"]), "score": h.score, **h.payload}
            for h in hits
        ]
    except Exception as e:
        logger.warning(f"Qdrant 检索失败: {e}")
        return []


def _bm25_search(query: str, top_k: int = 20) -> list[dict]:
    """BM25 关键字检索"""
    if not _bm25_ready:
        return []
    import jieba
    tokens = list(jieba.cut(query))
    scores = _bm25_index.get_scores(tokens)
    top_idx = np.argsort(scores)[::-1][:top_k]
    results = []
    for i in top_idx:
        if scores[i] > 0:
            results.append({
                "id": str(_bm25_docs[i]["id"]),
                "score": float(scores[i]),
                **_bm25_docs[i],
            })
    return results


def _rrf(
    ranked_lists: list[list[str]],
    k: int = 60
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion（RRF）融合多路排名"""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _rerank(query: str, docs: list[dict], top_k: int) -> list[dict]:
    """Cohere Rerank API 精排（如果未配置 API Key 则跳过）"""
    co = _get_cohere()
    if co is None or not docs:
        return docs[:top_k]
    try:
        texts = [d.get("title", "") for d in docs]
        resp = co.rerank(
            model=config.COHERE_RERANK_MODEL,
            query=query,
            documents=texts,
            top_n=top_k,
        )
        reranked = []
        for r in resp.results:
            d = dict(docs[r.index])
            d["rerank_score"] = r.relevance_score
            reranked.append(d)
        return reranked
    except Exception as e:
        logger.warning(f"Cohere Rerank 失败，降级为 RRF 排序: {e}")
        return docs[:top_k]


# ══════════════════════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════════════════════

def search(
    query: str,
    top_k: int = 10,
    year_filter: Optional[int] = None,
    partition_filter: Optional[str] = None,
) -> list[dict]:
    """
    混合检索主入口：Qdrant + BM25 → RRF → Cohere Rerank
    返回格式：[{id, title, author, partition, year, score, hybrid, rerank_score?}, ...]
    """
    # Step1: 两路召回
    vec_results = _qdrant_search(
        query, top_k=top_k * 3,
        year_filter=year_filter,
        partition_filter=partition_filter,
    )

    if not vec_results:
        return []

    bm25_results = _bm25_search(query, top_k=top_k * 3) if _bm25_ready else []
    hybrid = len(bm25_results) > 0

    # Step2: RRF 融合
    vec_ids  = [r["id"] for r in vec_results]
    bm25_ids = [r["id"] for r in bm25_results]
    ranked_lists = [vec_ids, bm25_ids] if hybrid else [vec_ids]
    fused = _rrf(ranked_lists)

    # Step3: 按 RRF 分数重组 doc
    id_to_doc: dict[str, dict] = {r["id"]: r for r in vec_results}
    for r in bm25_results:
        if r["id"] not in id_to_doc:
            id_to_doc[r["id"]] = r

    merged = []
    for doc_id, rrf_score in fused[:top_k * 2]:
        if doc_id in id_to_doc:
            doc = dict(id_to_doc[doc_id])
            doc["score"]  = float(rrf_score)
            doc["hybrid"] = hybrid
            merged.append(doc)

    # Step4: Cohere Rerank 精排
    final = _rerank(query, merged, top_k)
    return final


def search_vector_only(
    query: str,
    top_k: int = 10,
    year_filter: Optional[int] = None,
    partition_filter: Optional[str] = None,
) -> list[dict]:
    """纯向量检索（不做 RRF 和 Rerank），调试 / 对比用"""
    results = _qdrant_search(
        query, top_k=top_k,
        year_filter=year_filter,
        partition_filter=partition_filter,
    )
    for r in results:
        r["score"] = float(r.get("score", 0.0))
    return results[:top_k]


def get_collection_count() -> int:
    """返回 collection 中的向量数量（健康检查用）"""
    try:
        info = _get_qdrant().get_collection(config.QDRANT_COLLECTION)
        return info.points_count or 0
    except Exception:
        return -1
