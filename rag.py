import os, pickle, sqlite3
import numpy as np
import faiss
import config

_INDEX_PATH = os.path.join(os.path.dirname(__file__), 'faiss_index.bin')
_META_PATH  = os.path.join(os.path.dirname(__file__), 'faiss_meta.pkl')
_MODEL_NAME = 'BAAI/bge-small-zh-v1.5'   # 中文小模型，约90MB，无需PyTorch

_index = None
_meta  = None
_model = None

def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        print('[RAG] 加载向量模型...')
        _model = TextEmbedding(model_name=_MODEL_NAME)
        print('[RAG] 模型加载完成')
    return _model

def build_index(force: bool = False):
    global _index, _meta
    if not force and os.path.exists(_INDEX_PATH) and os.path.exists(_META_PATH):
        _index = faiss.read_index(_INDEX_PATH)
        with open(_META_PATH, 'rb') as f:
            _meta = pickle.load(f)
        print(f'[RAG] 已加载索引，共 {len(_meta)} 条')
        return
    print('[RAG] 开始构建向量索引...')
    conn = sqlite3.connect(config.DB_PATH)
    cur  = conn.cursor()
    cur.execute('SELECT id, 标题, 简介, 作者, 分区, data_year FROM HuiZong')
    rows = cur.fetchall()
    conn.close()
    model     = _get_model()
    texts     = []
    meta_list = []
    for row in rows:
        vid_id, title, intro, author, partition, year = row
        intro_str = intro if intro else ''
        text = f'{title} {intro_str} {author} {partition}'.strip()
        texts.append(text)
        meta_list.append({
            'id': vid_id, 'title': title, 'intro': intro_str,
            'author': author, 'partition': partition, 'year': year,
        })
    print(f'[RAG] 编码 {len(texts)} 条数据...')
    embeddings = np.array(list(model.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings = embeddings / np.maximum(norms, 1e-8)
    dim    = embeddings.shape[1]
    _index = faiss.IndexFlatIP(dim)
    _index.add(embeddings)
    _meta  = meta_list
    faiss.write_index(_index, _INDEX_PATH)
    with open(_META_PATH, 'wb') as f:
        pickle.dump(_meta, f)
    print(f'[RAG] 索引构建完成，共 {len(_meta)} 条，维度 {dim}')

def search(query: str, top_k: int = 8) -> list:
    global _index, _meta
    if _index is None:
        build_index()
    model = _get_model()
    q_vec = np.array(list(model.embed([query])), dtype=np.float32)
    q_vec = q_vec / np.maximum(np.linalg.norm(q_vec), 1e-8)
    scores, indices = _index.search(q_vec, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0:
            continue
        item = dict(_meta[idx])
        item['score'] = round(float(score), 4)
        results.append(item)
    return results
