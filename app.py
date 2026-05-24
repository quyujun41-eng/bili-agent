import json, time, traceback, os, threading
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import config
from agents import sql_agent_stream
import memory as mem
import monitor as mon

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/ask', methods=['POST'])
def ask():
    body       = request.get_json(silent=True) or {}
    question   = body.get('question','').strip()
    session_id = body.get('session_id','')
    if not question:
        return jsonify({'error': 'question required'}), 400

    t_start    = time.time()
    final_agent = ['']
    had_error   = [False]

    def generate():
        try:
            for chunk in sql_agent_stream(question, session_id):
                if chunk.get('type') == 'agent':
                    final_agent[0] = chunk.get('agent','')
                elif chunk.get('type') == 'error':
                    had_error[0] = True
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            traceback.print_exc()
            had_error[0] = True
            yield f"data: {json.dumps({'type':'error','error':str(e)})}\n\n"
        finally:
            ms = int((time.time()-t_start)*1000)
            mon.log_request(session_id, question, final_agent[0], ms, had_error[0])

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@app.route('/api/session/clear', methods=['POST'])
def clear_session():
    body = request.get_json(silent=True) or {}
    sid  = body.get('session_id','')
    if sid:
        mem.clear_session(sid)
    return jsonify({'ok': True})


@app.route('/monitor')
def monitor_page():
    return render_template('monitor.html')


@app.route('/api/monitor/stats')
def monitor_stats():
    return jsonify(mon.get_stats())


@app.route('/eval')
def eval_page():
    return render_template('monitor.html', show_eval=True)


@app.route('/api/eval/results')
def eval_results():
    path = os.path.join(os.path.dirname(__file__), 'data', 'eval_results.json')
    if not os.path.exists(path):
        return jsonify({'status': 'not_run'})
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    data['status'] = 'done'
    return jsonify(data)


@app.route('/api/eval/run', methods=['POST'])
def eval_run():
    path = os.path.join(os.path.dirname(__file__), 'data', 'eval_running.flag')
    if os.path.exists(path):
        return jsonify({'status': 'running'})

    def _bg():
        open(path,'w').write('1')
        try:
            import eval as ev
            ev.run_eval()
        except Exception as e:
            traceback.print_exc()
        finally:
            try: os.remove(path)
            except: pass

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'status': 'started'})


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=config.PORT)
