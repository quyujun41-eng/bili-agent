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

    result = sql_agent(question)
    if result['status'] == 'error':
        return jsonify({'error': f"查询失败：{result['error']}", 'sql': result.get('sql', '')})

    chart = chart_agent(question, result['columns'], result['rows'])

    return jsonify({
        'answer': result['answer'],
        'sql': result['sql'],
        'columns': result['columns'],
        'rows': result['rows'],
        'total': result['total'],
        'chart': chart,
    })


if __name__ == '__main__':
    app.run(debug=False, port=config.PORT)
