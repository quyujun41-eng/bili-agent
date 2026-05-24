import json, time, traceback, os, threading, asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import config
from agents import sql_agent_stream
import memory as mem
import monitor as mon

app = FastAPI(title="B站AI数据分析", version="2.0.0", docs_url="/docs")
templates = Jinja2Templates(directory="templates")
_pool = ThreadPoolExecutor(max_workers=16)


# ── Pydantic 请求/响应模型 ────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    session_id: str = Field(default="", max_length=128)

class ClearRequest(BaseModel):
    session_id: str = Field(default="", max_length=128)

class EvalRunResponse(BaseModel):
    status: str


# ── 页面路由 ──────────────────────────────────────────────────────────────────
@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/monitor")
async def monitor_page(request: Request):
    return templates.TemplateResponse(request=request, name="monitor.html")


# ── API：流式问答 ──────────────────────────────────────────────────────────────
@app.post("/api/ask")
async def ask(body: AskRequest):
    t_start = time.time()
    final_agent = [""]
    had_error = [False]

    async def stream():
        loop = asyncio.get_running_loop()
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
                    q.put_nowait, {"type": "error", "error": str(e)})
            finally:
                ms = int((time.time() - t_start) * 1000)
                mon.log_request(body.session_id, body.question,
                                final_agent[0], ms, had_error[0])
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── API：会话管理 ──────────────────────────────────────────────────────────────
@app.post("/api/session/clear")
async def clear_session(body: ClearRequest):
    if body.session_id:
        mem.clear_session(body.session_id)
    return {"ok": True}


# ── API：监控 ─────────────────────────────────────────────────────────────────
@app.get("/api/monitor/stats")
async def monitor_stats():
    return mon.get_stats()


# ── API：评测 ─────────────────────────────────────────────────────────────────
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


# ── 健康检查（CI/CD 探针） ────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version}
