import json, traceback, uuid
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
import memory as mem
import rag
from agents import sql_agent_stream

app       = FastAPI(title="B站AI数据分析", version="3.0.0")

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        token = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            token = request.query_params.get("token", "")
        if config.ACCESS_TOKEN and token != config.ACCESS_TOKEN:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)

templates = Jinja2Templates(directory="templates")


# ── 启动事件：所有 import 完成后再初始化 Qdrant + BM25 ────────────────────────
@app.on_event("startup")
async def on_startup():
    import rag as _rag
    _rag._init_background()


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"access_token": config.ACCESS_TOKEN})


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

    async def generate():
        try:
            for chunk in sql_agent_stream(question, session_id=session_id, history=body.history or None):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    qdrant_count = rag.get_collection_count()
    redis_ok     = mem.ping()
    qdrant_ok    = qdrant_count >= 0
    return JSONResponse({
        "status":  "ok" if (qdrant_ok and redis_ok) else "degraded",
        "version": "3.0.0",
        "deps": {
            "qdrant": {"ok": qdrant_ok, "vectors": qdrant_count},
            "redis":  {"ok": redis_ok},
        },
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=config.PORT, workers=2)
