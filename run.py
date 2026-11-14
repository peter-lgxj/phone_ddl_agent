import subprocess
import time
from screen_agent import ScreenAgent
import config
# 打开微信，找到包航的对话，阅读他的最后一条聊天记录，并帮我回复他

def clear_background_apps():
    """清空手机后台所有运行中的 App"""
    print("正在清空后台程序...")
    subprocess.run(["adb", "shell", "input", "keyevent", "187"])
    time.sleep(1.5)
    from phone_controller import PhoneController
    phone = PhoneController()
    for text in ["全部清除", "清除全部", "一键清理", "Clear all"]:
        btn = phone.device(text=text)
        if btn.exists:
            btn.click()
            print(f"已点击：{text}")
            time.sleep(1)
            return
    width, height = phone.device.window_size()
    # 从屏幕中部往上滑，坐标按分辨率比例计算，避免写死 1080x2400
    phone.device.swipe(width // 2, int(height * 0.5), width // 2, int(height * 0.1),
                       duration=0.3)
    time.sleep(0.5)
    for text in ["全部清除", "清除全部", "一键清理", "Clear all"]:
        btn = phone.device(text=text)
        if btn.exists:
            btn.click()
            print(f"已点击：{text}")
            time.sleep(1)
            return
    print("未找到清除按钮，跳过")


def run_agent(task: str):
    """清空后台，然后执行任务"""
    # clear_background_apps()
    subprocess.run(["adb", "shell", "input", "keyevent", "3"])
    time.sleep(1)
    agent = ScreenAgent(max_steps=config.MAX_STEPS)  # 从 config 读，不再传 api_key
    agent.run(task)


if __name__ == "__main__":
    provider = config.ACTIVE_PROVIDER
    model = config.PROVIDERS[provider]["model"]
    print(f"手机智能体已就绪  [{provider} / {model}]")
    print("示例：打开微信，找到张三的对话，发送'在吗'")
    print("-" * 40)
    task = input("请输入任务：").strip()
    if task:
        run_agent(task)