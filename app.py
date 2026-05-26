import json, time, traceback, os, threading, asyncio, uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import config
from agents import sql_agent_stream
import memory as mem
import monitor as mon
from logger import app_log

app = FastAPI(title="B站AI数据分析", version="2.0.0", docs_url="/docs")
templates = Jinja2Templates(directory="templates")
_pool = ThreadPoolExecutor(max_workers=16)


# ── 简易速率限制（20 req/min per IP，纯内存）────────────────────────────────
_rate_buckets: dict = defaultdict(list)
_RATE_LIMIT  = 20
_RATE_WINDOW = 60.0

def _check_rate(ip: str) -> None:
    """超限抛 429，否则记录本次请求时间戳"""
    now    = time.time()
    bucket = [t for t in _rate_buckets[ip] if now - t < _RATE_WINDOW]
    _rate_buckets[ip] = bucket
    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，限速 {_RATE_LIMIT} 次/{int(_RATE_WINDOW)}s"
        )
    _rate_buckets[ip].append(now)


# ── Request-ID 中间件 ─────────────────────────────────────────────────────────
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.rid = rid
    t0       = time.time()
    response = await call_next(request)
    ms       = int((time.time() - t0) * 1000)
    response.headers["X-Request-ID"] = rid
    app_log.info(
        f"{request.method} {request.url.path} "
        f"→ {response.status_code} ({ms}ms) rid={rid}"
    )
    return response


# ── 全局异常兜底 ──────────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    rid = getattr(request.state, "rid", "-")
    app_log.bind(rid=rid).error(
        f"Unhandled exception: {exc}\n{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={"error": "内部服务器错误，请稍后重试", "request_id": rid},
    )


# ── Pydantic 请求/响应模型 ─────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question:   str = Field(..., min_length=1, max_length=500)
    session_id: str = Field(default="", max_length=128)

class ClearRequest(BaseModel):
    session_id: str = Field(default="", max_length=128)

class EvalRunResponse(BaseModel):
    status: str


# ── 页面路由 ───────────────────────────────────────────────────────────────────
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/monitor")
async def monitor_page(request: Request):
    return templates.TemplateResponse(request=request, name="monitor.html")


# ── API：流式问答 ───────────────────────────────────────────────────────────────
@app.post("/api/ask")
async def ask(request: Request, body: AskRequest):
    _check_rate(request.client.host)
    rid         = getattr(request.state, "rid", "-")
    log         = app_log.bind(rid=rid)
    t_start     = time.time()
    final_agent = [""]
    had_error   = [False]

    log.info(f"ask q={body.question[:60]!r} session={body.session_id or '-'}")

    async def stream():
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def _producer():
            try:
                for chunk in sql_agent_stream(body.question, body.session_id):
                    if chunk.get("type") == "agent":
                        final_agent[0] = chunk.get("agent", "")
                    elif chunk.get("type") == "error":
                        had_error[0] = True
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            except Exception as e:
                traceback.print_exc()
                had_error[0] = True
                loop.call_soon_threadsafe(
                    q.put_nowait, {"type": "error", "error": str(e)}
                )
            finally:
                ms = int((time.time() - t_start) * 1000)
                mon.log_request(
                    body.session_id, body.question,
                    final_agent[0], ms, had_error[0]
                )
                log.info(
                    f"ask done agent={final_agent[0]} ms={ms} err={had_error[0]}"
                )
                loop.call_soon_threadsafe(q.put_nowait, None)

        loop.run_in_executor(_pool, _producer)

        while True:
            item = await q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-ID":     rid,
        },
    )


# ── API：会话管理 ───────────────────────────────────────────────────────────────
@app.post("/api/session/clear")
async def clear_session(body: ClearRequest):
    if body.session_id:
        mem.clear_session(body.session_id)
    return {"ok": True}


# ── API：监控 ──────────────────────────────────────────────────────────────────
@app.get("/api/monitor/stats")
async def monitor_stats():
    return mon.get_stats()


# ── API：评测 ──────────────────────────────────────────────────────────────────
@app.get("/api/eval/results")
async def eval_results():
    path = os.path.join(os.path.dirname(__file__), "data", "eval_results.json")
    if not os.path.exists(path):
        return {"status": "not_run"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["status"] = "done"
    return data

@app.post("/api/eval/run", response_model=EvalRunResponse)
async def eval_run():
    flag = os.path.join(os.path.dirname(__file__), "data", "eval_running.flag")
    if os.path.exists(flag):
        return {"status": "running"}

    def _bg():
        open(flag, "w").write("1")
        try:
            import eval as ev
            ev.run_eval()
        except Exception:
            traceback.print_exc()
        finally:
            try:
                os.remove(flag)
            except OSError:
                pass

    threading.Thread(target=_bg, daemon=True, name="eval-worker").start()
    return {"status": "started"}


# ── 健康检查 ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}


# ── 启动事件 ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    app_log.info(f"B站AI分析服务已启动 version={app.version} | ASGI/uvicorn")
