import sqlite3, time, os, threading

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
os.makedirs(_DIR, exist_ok=True)
DB = os.path.join(_DIR, 'monitor.db')
_lock = threading.Lock()

def _init():
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS req_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, question TEXT, agent TEXT,
            response_ms INTEGER, had_error INTEGER,
            ts REAL DEFAULT(strftime('%s','now')))''')
_init()

def log_request(session_id, question, agent, response_ms, had_error=False):
    with _lock:
        with sqlite3.connect(DB) as c:
            c.execute('INSERT INTO req_log(session_id,question,agent,response_ms,had_error) VALUES(?,?,?,?,?)',
                      (session_id, (question or '')[:200], agent or '', int(response_ms), 1 if had_error else 0))

def get_stats():
    with sqlite3.connect(DB) as c:
        total   = c.execute('SELECT COUNT(*) FROM req_log').fetchone()[0]
        today0  = time.mktime(time.strptime(time.strftime('%Y-%m-%d'),'%Y-%m-%d'))
        today   = c.execute('SELECT COUNT(*) FROM req_log WHERE ts>=?',(today0,)).fetchone()[0]
        agents  = c.execute('SELECT agent,COUNT(*) FROM req_log WHERE agent!="" GROUP BY agent ORDER BY 2 DESC').fetchall()
        timing  = c.execute('SELECT agent,AVG(response_ms) FROM req_log WHERE agent!="" GROUP BY agent').fetchall()
        errors  = c.execute('SELECT COUNT(*) FROM req_log WHERE had_error=1').fetchone()[0]
        avg_ms  = c.execute('SELECT AVG(response_ms) FROM req_log WHERE had_error=0').fetchone()[0]
        recent  = c.execute(
            'SELECT question,agent,response_ms,had_error,datetime(ts,"unixepoch","localtime") '
            'FROM req_log ORDER BY ts DESC LIMIT 30').fetchall()
    return {
        'total':      total,
        'today':      today,
        'error_rate': round(errors/total*100,1) if total else 0,
        'avg_ms':     round(avg_ms) if avg_ms else 0,
        'agents':     [{'name':a,'count':n} for a,n in agents],
        'timing':     [{'agent':a,'avg_ms':round(m)} for a,m in timing if m],
        'recent':     [{'q':(q or '')[:60],'agent':ag,'ms':ms,'err':bool(e),'time':t}
                       for q,ag,ms,e,t in recent],
    }
