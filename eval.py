"""
RAG + SQL 评测脚本
用法：python eval.py
结果写入 data/eval_results.json
"""
import json, time, os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RAG_QUERIES = [
    "推荐搞笑有趣的视频",
    "有哪些关于人工智能的视频",
    "学习Python编程的教程",
    "手游攻略视频",
    "美食烹饪教程",
    "旅行Vlog",
    "健身减肥运动",
    "动漫二次元推荐",
    "科技数码评测",
    "音乐MV流行歌曲",
]

SQL_QUERIES = [
    "播放量最高的10个视频是哪些",
    "各分区视频数量分别是多少",
    "平均播放量最高的分区",
    "发布视频最多的UP主前5名",
    "点赞超过50万的视频有多少条",
    "2025年和2026年视频数量对比",
    "粉丝数最多的UP主",
    "弹幕量最高的10个视频",
    "收藏量超过10万的视频数量",
    "平均投币量最高的分区",
]

def run_eval():
    import rag
    from tools import sql_query_tool

    results = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'rag': [], 'sql': [],
        'summary': {}
    }

    # RAG 对比评测
    print("[Eval] 开始 RAG 对比评测...")
    hybrid_scores, vector_scores, hybrid_extras = [], [], []
    for q in RAG_QUERIES:
        t0 = time.time()
        hybrid = rag.search(q, top_k=8)
        hybrid_ms = int((time.time()-t0)*1000)

        t0 = time.time()
        pure_vec = rag.search_vector_only(q, top_k=8)
        vec_ms = int((time.time()-t0)*1000)

        h_ids = {str(r.get('id','')) for r in hybrid}
        v_ids = {str(r.get('id','')) for r in pure_vec}
        extra = len(h_ids - v_ids)

        h_top = hybrid[0]['vec_score'] if hybrid else 0
        v_top = pure_vec[0].get('score',0) if pure_vec else 0

        hybrid_scores.append(h_top)
        vector_scores.append(v_top)
        hybrid_extras.append(extra)

        results['rag'].append({
            'query':       q,
            'hybrid_ms':   hybrid_ms,
            'vector_ms':   vec_ms,
            'hybrid_top1': round(h_top,4),
            'vector_top1': round(v_top,4),
            'bm25_added':  extra,
        })
        print(f"  {q[:20]:20s}  hybrid={h_top:.3f}  vector={v_top:.3f}  bm25_added={extra}")
        time.sleep(0.5)

    # SQL 评测
    print("[Eval] 开始 SQL 评测...")
    sql_ok = 0
    for q in SQL_QUERIES:
        t0 = time.time()
        try:
            raw = sql_query_tool.invoke(q)
            res = json.loads(raw)
            ok  = 'error' not in res and res.get('total',0) > 0
            ms  = int((time.time()-t0)*1000)
            if ok: sql_ok += 1
            results['sql'].append({'query':q,'ok':ok,'ms':ms,'total':res.get('total',0),'sql':res.get('sql','')[:120]})
            print(f"  {'OK' if ok else 'FAIL'}  {q[:30]:30s}  {ms}ms")
        except Exception as e:
            results['sql'].append({'query':q,'ok':False,'ms':0,'error':str(e)})
            print(f"  ERR  {q[:30]:30s}  {e}")
        time.sleep(0.3)

    # 汇总
    avg_h  = round(sum(hybrid_scores)/len(hybrid_scores),4) if hybrid_scores else 0
    avg_v  = round(sum(vector_scores)/len(vector_scores),4) if vector_scores else 0
    lift   = round((avg_h-avg_v)/avg_v*100,1) if avg_v else 0
    avg_extra = round(sum(hybrid_extras)/len(hybrid_extras),1)

    results['summary'] = {
        'rag_queries':       len(RAG_QUERIES),
        'hybrid_avg_top1':   avg_h,
        'vector_avg_top1':   avg_v,
        'score_lift_pct':    lift,
        'bm25_avg_added':    avg_extra,
        'sql_queries':       len(SQL_QUERIES),
        'sql_success_rate':  round(sql_ok/len(SQL_QUERIES)*100,1),
        'sql_success_count': sql_ok,
    }

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'eval_results.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out,'w',encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print("\n[Eval] 完成！")
    print(f"  RAG 混合检索 Top-1 均分: {avg_h}  纯向量: {avg_v}  提升: {lift}%")
    print(f"  BM25 平均补充结果: {avg_extra} 条")
    print(f"  SQL 成功率: {sql_ok}/{len(SQL_QUERIES)} = {results['summary']['sql_success_rate']}%")
    return results

if __name__ == '__main__':
    run_eval()
