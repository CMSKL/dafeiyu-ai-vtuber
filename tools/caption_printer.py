"""兼容现有 /send_message 的本机字幕服务；仅绑定回环地址，不播放音频。

除字幕外还承担两类舞台信号（都是只读轮询，前端按 seq 判断是否是新事件）：
- /thinking?on=1|0   角色是否处于「思考中」，超过 THINKING_TTL 秒自动失效，
                      避免核心异常退出后气泡一直挂着
- /effect?name=clap  一次性特效触发，seq 单调递增

工位状态保存在 out/songroom/work-state.json，控制与投票走 POST /work/*。
播放端以 POST /playback 回报实际起播、心跳、结束，独立维护 speaking。
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import json
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.work_state import WorkState

OUT = ROOT / 'out/songroom'
LOCK = threading.Lock()
STATE = {'id': 0, 'text': '', 'at': 0}

# 思考状态：核心开始跑 LLM 时置位，出结果或超时后失效
THINKING_TTL = 25.0
THINKING = {'on': False, 'at': 0.0}

# 特效：每次触发 seq+1，前端记住上次看过的 seq
EFFECT = {'name': '', 'seq': 0, 'at': 0.0}
PLAYBACK = {}
SPEAKING_TTL = 15.0


def playback_event(payload):
    """Prepared captions become visible only on an actual player start callback."""
    playback_id = payload.get('id')
    phase = payload.get('phase')
    if not isinstance(playback_id, str) or not 1 <= len(playback_id) <= 100:
        raise ValueError('invalid playback id')
    if phase not in ('prepare', 'started', 'heartbeat', 'ended'):
        raise ValueError('invalid playback phase')
    now = time.time()
    with LOCK:
        for key, value in list(PLAYBACK.items()):
            if now - value['at'] > 3600:
                del PLAYBACK[key]
        item = PLAYBACK.get(playback_id)
        if phase == 'prepare':
            if item is None:
                text = payload.get('text', '')
                if not isinstance(text, str):
                    raise ValueError('text must be a string')
                if len(PLAYBACK) >= 1000:
                    raise ValueError('too many prepared playbacks')
                PLAYBACK[playback_id] = {'text': text[:1000], 'phase': 'prepared',
                                         'at': now, 'speech': payload.get('speech', True) is True}
        elif item is None:
            raise ValueError('unknown playback id')
        elif phase == 'started' and item['phase'] == 'prepared':
            item.update(phase='started', at=now, started_at=now)
            if item['text']:
                STATE.update(id=STATE['id'] + 1, text=item['text'], at=now)
        elif phase == 'heartbeat' and item['phase'] == 'started':
            item['at'] = now
        elif phase == 'ended':
            item.update(phase='ended', at=now)
    return {'code': 200, 'id': playback_id, 'phase': phase}


def emit(event):
    """运行日志。日志写不进去（编码、文件被占用……）不能让请求处理线程崩掉。

    stdout 被重定向成 GBK 文件时，“♪”、emoji、“ʚ”这类字符会抛 UnicodeEncodeError；
    以前这条异常发生在状态已更新、响应还没发出去之间，客户端看到的是“连接被断开”。
    """
    try:
        print(json.dumps(event, ensure_ascii=False), flush=True)
    except Exception:
        pass


def run_work_clock(work, stop):
    """Checkpoint once per second. A failed tick must not kill the worker."""
    while not stop.wait(1):
        try:
            work.checkpoint()
        except Exception as error:
            emit({'event': 'WORK_CHECKPOINT_FAILED', 'error': repr(error)})
            try:
                work.checkpoint()
            except Exception as retry_error:
                emit({'event': 'WORK_CHECKPOINT_RETRY_FAILED', 'error': repr(retry_error)})


class Handler(BaseHTTPRequestHandler):
    def allowed_origin(self):
        origin = self.headers.get('Origin', '')
        try:
            parsed = urlparse(origin)
        except ValueError:
            self.send_error(403)
            return False
        if origin and (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')):
            self.send_error(403)
            return False
        return True

    def reply(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        origin = self.headers.get('Origin', '')
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        if self.allowed_origin():
            self.reply({'code': 200})

    def do_POST(self):
        if not self.allowed_origin():
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > 16384:
                raise ValueError('JSON body must be 1..16384 bytes')
            if self.headers.get_content_type() != 'application/json':
                raise ValueError('Content-Type must be application/json')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
            path = urlparse(self.path).path
            work = self.server.work
            if path == '/work/control':
                data = {'work': work.control(payload.get('action'))}
            elif path == '/work/event':
                data = {'work': work.start_event(payload.get('event_id'))}
            elif path == '/work/vote':
                data = work.vote(payload.get('event_id'), payload.get('user_id'), payload.get('option'))
            elif path == '/work/message':
                data = {'work': work.set_message(payload.get('nickname'), payload.get('text'))}
            elif path == '/playback':
                data = playback_event(payload)
            else:
                self.reply({'error': 'unknown endpoint'}, 404)
                return
            self.reply(data)
        except (ValueError, TypeError, UnicodeDecodeError) as error:
            self.reply({'error': str(error)}, 400)
        except OSError:
            self.reply({'error': 'state checkpoint failed'}, 503)

    def do_GET(self):
        if not self.allowed_origin():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        now = time.time()

        if parsed.path == '/send_message':
            text = query.get('content', [''])[0][:1000]
            with LOCK:
                STATE.update(id=STATE['id'] + 1, text=text, at=now)
                value = dict(STATE)
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / 'caption-last.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            data = {'code': 200, 'message': '字幕已接收', 'id': value['id']}
            emit({'event': 'CAPTION_RECEIVED', **value})

        elif parsed.path == '/thinking':
            on = query.get('on', ['0'])[0] in ('1', 'true', 'True')
            with LOCK:
                THINKING['on'] = on
                THINKING['at'] = now
            data = {'code': 200, 'thinking': on}
            emit({'event': 'THINKING', 'on': on})

        elif parsed.path == '/effect':
            name = query.get('name', ['clap'])[0][:32] or 'clap'
            with LOCK:
                EFFECT.update(name=name, seq=EFFECT['seq'] + 1, at=now)
                value = dict(EFFECT)
            data = {'code': 200, **value}
            emit({'event': 'EFFECT', **value})

        elif parsed.path == '/state':
            try:
                work = self.server.work.snapshot()
            except OSError:
                self.reply({'error': 'state checkpoint failed'}, 503)
                return
            with LOCK:
                data = dict(STATE)
                thinking_on = THINKING['on'] and (now - THINKING['at'] < THINKING_TTL)
                data['thinking'] = thinking_on
                data['thinking_age'] = round(now - THINKING['at'], 2) if THINKING['at'] else None
                data.update(effect=EFFECT['name'], effect_seq=EFFECT['seq'], effect_at=EFFECT['at'])
                data['speaking'] = any(item['phase'] == 'started' and item['speech']
                                       and now - item['at'] < SPEAKING_TTL for item in PLAYBACK.values())
            data['work'] = work

        elif parsed.path == '/health':
            data = {'code': 200, 'service': 'local-caption-printer'}

        elif parsed.path == '/playback/status':
            with LOCK:
                item = PLAYBACK.get(query.get('id', [''])[0])
                data = {'phase': item['phase'], 'started_at': item.get('started_at')} if item else {'phase': 'unknown'}

        else:
            self.send_error(404)
            return

        self.reply(data)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    # 日志统一用 UTF-8，不可编码的字符用替代符，不再依赖系统默认的 GBK
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 5500), Handler)
    server.work = WorkState(OUT / 'work-state.json')
    # Work and events keep running even when there are no browser viewers.
    stop = threading.Event()
    worker = threading.Thread(target=run_work_clock, args=(server.work, stop),
                              daemon=True, name='work-clock')
    worker.start()
    print('CAPTION_PRINTER_READY http://127.0.0.1:5500', flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        worker.join()
        try:
            server.work.checkpoint()
        except Exception as error:
            emit({'event': 'WORK_SHUTDOWN_CHECKPOINT_FAILED', 'error': repr(error)})
        server.server_close()
