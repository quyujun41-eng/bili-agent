from flask import Flask, render_template, request, jsonify
import config
from agents import sql_agent, chart_agent

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/ask', methods=['POST'])
def ask():
    question = (request.get_json(silent=True) or {}).get('question', '').strip()
    if not question:
        return jsonify({'error': '请输入问题'}), 400

    try:
        result = sql_agent(question)
    except Exception as e:
        return jsonify({'error': f"Agent 异常：{e}", 'sql': ''})

    if result['status'] == 'error':
        return jsonify({'error': f"查询失败：{result['error']}", 'sql': result.get('sql', '')})

    try:
        chart = chart_agent(question, result['columns'], result['rows'])
    except Exception:
        chart = {'should_chart': False}

    return jsonify({
        'answer': result['answer'],
        'sql': result['sql'],
        'columns': result['columns'],
        'rows': result['rows'],
        'total': result['total'],
        'chart': chart,
    })


@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({'error': str(e), 'sql': ''}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=config.PORT)
