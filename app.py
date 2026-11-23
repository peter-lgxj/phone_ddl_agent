import base64
import os
import shutil
import threading
import sys
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

from device_lock import device_lock
from adb_utils import ADB
from ddl_agent.web import blueprint as ddl_blueprint

app = Flask(__name__)
app.register_blueprint(ddl_blueprint)
# 默认值仅用于本地调试；对外暴露时请通过环境变量覆盖
app.config['SECRET_KEY'] = os.getenv("FLASK_SECRET_KEY", "mobile_agent_secret")
socketio = SocketIO(
    app,
    cors_allowed_origins=os.getenv("CORS_ALLOWED_ORIGINS", "*"),
    async_mode='threading',
)

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


class _WebAgentHooks:
    """网页实时推送钩子；参数按 ScreenAgent 基类签名透传，避免包装层签名漂移。"""

    def _think_and_act(self, memory, merged: bool = False, **kwargs):
        action = super()._think_and_act(memory, merged=merged, **kwargs)
        socketio.emit('update', {
            'step': len(memory.steps) + 1,
            'current_img': encode_image('screen_current.png'),
            'action': action,
        })
        return action

    def _observe(self, last_action, memory, **kwargs):
        obs = super()._observe(last_action, memory, **kwargs)
        socketio.emit('observe', {
            'observe_img': encode_image('screen_observe.png'),
            'observation': obs,
        })
        return obs


# ── 单任务模式 ─────────────────────────────────────────
def run_single(task: str):
    global agent_running
    agent_running = True
    sys.stdout = WebLogger(sys.__stdout__)
    try:
        _require_adb()
        import config
        from screen_agent import ScreenAgent

        class WebAgent(_WebAgentHooks, ScreenAgent):
            pass

        import subprocess, time
        with device_lock(timeout=30) as locked:
            if not locked:
                raise RuntimeError("设备正被其它任务占用（另一个 Web/CLI/实验任务在运行），请稍后重试")
            # 回桌面与执行器构造都在锁内，避免与另一个任务的重置互相穿插
            subprocess.run([ADB, "shell", "input", "keyevent", "3"])
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

        class WebSubAgent(_WebAgentHooks, SubTaskScreenAgent):
            pass

        class WebMultiAgent(MultiAppAgent):
            def __init__(self):
                super().__init__(emit_fn=socketio.emit,
                                 exec_agent=WebSubAgent(max_steps=config.MAX_STEPS))

        import subprocess, time
        with device_lock(timeout=30) as locked:
            if not locked:
                raise RuntimeError("设备正被其它任务占用（另一个 Web/CLI/实验任务在运行），请稍后重试")
            # 回桌面与执行器构造都在锁内，避免与另一个任务的重置互相穿插
            subprocess.run([ADB, "shell", "input", "keyevent", "3"])
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
