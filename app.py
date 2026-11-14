import base64
import shutil
import threading
import sys
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mobile_agent_secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

agent_running = False


class WebLogger:
    """把 print 输出实时推送到前端"""
    def __init__(self, original):
        self.original = original

    def write(self, text):
        self.original.write(text)
        if text.strip():
            socketio.emit('log', {'text': text.rstrip()})

    def flush(self):
        self.original.flush()


def encode_image(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception:
        return ""


def _require_adb():
    """启动任务前确认 adb 可用，否则报错只指向 FileNotFoundError，难以定位"""
    if shutil.which("adb") is None:
        raise RuntimeError(
            "未找到 adb：请安装 Android Platform Tools 并把 adb 加入 PATH，"
            "再用 `adb devices` 确认手机已连接且已授权 USB 调试。"
        )


# ── 单任务模式 ─────────────────────────────────────────
def run_single(task: str):
    global agent_running
    agent_running = True
    sys.stdout = WebLogger(sys.__stdout__)
    try:
        _require_adb()
        import config
        from screen_agent import ScreenAgent

        class WebAgent(ScreenAgent):
            def _think_and_act(self, memory):
                action = super()._think_and_act(memory)
                socketio.emit('update', {
                    'step': len(memory.steps) + 1,
                    'current_img': encode_image('screen_current.png'),
                    'action': action,
                })
                return action

            def _observe(self, last_action, memory):
                obs = super()._observe(last_action, memory)
                socketio.emit('observe', {
                    'observe_img': encode_image('screen_observe.png'),
                    'observation': obs,
                })
                return obs

        import subprocess, time
        subprocess.run(["adb", "shell", "input", "keyevent", "3"])
        time.sleep(1)
        agent = WebAgent(max_steps=config.MAX_STEPS)
        agent.run(task)
        socketio.emit('done', {'message': '任务完成'})
    except Exception as e:
        socketio.emit('error', {'message': str(e)})
        import traceback; traceback.print_exc()
    finally:
        sys.stdout = sys.__stdout__
        agent_running = False


# ── 多应用任务模式 ─────────────────────────────────────
def run_multi(task: str):
    global agent_running
    agent_running = True
    sys.stdout = WebLogger(sys.__stdout__)
    try:
        _require_adb()
        import config
        from multi_agent import MultiAppAgent, SubTaskScreenAgent

        class WebSubAgent(SubTaskScreenAgent):
            def _think_and_act(self, memory):
                action = super()._think_and_act(memory)
                socketio.emit('update', {
                    'step': len(memory.steps) + 1,
                    'current_img': encode_image('screen_current.png'),
                    'action': action,
                })
                return action

            def _observe(self, last_action, memory):
                obs = super()._observe(last_action, memory)
                socketio.emit('observe', {
                    'observe_img': encode_image('screen_observe.png'),
                    'observation': obs,
                })
                return obs

        class WebMultiAgent(MultiAppAgent):
            def __init__(self):
                super().__init__(emit_fn=socketio.emit)
                self.exec_agent = WebSubAgent(max_steps=config.MAX_STEPS)

        import subprocess, time
        subprocess.run(["adb", "shell", "input", "keyevent", "3"])
        time.sleep(1)
        agent = WebMultiAgent()
        agent.run(task)
        socketio.emit('done', {'message': '多应用任务完成'})
    except Exception as e:
        socketio.emit('error', {'message': str(e)})
        import traceback; traceback.print_exc()
    finally:
        sys.stdout = sys.__stdout__
        agent_running = False


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('start_task')
def handle_start(data):
    global agent_running
    if agent_running:
        emit('error', {'message': '任务进行中，请等待完成'})
        return
    task = data.get('task', '').strip()
    if not task:
        emit('error', {'message': '请输入任务内容'})
        return
    mode   = data.get('mode', 'single')
    target = run_multi if mode == 'multi' else run_single
    threading.Thread(target=target, args=(task,), daemon=True).start()
    emit('started', {'task': task, 'mode': mode})


if __name__ == '__main__':
    socketio.run(app, debug=False, port=5000, allow_unsafe_werkzeug=True)