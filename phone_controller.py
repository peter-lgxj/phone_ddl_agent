import subprocess
import time
from PIL import Image
import io
import config

# 实测基线：3.7.1 是当前最新版，但仍未修复 Android 17 的 ApplicationSharedMemory 问题
MIN_UIAUTOMATOR2_VERSION = (3, 7, 1)
ANDROID17_ISSUE_URL = "https://github.com/openatx/uiautomator2/issues/1209"


class Uiautomator2CompatibilityError(RuntimeError):
    """uiautomator2 已连接但服务端 RPC 不可用（典型：Android 17）。"""


def _parse_version(value: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for part in str(value or "").split("."):
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers)


def _android_sdk() -> str:
    """读取设备 SDK；失败时返回空串，仅用于生成更具体的报错。"""
    try:
        from adb_utils import ADB
        result = subprocess.run(
            [ADB, "shell", "getprop", "ro.build.version.sdk"],
            capture_output=True, timeout=10,
        )
        return (result.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _compatibility_message(version: str, sdk: str, error: Exception) -> str:
    detail = str(error)
    if "ApplicationSharedMemory" in detail or sdk == "37":
        return (
            f"uiautomator2 {version or '未知版本'} 在 Android 17（SDK {sdk or '37'}）上"
            "首次读取设备信息失败：ApplicationSharedMemory not initialized。"
            "这是 uiautomator2 服务端与 Android 17 的已知兼容问题"
            f"（{ANDROID17_ISSUE_URL}），截至最新版 3.7.1 仍未修复。\n"
            "请改用 ADB 回退驱动：SCREEN_DRIVER=adb_dump python run.py；"
            "或保持 SCREEN_DRIVER=auto（默认）让程序自动回退。"
        )
    upgrade = ""
    if _parse_version(version) < MIN_UIAUTOMATOR2_VERSION:
        upgrade = (
            f"当前版本低于实测基线 {'.'.join(map(str, MIN_UIAUTOMATOR2_VERSION))}，"
            "请先执行 pip install -U 'uiautomator2>=3.7.1'。"
        )
    return (
        f"uiautomator2 已连接设备，但首次 RPC 失败（SDK {sdk or '未知'}）：{detail}。"
        f"{upgrade}"
        "若设备是 Android 17，请使用 SCREEN_DRIVER=adb_dump 回退驱动。"
    )


class PhoneController:
    """手机控制器：基于 uiautomator2，支持中文输入"""

    kind = "uiautomator2"

    def __init__(self,locator):
        # 延迟导入：没有 uiautomator2 时本模块仍可被导入（CI 单测需要）
        import uiautomator2 as u2

        self.locator=locator
        version = str(getattr(u2, "__version__", "") or "")
        # 自动连接 USB 设备
        try:
            self.device = u2.connect()
        except Exception as e:
            if "ApplicationSharedMemory" in str(e):
                raise Uiautomator2CompatibilityError(
                    _compatibility_message(version, _android_sdk(), e)
                ) from e
            raise RuntimeError(
                "无法连接手机：请确认已安装 adb 并加入 PATH、手机已开启开发者选项与 USB 调试、"
                "`adb devices` 能看到已授权设备。"
                f"（原始错误：{e}）"
            ) from e

        # u2.connect() 本身可能成功，真正的兼容性错误发生在第一次 RPC（d.info）
        try:
            info = self.device.info
        except Exception as e:
            raise Uiautomator2CompatibilityError(
                _compatibility_message(version, _android_sdk(), e)
            ) from e
        self.version = version
        print(f"已连接设备：{info.get('productName', '未知')}（uiautomator2 {version or '?'}）")

    def warmup(self) -> None:
        """连接阶段已做 device.info；再查一次窗口尺寸，把首个 RPC 的握手挪到启动期。"""
        try:
            self.device.window_size()
        except Exception:
            pass

    def dump_hierarchy(self) -> str:
        """供 OCR 控件树通道复用；与 ADB 回退驱动保持同一接口。"""
        return self.device.dump_hierarchy()

    def screenshot(self, save_path="screen.png"):
        """截图"""
        img = self.device.screenshot()
        img.save(save_path)
        print(f"截图已保存：{save_path}，尺寸：{img.size}")
        return img

    def tap(self, x, y):
        """点击坐标"""
        self.device.click(x, y)
        print(f"点击：({x}, {y})")
        time.sleep(0.8)

    def swipe(self, x1, y1, x2, y2, duration=0.3):
        """滑动"""
        self.device.swipe(x1, y1, x2, y2, duration=duration)
        print(f"滑动：({x1},{y1}) → ({x2},{y2})")
        time.sleep(0.8)

    def type_text(self, text: str):
        """
        输入文字，自动激活输入框，支持中文。
        只负责输入，不自动发送——发送由智能体的 send 动作单独完成，
        以免与动作序列里的 send 叠加导致重复发送或重复回车。
        """

        # 已聚焦在输入框时直接写入：避免重复点击（tap_input_box 内含 0.8s 等待，YOLO 兜底更慢）
        focused = self.device(focused=True, className="android.widget.EditText")
        if bool(focused.exists):
            try:
                focused.set_text(text)
                print(f"输入文字(已聚焦)：{text}")
                time.sleep(0.3)
                return
            except Exception as e:
                print(f"输入文字(已聚焦)失败：{e}")

        # 未聚焦才点击激活
        self.tap_input_box()
        time.sleep(0.3)
        
        # 依次尝试几种写入方式
        # 方式1：标准 EditText
        try:
            box = self.device(className="android.widget.EditText")
            if box.exists:
                box.set_text(text)
                print(f"输入文字(EditText)：{text}")
                time.sleep(0.5)
                return
        except Exception as e:
            print(f"输入文字(EditText)失败：{e}")

        # 方式2：focused 状态的控件（当前获得焦点的输入框）
        try:
            box = self.device(focused=True)
            if box.exists:
                box.set_text(text)
                print(f"输入文字(focused)：{text}")
                time.sleep(0.5)
                return
        except Exception as e:
            print(f"输入文字(focused)失败：{e}")

        # 方式3：send_keys 兜底
        self.device.send_keys(text, clear=False)
        print(f"输入文字(send_keys)：{text}")
        time.sleep(0.5)


    def tap_input_box(self) -> bool:
        """智能查找并点击输入框：控件（XML）优先，YOLO 作为兜底"""
        # 方式1：标准 EditText
        try:
            box = self.device(className="android.widget.EditText")
            if box.exists:
                box.click()
                print("点击输入框(EditText)")
                time.sleep(0.8)
                return True
        except Exception as e:
            print(f"点击输入框(EditText)失败：{e}")

        # 方式2：微信输入框特殊类名（resource-id 随微信版本变化，可在 config 中置空关闭）
        if config.WECHAT_INPUT_RESOURCE_ID:
            try:
                box = self.device(className="android.widget.LinearLayout",
                                resourceId=config.WECHAT_INPUT_RESOURCE_ID)
                if box.exists:
                    box.click()
                    print("点击输入框(微信)")
                    time.sleep(0.8)
                    return True
            except Exception as e:
                print(f"点击输入框(微信)失败：{e}")

        # 方式3：YOLO 视觉检测兜底
        # 权重（config.INPUT_BOX_MODEL）缺失或加载失败时只提示并返回 False，
        # 不能把 FileNotFoundError 抛到任务循环里导致整个任务中断。
        try:
            if not hasattr(self, "_input_detector"):
                from input_detector import InputDetector
                self._input_detector = InputDetector()
            self.screenshot("screen_for_detection.png")
            detected = self._input_detector.find_best("screen_for_detection.png")
        except Exception as e:
            print(f"YOLO 输入框检测不可用（{e}）")
            return False

        if detected:
            cx, cy = detected["center"]
            print(f"YOLO 检测到输入框，置信度:{detected['confidence']}  坐标:({cx},{cy})")
            self.device.click(cx, cy)
            time.sleep(0.8)
            return True

        print("未找到输入框控件，YOLO 也未检测到输入框")
        return False

    def press_back(self):
        """返回键"""
        self.device.press("back")
        print("按下返回键")
        time.sleep(0.5)

    def press_home(self):
        """Home 键"""
        self.device.press("home")
        print("按下 Home 键")
        time.sleep(0.5)

    def open_app(self, package_name: str):
        """通过包名启动 App"""
        self.device.app_start(package_name)
        print(f"启动 App：{package_name}")
        time.sleep(2.5)


    def press_enter(self):
        """按回车键，用于发送消息、提交搜索"""
        self.device.press("enter")
        print("按下回车键")
        time.sleep(0.5)

    def send(self):
        """发送消息/提交；与 press_enter 等价，便于驱动层统一拦只读动作。"""
        self.press_enter()

    def long_tap(self, x, y, duration=1.0):
        """长按，用于触发输入框光标"""
        self.device.long_click(x, y, duration=duration)
        print(f"长按：({x}, {y})")
        time.sleep(0.5)
        

    def is_keyboard_shown(self) -> bool:
        from adb_utils import ADB
        result = subprocess.run(
            [ADB, "shell", "dumpsys", "input_method"],
            capture_output=True,
            encoding="utf-8",
            errors="ignore"
        )
        return "mInputShown=true" in result.stdout


    def tap_send_button(self):
        """发送消息，优先找 Send 按钮，找不到用回车"""
        try:
            # ADBKeyboard 的 Send 按钮
            for text in ["Send", "发送"]:
                btn = self.device(text=text)
                if btn.exists:
                    btn.click()
                    print(f"点击发送按钮：{text}")
                    time.sleep(0.8)
                    return
        except Exception as e:
            print(f"查找发送按钮失败：{e}")

        # 找不到按钮才用回车
        self.device.press("enter")
        print("按回车发送")
        time.sleep(0.5)
    def test_send(self):
        self.device.press("search")

# ===== 测试入口 =====
if __name__ == "__main__":
    from ocr_locator import OCRLocator
    loc=OCRLocator()
    phone = PhoneController(loc)
    
    
    # phone.tap_input_box()
    # phone.device.set_fastinput_ime(True)
    # phone.device.press("search")
    # # phone.test_send()
    phone.type_text("hi")
    
    # phone.tap(934, 287)
    # 测试1：截图
    # print("=== 测试截图 ===")
    # phone.screenshot("screen.png")

    # # 测试2：回到桌面
    # print("=== 回到桌面 ===")
    # phone.press_home()
    # time.sleep(1)

    # # 再截一张，看看是否变化
    # phone.screenshot("screen_home.png")
    # print("完成！请查看项目文件夹里的 screen.png 和 screen_home.png")
    
    
