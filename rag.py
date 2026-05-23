"""
RAG 模块 —— BM25 + jieba 中文分词语义检索
无需 API、无需大模型、内存极省，适合中文视频标题搜索
"""
import os, pickle, sqlite3
import jieba
from rank_bm25 import BM25Okapi
import config

_INDEX_PATH = os.path.join(os.path.dirname(__file__), 'bm25_index.pkl')

_bm25  = None
_meta  = None


def _tokenize(text: str) -> list:
    """jieba 分词，过滤单字和空格"""
    return [w for w in jieba.cut(text) if len(w.strip()) > 1]


def build_index(force: bool = False):
    global _bm25, _meta

    if not force and os.path.exists(_INDEX_PATH):
        with open(_INDEX_PATH, 'rb') as f:
            data = pickle.load(f)
        _bm25 = data['bm25']
        _meta = data['meta']
        print(f'[RAG] 已加载BM25索引，共 {len(_meta)} 条')
        return

    print('[RAG] 从数据库读取数据...')
    conn = sqlite3.connect(config.DB_PATH)
    cur  = conn.cursor()
    cur.execute('SELECT id, 标题, 简介, 作者, 分区, data_year FROM HuiZong')
    rows = cur.fetchall()
    conn.close()

    corpus, meta_list = [], []
    for vid_id, title, intro, author, partition, year in rows:
        intro_str = intro if intro else ''
        text = f'{title} {intro_str} {author} {partition}'.strip()
        tokens = _tokenize(text)
        corpus.append(tokens)
        meta_list.append({
            'id': vid_id, 'title': title, 'intro': intro_str,
            'author': author, 'partition': partition, 'year': year,
        })

    print(f'[RAG] 构建BM25索引，共 {len(corpus)} 条...')
    _bm25 = BM25Okapi(corpus)
    _meta = meta_list

    with open(_INDEX_PATH, 'wb') as f:
        pickle.dump({'bm25': _bm25, 'meta': _meta}, f)
    print(f'[RAG] BM25索引构建完成，共 {len(_meta)} 条')


def search(query: str, top_k: int = 8) -> list:
    global _bm25, _meta
    if _bm25 is None:
        build_index()

    tokens = _tokenize(query)
    scores = _bm25.get_scores(tokens)

    # 取 top_k 最高分
    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] <= 0:
            continue
        item = dict(_meta[idx])
        item['score'] = round(float(scores[idx]), 4)
        results.append(item)

    return results
