import json, traceback
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import config
from agents import sql_agent_stream
import memory as mem

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/ask', methods=['POST'])
def ask():
    body       = request.get_json(silent=True) or {}
    question   = body.get('question', '').strip()
    session_id = body.get('session_id', '')
    if not question:
        return jsonify({'error': '请输入问题'}), 400

    def generate():
        try:
            for chunk in sql_agent_stream(question, session_id):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/api/session/clear', methods=['POST'])
def clear_session():
    """清空指定 session 的对话历史（前端"新对话"按钮调用）"""
    body       = request.get_json(silent=True) or {}
    session_id = body.get('session_id', '')
    if session_id:
        mem.clear_session(session_id)
    return jsonify({'ok': True})


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=config.PORT)
