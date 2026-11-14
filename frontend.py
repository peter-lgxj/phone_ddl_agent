import streamlit as st
from PIL import Image, ImageDraw, ImageFont
import ui_state
import time
import os
import subprocess
import threading

st.set_page_config(
    page_title="手机智能体",
    layout="wide",
    page_icon="📱"
)

st.markdown("""
<style>
.step-box {
    background: #1e1e2e;
    border-radius: 8px;
    padding: 12px;
    margin: 6px 0;
    border-left: 3px solid #7c3aed;
    font-family: monospace;
    font-size: 13px;
}
.thought { color: #a78bfa; }
.action  { color: #34d399; }
.observe { color: #60a5fa; }
.status-running  { color: #fbbf24; font-weight: bold; }
.status-finished { color: #34d399; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------ #
#  工具函数
# ------------------------------------------------------------------ #
def draw_action_on_image(img_path: str, action: dict) -> Image.Image:
    """在截图上标注即将执行的动作"""
    img = Image.open(img_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    action_type = action.get("action_type", "")
    description = action.get("description", "")

    if action_type == "tap":
        x = action.get("x", 0)
        y = action.get("y", 0)
        if isinstance(x, list): x = x[0]
        if isinstance(y, list): y = y[0]
        x, y = int(float(x)), int(float(y))
        r = 30
        # 红色圆圈 + 十字
        draw.ellipse([x-r, y-r, x+r, y+r], outline="red", width=4)
        draw.line([x-r, y, x+r, y], fill="red", width=3)
        draw.line([x, y-r, x, y+r], fill="red", width=3)
        # 标注文字
        _draw_label(draw, x, y - r - 30, description, w)

    elif action_type == "swipe":
        x1 = int(float(action.get("x", 0)))
        y1 = int(float(action.get("y", 0)))
        x2 = int(float(action.get("x2", x1)))
        y2 = int(float(action.get("y2", y1)))
        draw.line([x1, y1, x2, y2], fill="orange", width=5)
        draw.ellipse([x1-10, y1-10, x1+10, y1+10], fill="orange")
        draw.ellipse([x2-10, y2-10, x2+10, y2+10], fill="red")
        _draw_label(draw, (x1+x2)//2, min(y1,y2)-30, description, w)

    elif action_type in ("type", "tap_input_box"):
        # 蓝色横条在底部标注输入区域
        draw.rectangle([0, h-160, w, h-80], outline="blue", width=4)
        _draw_label(draw, w//2, h-180, description, w)

    elif action_type in ("back", "home"):
        icon = "← 返回" if action_type == "back" else "⌂ 主页"
        draw.rectangle([20, h-80, 200, h-20], fill="#333", outline="white", width=2)
        _draw_label(draw, 110, h-65, icon, w)

    elif action_type == "open_app":
        _draw_label(draw, w//2, h//2, f"🚀 {description}", w)

    else:
        _draw_label(draw, w//2, 60, description, w)

    return img

def _draw_label(draw, x, y, text, img_w, max_chars=30):
    """在图片上画带背景的文字标签"""
    if not text:
        return
    text = text[:max_chars] + "..." if len(text) > max_chars else text
    # 背景框
    text_w = len(text) * 9
    x1 = max(0, x - text_w // 2)
    x2 = min(img_w, x + text_w // 2)
    draw.rectangle([x1-4, y-4, x2+4, y+20], fill=(0, 0, 0, 180))
    draw.text((x1, y), text, fill="white")

def action_type_icon(t: str) -> str:
    icons = {
        "tap": "👆", "swipe": "👉", "type": "⌨️",
        "tap_input_box": "📝", "send": "📤",
        "open_app": "🚀", "back": "←", "home": "🏠",
        "wait": "⏳", "finish": "✅", "press_enter": "↵",
    }
    return icons.get(t, "▶")

# ------------------------------------------------------------------ #
#  运行任务的线程函数
# ------------------------------------------------------------------ #
def run_task_thread(task: str):
    import sys
    sys.path.insert(0, ".")
    from screen_agent import ScreenAgent
    agent = ScreenAgent()
    agent.run(task)

# ------------------------------------------------------------------ #
#  页面布局
# ------------------------------------------------------------------ #
st.title("📱 手机智能体控制台")

# 任务输入区
with st.container():
    col_input, col_btn = st.columns([4, 1])
    with col_input:
        task_input = st.text_input(
            "任务指令",
            placeholder="例如：打开微信，找到张三的对话，发送'在吗'",
            label_visibility="collapsed"
        )
    with col_btn:
        start_btn = st.button("▶ 开始执行", type="primary", use_container_width=True)

if start_btn and task_input:
    ui_state.init_state(task_input)
    t = threading.Thread(target=run_task_thread, args=(task_input,), daemon=True)
    t.start()
    st.success(f"任务已启动：{task_input}")

st.divider()

# 主体三栏
col_left, col_mid, col_right = st.columns([2, 2, 3])

state = ui_state.load()

if not state:
    st.info("请在上方输入任务指令并点击开始执行")
    st.stop()

# ------------------------------------------------------------------ #
#  左栏：current 截图 + 动作标注
# ------------------------------------------------------------------ #
with col_left:
    st.subheader("📸 当前屏幕")
    action = state.get("last_action", {})
    if os.path.exists("screen_current.png"):
        try:
            img = draw_action_on_image("screen_current.png", action)
            st.image(img, use_container_width=True)
        except Exception:
            st.image("screen_current.png", use_container_width=True)

    # 动作说明卡片
    if action:
        t = action.get("action_type", "")
        desc = action.get("description", "")
        st.markdown(f"""
        <div class="step-box">
        <span style="font-size:20px">{action_type_icon(t)}</span>
        <span class="action"> 即将执行：{desc}</span><br>
        <span style="color:#888">动作类型：{t}</span>
        </div>
        """, unsafe_allow_html=True)

    # 快捷按钮（仅展示，不实际触发）
    st.caption("系统操作示意")
    bcol1, bcol2, bcol3 = st.columns(3)
    bcol1.button("← 返回", disabled=True, use_container_width=True)
    bcol2.button("⌂ 主页", disabled=True, use_container_width=True)
    bcol3.button("⏸ 等待", disabled=True, use_container_width=True)

# ------------------------------------------------------------------ #
#  中栏：observe 截图
# ------------------------------------------------------------------ #
with col_mid:
    st.subheader("👁 执行后观察")
    if os.path.exists("screen_observe.png"):
        st.image("screen_observe.png", use_container_width=True)
    else:
        st.info("执行后截图将显示在这里")

    # 最新 observation 文字
    steps = state.get("steps", [])
    if steps:
        last = steps[-1]
        obs = last.get("observation", "")
        if obs:
            st.markdown(f"""
            <div class="step-box">
            <span class="observe">👁 {obs}</span>
            </div>
            """, unsafe_allow_html=True)

# ------------------------------------------------------------------ #
#  右栏：执行日志
# ------------------------------------------------------------------ #
with col_right:
    status = state.get("status", "")
    step_now = state.get("current_step", 0)
    task_name = state.get("task", "")

    status_html = (
        f'<span class="status-running">⚙️ 执行中（第 {step_now} 步）</span>'
        if status == "running" else
        f'<span class="status-finished">✅ 已完成（共 {step_now} 步）</span>'
        if status == "finished" else
        f'<span style="color:#f87171">⚠️ {status}</span>'
    )

    st.subheader("📋 执行日志")
    st.markdown(f"**任务：** {task_name}", unsafe_allow_html=False)
    st.markdown(status_html, unsafe_allow_html=True)
    st.divider()

    # 逐步展示
    for s in reversed(steps):  # 最新的在最上面
        icon = action_type_icon(s.get("action_type", ""))
        st.markdown(f"""
        <div class="step-box">
        <span style="color:#888">第 {s['step']} 步</span>
        <span style="float:right;color:#888">{icon} {s.get('action_type','')}</span><br>
        <span class="thought">💭 {s.get('thought','')[:120]}...</span><br>
        <span class="action">▶ {s.get('description','')}</span><br>
        <span class="observe">👁 {s.get('observation','')[:80]}</span>
        </div>
        """, unsafe_allow_html=True)

# ------------------------------------------------------------------ #
#  自动刷新
# ------------------------------------------------------------------ #
if state.get("status") == "running":
    time.sleep(1.5)
    st.rerun()