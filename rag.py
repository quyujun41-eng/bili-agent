"""
rag.py —— 混合检索模块（Qdrant + BM25 + RRF + Cohere Rerank）
Embedding: OpenAI text-embedding-3-small API（每批 100 条）
"""
import hashlib, json, logging, threading
from typing import Optional

import numpy as np
import config

logger = logging.getLogger(__name__)

_qdrant_client = None
_openai_client = None
_redis_client  = None
_cohere_client = None
_bm25_index    = None
_bm25_docs     = []
_bm25_ready    = False


def _get_qdrant():
    global _qdrant_client
    if _qdrant_client is None:
        from qdrant_client import QdrantClient
        _qdrant_client = QdrantClient(url=config.QDRANT_URL, timeout=10)
    return _qdrant_client


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=config.OPENAI_API_KEY, base_url=config.OPENAI_BASE_URL)
    return _openai_client


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


def _embed(text: str) -> list:
    key = "emb:" + hashlib.md5(text.encode()).hexdigest()
    r = _get_redis()
    if r:
        try:
            cached = r.get(key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass

    resp = _get_openai().embeddings.create(model=config.EMBED_MODEL, input=[text])
    vec  = resp.data[0].embedding

    if r:
        try:
            r.setex(key, config.EMBED_CACHE_TTL, json.dumps(vec))
        except Exception:
            pass
    return vec


def _embed_batch(texts: list) -> list:
    client   = _get_openai()
    all_vecs = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i + 100]
        resp  = client.embeddings.create(model=config.EMBED_MODEL, input=batch)
        all_vecs.extend([d.embedding for d in resp.data])
        logger.info(f"  Embedded {min(i + 100, len(texts))}/{len(texts)}")
    return all_vecs


def _ensure_collection() -> bool:
    from qdrant_client.models import Distance, VectorParams
    client     = _get_qdrant()
    target_dim = config.EMBED_DIM
    try:
        existing = [c.name for c in client.get_collections().collections]
        if config.QDRANT_COLLECTION in existing:
            info        = client.get_collection(config.QDRANT_COLLECTION)
            current_dim = info.config.params.vectors.size
            if current_dim != target_dim:
                logger.warning(f"向量维度变更 {current_dim}→{target_dim}，重建 collection")
                client.delete_collection(config.QDRANT_COLLECTION)
            elif (info.points_count or 0) > 0:
                return True
        client.create_collection(
            collection_name=config.QDRANT_COLLECTION,
            vectors_config=VectorParams(size=target_dim, distance=Distance.COSINE),
        )
        logger.info(f"创建 Qdrant collection: {config.QDRANT_COLLECTION} dim={target_dim}")
        return False
    except Exception as e:
        logger.error(f"Qdrant collection 初始化失败: {e}")
        raise


def _build_qdrant_index():
    import sqlite3
    from qdrant_client.models import PointStruct

    client = _get_qdrant()
    logger.info("开始构建 Qdrant 索引（OpenAI embedding）...")

    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "SELECT id, 标题, 作者, 分区, data_year, 简介 FROM HuiZong"
    ).fetchall()
    conn.close()

    if not rows:
        logger.warning("数据库为空，跳过索引构建")
        return

    texts, metas = [], []
    for row in rows:
        vid, title, author, partition, year, desc = row
        search_text = (title or "")
        if desc:
            search_text += " " + str(desc)[:200]
        texts.append(search_text.strip())
        metas.append({
            "id":        int(vid),
            "title":     title or "",
            "author":    author or "",
            "partition": partition or "",
            "year":      int(year) if year else 0,
        })

    logger.info(f"向量化 {len(texts)} 条，每批 100 条...")
    vecs = _embed_batch(texts)

    points = []
    for i, (vec, meta) in enumerate(zip(vecs, metas)):
        points.append(PointStruct(id=meta["id"], vector=vec, payload=meta))
        if len(points) >= 500:
            client.upsert(collection_name=config.QDRANT_COLLECTION, points=points)
            logger.info(f"  已上传 {i + 1}/{len(texts)}")
            points = []
    if points:
        client.upsert(collection_name=config.QDRANT_COLLECTION, points=points)

    logger.info(f"Qdrant 索引构建完成，共 {len(texts)} 条")


def _build_bm25_index():
    global _bm25_index, _bm25_docs, _bm25_ready
    try:
        import sqlite3, jieba
        from rank_bm25 import BM25Okapi

        conn = sqlite3.connect(config.DB_PATH)
        rows = conn.execute(
            "SELECT id, 标题, 作者, 分区, data_year FROM HuiZong"
        ).fetchall()
        conn.close()

        if not rows:
            return

        docs, corpus = [], []
        for vid, title, author, partition, year in rows:
            text   = f"{title or ''} {partition or ''}"
            tokens = list(jieba.cut(text))
            corpus.append(tokens)
            docs.append({
                "id":        int(vid),
                "title":     title or "",
                "author":    author or "",
                "partition": partition or "",
                "year":      int(year) if year else 0,
            })

        _bm25_index = BM25Okapi(corpus)
        _bm25_docs  = docs
        _bm25_ready = True
        logger.info(f"BM25 索引就绪，共 {len(docs)} 条")
    except Exception as e:
        logger.warning(f"BM25 索引构建失败: {e}")


def _init_background():
    def _run():
        try:
            already_exists = _ensure_collection()
            if not already_exists:
                _build_qdrant_index()
        except Exception as e:
            logger.error(f"Qdrant 初始化失败: {e}")
        _build_bm25_index()

    threading.Thread(target=_run, daemon=True, name="rag-init").start()


def _qdrant_search(query: str, top_k: int = 20,
                   year_filter: Optional[int] = None,
                   partition_filter: Optional[str] = None) -> list:
    from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

    client = _get_qdrant()
    vec    = _embed(query)
    must   = []
    if year_filter:
        must.append(FieldCondition(key="year", range=Range(gte=year_filter)))
    if partition_filter:
        must.append(FieldCondition(key="partition", match=MatchValue(value=partition_filter)))
    filt = Filter(must=must) if must else None

    try:
        hits = client.search(
            collection_name=config.QDRANT_COLLECTION,
            query_vector=vec,
            limit=top_k,
            query_filter=filt,
            with_payload=True,
        )
        return [{"id": str(h.payload["id"]), "score": h.score, **h.payload} for h in hits]
    except Exception as e:
        logger.warning(f"Qdrant 检索失败: {e}")
        return []


def _bm25_search(query: str, top_k: int = 20) -> list:
    if not _bm25_ready:
        return []
    import jieba
    tokens  = list(jieba.cut(query))
    scores  = _bm25_index.get_scores(tokens)
    top_idx = np.argsort(scores)[::-1][:top_k]
    return [
        {"id": str(_bm25_docs[i]["id"]), "score": float(scores[i]), **_bm25_docs[i]}
        for i in top_idx if scores[i] > 0
    ]


def _rrf(ranked_lists: list, k: int = 60) -> list:
    scores: dict = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _rerank(query: str, docs: list, top_k: int) -> list:
    co = _get_cohere()
    if co is None or not docs:
        return docs[:top_k]
    try:
        texts    = [d.get("title", "") for d in docs]
        resp     = co.rerank(model=config.COHERE_RERANK_MODEL, query=query, documents=texts, top_n=top_k)
        reranked = []
        for r in resp.results:
            d = dict(docs[r.index])
            d["rerank_score"] = r.relevance_score
            reranked.append(d)
        return reranked
    except Exception as e:
        logger.warning(f"Cohere Rerank 失败，降级为 RRF 排序: {e}")
        return docs[:top_k]


def search(query: str, top_k: int = 10,
           year_filter: Optional[int] = None,
           partition_filter: Optional[str] = None) -> list:
    vec_results  = _qdrant_search(query, top_k=top_k * 3,
                                  year_filter=year_filter, partition_filter=partition_filter)
    if not vec_results:
        return []
    bm25_results = _bm25_search(query, top_k=top_k * 3) if _bm25_ready else []
    hybrid       = len(bm25_results) > 0

    vec_ids  = [r["id"] for r in vec_results]
    bm25_ids = [r["id"] for r in bm25_results]
    fused    = _rrf([vec_ids, bm25_ids] if hybrid else [vec_ids])

    id_to_doc: dict = {r["id"]: r for r in vec_results}
    for r in bm25_results:
        if r["id"] not in id_to_doc:
            id_to_doc[r["id"]] = r

    merged = []
    for doc_id, rrf_score in fused[:top_k * 2]:
        if doc_id in id_to_doc:
            doc          = dict(id_to_doc[doc_id])
            doc["score"] = float(rrf_score)
            doc["hybrid"] = hybrid
            merged.append(doc)

    return _rerank(query, merged, top_k)


def get_collection_count() -> int:
    try:
        info = _get_qdrant().get_collection(config.QDRANT_COLLECTION)
        return info.points_count or 0
    except Exception:
        return -1
