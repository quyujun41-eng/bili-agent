"""
RAG 模块 —— Chroma 向量数据库 + OpenAI-compatible Embedding API
首次运行自动构建索引，后续直接查询，内存占用极低
"""
import os, sqlite3, time
import chromadb
from chromadb.utils import embedding_functions
import config

_CHROMA_PATH = os.path.join(os.path.dirname(__file__), 'chroma_db')
_COLLECTION  = 'bili_videos'

_client     = None
_collection = None


def _get_ef():
    """OpenAI-compatible Embedding Function"""
    return embedding_functions.OpenAIEmbeddingFunction(
        api_key    = config.ANTHROPIC_API_KEY,
        api_base   = config.ANTHROPIC_BASE_URL.rstrip('/') + '/v1'
                     if not config.ANTHROPIC_BASE_URL.endswith('/v1')
                     else config.ANTHROPIC_BASE_URL,
        model_name = 'text-embedding-3-small'
    )


def _get_collection():
    global _client, _collection
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
        print('[RAG] 向量库为空，开始构建索引...')
        _build_index(_collection)
    else:
        print(f'[RAG] 已加载向量库，共 {_collection.count()} 条')
    return _collection


def _build_index(col):
    """从 SQLite 读取数据，批量写入 Chroma"""
    conn = sqlite3.connect(config.DB_PATH)
    cur  = conn.cursor()
    cur.execute('SELECT id, 标题, 简介, 作者, 分区, data_year FROM HuiZong')
    rows = cur.fetchall()
    conn.close()

    BATCH = 200
    total = len(rows)
    print(f'[RAG] 共 {total} 条，分批写入（每批 {BATCH}）...')

    for i in range(0, total, BATCH):
        batch = rows[i:i+BATCH]
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
                'year'     : int(year) if year else 0
            })
        col.add(documents=docs, metadatas=metas, ids=ids)
        print(f'[RAG] 进度 {min(i+BATCH,total)}/{total}')
        time.sleep(0.3)   # 限速，避免 API 频控

    print('[RAG] 索引构建完成！')


def search(query: str, top_k: int = 8) -> list:
    col = _get_collection()
    results = col.query(
        query_texts = [query],
        n_results   = min(top_k, col.count()),
        include     = ['metadatas', 'distances']
    )
    output = []
    for meta, dist in zip(results['metadatas'][0], results['distances'][0]):
        item = dict(meta)
        item['score'] = round(1 - dist, 4)   # cosine distance → similarity
        output.append(item)
    return output
