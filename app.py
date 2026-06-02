import json, traceback, uuid, time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import config
import memory as mem
import rag
import monitor
from agents import sql_agent_stream

app       = FastAPI(title="B站AI数据分析", version="5.0.0")
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        token = ""
        auth  = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            token = request.query_params.get("token", "")
        if config.ACCESS_TOKEN and token != config.ACCESS_TOKEN:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.on_event("startup")
async def on_startup():
    import rag as _rag
    _rag._init_background()


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html",
                                      {"access_token": config.ACCESS_TOKEN})


class AskBody(BaseModel):
    question:   str
    session_id: str  = ""
    history:    list = []


@app.post("/api/ask")
async def ask(body: AskBody):
    question   = body.question.strip()
    session_id = body.session_id or str(uuid.uuid4())
    if not question:
        raise HTTPException(400, "请输入问题")

    start_time  = time.time()
    final_agent = ["chat"]
    success     = [True]

    async def generate():
        try:
            for chunk in sql_agent_stream(question, session_id=session_id,
                                          history=body.history or None):
                if chunk.get("type") == "agent":
                    final_agent[0] = chunk.get("agent", "chat")
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            success[0] = False
            traceback.print_exc()
            monitor.log(question, final_agent[0],
                        (time.time() - start_time) * 1000, False, str(e))
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        else:
            monitor.log(question, final_agent[0],
                        (time.time() - start_time) * 1000, True)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions")
async def list_sessions():
    return JSONResponse(mem.list_sessions(limit=30))


@app.get("/api/session/{session_id}/history")
async def get_session_history(session_id: str):
    return JSONResponse(mem.get_history(session_id))


@app.delete("/api/session/{session_id}")
async def clear_session(session_id: str):
    mem.clear_session(session_id)
    return JSONResponse({"ok": True})


@app.get("/api/stats")
async def get_stats():
    return JSONResponse(monitor.stats())


class FeedbackBody(BaseModel):
    session_id: str = ""
    question:   str = ""
    rating:     str  # "up" | "down"


@app.post("/api/feedback")
async def feedback(body: FeedbackBody):
    if body.rating not in ("up", "down"):
        raise HTTPException(400, "rating must be up or down")
    monitor.log_feedback(body.session_id, body.question, body.rating)
    return JSONResponse({"ok": True})


@app.get("/api/eval")
async def eval_index():
    """对 RAG 索引做抽样质量评测"""
    test_queries = ["搞笑视频", "游戏攻略", "美食烹饪", "音乐MV",
                    "科技数码", "动漫二次元", "运动健身", "旅行vlog"]
    results = []
    for q in test_queries:
        hits = rag.search(q, top_k=3)
        results.append({
            "query":     q,
            "hits":      len(hits),
            "top_score": round(hits[0]["score"], 4) if hits else 0,
            "top_title": hits[0]["title"][:30] if hits else "",
        })
    avg_score = round(sum(r["top_score"] for r in results) / len(results), 4)
    return JSONResponse({
        "vectors":   rag.get_collection_count(),
        "avg_score": avg_score,
        "queries":   results,
    })


@app.get("/health")
async def health():
    qdrant_count = rag.get_collection_count()
    redis_ok     = mem.ping()
    qdrant_ok    = qdrant_count >= 0
    return JSONResponse({
        "status":  "ok" if (qdrant_ok and redis_ok) else "degraded",
        "version": "5.0.0",
        "deps": {
            "qdrant": {"ok": qdrant_ok, "vectors": qdrant_count},
            "redis":  {"ok": redis_ok},
        },
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, workers=1)
