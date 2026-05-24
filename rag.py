"""
RAG 模块 —— 混合检索：Chroma 向量搜索 + BM25 关键词搜索 + RRF 融合
Chroma：首次运行自动构建索引（调用 Embedding API），持久化存储
BM25：每次启动后台线程预热，无需 API，纯内存计算
"""
import os, sqlite3, time, threading
from collections import defaultdict
import chromadb
from chromadb.utils import embedding_functions
import config

# 延迟导入（BM25 依赖较重）
_bm25_module = None
_jieba_module = None

_CHROMA_PATH = os.path.join(os.path.dirname(__file__), 'chroma_db')
_COLLECTION  = 'bili_videos'

# Chroma 相关
_client     = None
_collection = None
_chroma_lock = threading.Lock()

# BM25 相关
_bm25       = None
_bm25_ids   = []    # 与 corpus 对应的文档 ID（str）
_bm25_metas = {}    # str(id) → metadata dict
_bm25_ready = False
_bm25_lock  = threading.Lock()


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _get_ef():
    api_base = config.ANTHROPIC_BASE_URL.rstrip('/')
    if not api_base.endswith('/v1'):
        api_base += '/v1'
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key    = config.ANTHROPIC_API_KEY,
        api_base   = api_base,
        model_name = 'text-embedding-3-small'
    )


def _load_rows():
    """从 SQLite 读取所有视频行"""
    uri  = f'file:{config.DB_PATH}?mode=ro'
    conn = sqlite3.connect(uri, uri=True)
    cur  = conn.cursor()
    cur.execute('SELECT id, 标题, 简介, 作者, 分区, data_year FROM HuiZong')
    rows = cur.fetchall()
    conn.close()
    return rows


# ── Chroma 初始化 ─────────────────────────────────────────────────────────────

def _get_collection():
    global _client, _collection
    with _chroma_lock:
        if _collection is not None:
            return _collection
        _client = chromadb.PersistentClient(path=_CHROMA_PATH)
        ef = _get_ef()
        _collection = _client.get_or_create_collection(
            name               = _COLLECTION,
            embedding_function = ef,
            metadata           = {'hnsw:space': 'cosine'}
        )
        if _collection.count() == 0:
            print('[RAG] Chroma 索引为空，开始构建（首次约3-5分钟）...')
            _build_chroma_index()
        else:
            print(f'[RAG] Chroma 已加载，共 {_collection.count()} 条')
        return _collection


def _build_chroma_index():
    rows  = _load_rows()
    BATCH = 200
    total = len(rows)
    print(f'[RAG] 共 {total} 条，每批 {BATCH} 写入 Chroma...')
    for i in range(0, total, BATCH):
        batch      = rows[i:i+BATCH]
        ids, docs, metas = [], [], []
        for vid_id, title, intro, author, partition, year in batch:
            text = f'{title} {intro or ""} {author} {partition}'.strip()
            ids.append(str(vid_id))
            docs.append(text)
            metas.append({
                'id'       : int(vid_id),
                'title'    : title or '',
                'author'   : author or '',
                'partition': partition or '',
                'year'     : int(year) if year else 0,
            })
        _collection.add(documents=docs, metadatas=metas, ids=ids)
        print(f'[RAG] Chroma {min(i+BATCH, total)}/{total}')
        time.sleep(0.3)
    print('[RAG] Chroma 索引构建完成！')


# ── BM25 初始化（后台线程预热）────────────────────────────────────────────────

def _init_bm25():
    global _bm25, _bm25_ids, _bm25_metas, _bm25_ready, _bm25_module, _jieba_module
    try:
        import jieba
        from rank_bm25 import BM25Okapi
        _bm25_module  = BM25Okapi
        _jieba_module = jieba
        jieba.setLogLevel('WARNING')

        rows   = _load_rows()
        corpus = []
        for vid_id, title, intro, author, partition, year in rows:
            text   = f'{title} {intro or ""} {author} {partition}'.strip()
            tokens = list(jieba.cut(text))
            corpus.append(tokens)
            sid = str(vid_id)
            _bm25_ids.append(sid)
            _bm25_metas[sid] = {
                'id'       : int(vid_id),
                'title'    : title or '',
                'author'   : author or '',
                'partition': partition or '',
                'year'     : int(year) if year else 0,
            }
        with _bm25_lock:
            _bm25 = BM25Okapi(corpus)
            _bm25_ready = True
        print(f'[RAG] BM25 索引完成，共 {len(corpus)} 条')
    except Exception as e:
        print(f'[RAG] BM25 初始化失败（仅降级到纯向量搜索）: {e}')


# 应用启动时后台预热 BM25
threading.Thread(target=_init_bm25, daemon=True, name='bm25-warmup').start()


# ── RRF 融合 ──────────────────────────────────────────────────────────────────

def _rrf(ranked_lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion：合并多路召回结果"""
    scores: dict = defaultdict(float)
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked):
            scores[str(doc_id)] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


# ── 对外检索接口 ───────────────────────────────────────────────────────────────

def search(query: str, top_k: int = 8) -> list:
    """
    混合检索：Chroma（语义）+ BM25（关键词）→ RRF 融合 → top_k 结果
    若 BM25 尚未就绪，仅使用 Chroma 语义搜索（降级）。
    """
    RECALL = 20

    # ── 1. Chroma 向量召回 ─────────────────────────────────────────────────────
    col   = _get_collection()
    count = col.count()
    if count == 0:
        return []

    vec_res   = col.query(
        query_texts = [query],
        n_results   = min(RECALL, count),
        include     = ['metadatas', 'distances']
    )
    vec_ids       = vec_res['ids'][0]           # list[str]
    vec_ranked    = vec_ids
    vec_score_map = {
        vid: round(1 - d, 4)
        for vid, d in zip(vec_ids, vec_res['distances'][0])
    }
    vec_meta_map  = {
        vid: m
        for vid, m in zip(vec_ids, vec_res['metadatas'][0])
    }

    # ── 2. BM25 关键词召回（若已就绪）────────────────────────────────────────
    bm25_ranked = []
    if _bm25_ready and _bm25 is not None and _jieba_module is not None:
        tokens      = list(_jieba_module.cut(query))
        raw_scores  = _bm25.get_scores(tokens)
        top_indices = sorted(range(len(raw_scores)),
                             key=lambda i: -raw_scores[i])[:RECALL]
        bm25_ranked = [_bm25_ids[i] for i in top_indices]

    # ── 3. RRF 融合 ────────────────────────────────────────────────────────────
    all_lists = [vec_ranked]
    if bm25_ranked:
        all_lists.append(bm25_ranked)
    merged = _rrf(all_lists)[:top_k]

    # ── 4. 组装输出 ────────────────────────────────────────────────────────────
    output = []
    for doc_id, rrf_score in merged:
        sid  = str(doc_id)
        meta = dict(vec_meta_map.get(sid) or _bm25_metas.get(sid) or {})
        meta['score']     = round(rrf_score, 4)
        meta['vec_score'] = vec_score_map.get(sid, 0.0)
        meta['hybrid']    = bool(bm25_ranked)   # 标注是否使用了混合检索
        output.append(meta)

    return output
