import uiautomator2 as u2
import time

device = u2.connect()
w, h = device.window_size()
print(f"屏幕尺寸：{w} x {h}")

def is_keyboard_shown():
    """检测键盘是否弹出：比较点击前后的窗口高度变化"""
    try:
        # 方式1：通过 inputmethod 服务检测
        import subprocess
        result = subprocess.run(
            ["adb", "shell", "dumpsys", "input_method"],
            capture_output=True, text=True
        )
        return "mInputShown=true" in result.stdout
    except Exception:
        return False

ratios = [0.96, 0.95, 0.947, 0.94, 0.93, 0.92]

for ratio in ratios:
    y = int(h * ratio)
    x = w // 2
    print(f"\n点击 ({x}, {y})，比例 {ratio}...")
    device.click(x, y)
    time.sleep(1.5)

    shown = is_keyboard_shown()
    print(f"键盘弹出：{shown}")

    if shown:
        print(f"\n✅ 找到！输入框坐标：({x}, {y})，比例：{ratio}")
        device.send_keys("测试输入", clear=False)
        time.sleep(1)
        device.press("enter")
        break

    device.press("back")
    time.sleep(0.5)