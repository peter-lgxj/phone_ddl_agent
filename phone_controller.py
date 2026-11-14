import uiautomator2 as u2
import subprocess
import time
from PIL import Image
import io


class PhoneController:
    """手机控制器：基于 uiautomator2，支持中文输入"""

    def __init__(self,locator):
        # 自动连接 USB 设备
        self.device = u2.connect()
        self.locator=locator
        print(f"已连接设备：{self.device.info.get('productName', '未知')}")

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

    # def type_text(self, text: str):
    #     """
    #     输入文字，完美支持中文。
    #     原理：uiautomator2 内部用 jsonrpc 调用设备端 agent 完成输入。
    #     """
    #     self.device.send_keys(text, clear=False)
    #     print(f"输入文字：{text}")
    #     time.sleep(0.5)
    
    def type_text(self, text: str):
        """输入文字，自动激活输入框，支持中文"""
        
        # 先尝试点击激活输入框
        self.tap_input_box()
        time.sleep(0.5)
        
        # 依次尝试几种写入方式
        # 方式1：标准 EditText
        try:
            box = self.device(className="android.widget.EditText")
            if box.exists:
                box.set_text(text)
                print(f"输入文字(EditText)：{text}")
                time.sleep(0.5)
                return
        except Exception:
            pass

        # 方式2：focused 状态的控件（当前获得焦点的输入框）
        try:
            box = self.device(focused=True)
            if box.exists:
                box.set_text(text)
                print(f"输入文字(focused)：{text}")
                time.sleep(0.5)
                return
        except Exception:
            pass

        # 方式3：send_keys 兜底
        self.device.send_keys(text, clear=False)
        print(f"输入文字(send_keys)：{text}")
        time.sleep(0.5)


    def tap_input_box(self) -> bool:
        """智能查找并点击输入框"""
        # 方式1：标准 EditText
        try:
            box = self.device(className="android.widget.EditText")
            if box.exists:
                box.click()
                print("点击输入框(EditText)")
                time.sleep(0.8)
                return True
        except Exception:
            pass

        # 方式2：微信输入框特殊类名
        try:
            box = self.device(className="android.widget.LinearLayout", 
                            resourceId="com.tencent.mm:id/b4z")
            if box.exists:
                box.click()
                print("点击输入框(微信)")
                time.sleep(0.8)
                return True
        except Exception:
            pass

        print("未找到输入框控件")
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

    def long_tap(self, x, y, duration=1.0):
        """长按，用于触发输入框光标"""
        self.device.long_click(x, y, duration=duration)
        print(f"长按：({x}, {y})")
        time.sleep(0.5)
        

    def is_keyboard_shown(self) -> bool:
        result = subprocess.run(
            ["adb", "shell", "dumpsys", "input_method"],
            capture_output=True,
            encoding="utf-8",
            errors="ignore"
        )
        return "mInputShown=true" in result.stdout


    def tap_input_box(self) -> bool:
        """用 YOLO 检测输入框位置并点击"""
        # 懒加载，第一次调用时初始化
        if not hasattr(self, '_input_detector'):
            from input_detector import InputDetector
            self._input_detector = InputDetector()

        # 截当前屏幕用于检测
        self.screenshot("screen_for_detection.png")

        box = self._input_detector.find_best("screen_for_detection.png")
        if box:
            cx, cy = box["center"]
            print(f"YOLO 检测到输入框，置信度:{box['confidence']}  坐标:({cx},{cy})")
            self.device.click(cx, cy)
            time.sleep(0.8)
            return True

        print("YOLO 未检测到输入框")
        return False

    
    def type_text(self, text):
        
        """输入文字，自动激活输入框，输入完自动发送"""
        # 确保键盘已弹出
        if not self.is_keyboard_shown():
            self.tap_input_box()
            time.sleep(0.5)
            
        # time.sleep(1)
        # self.tap(404, 2342)
        # time.sleep(0.5)
        self.device.send_keys(text, clear=False)
        print(f"输入文字：{text}")
        time.sleep(0.5)
        self._auto_send2(text)
        time.sleep(0.5)
        # 输入完自动发送
        self._auto_send()

    def _auto_send2(self,text):
        elements=self.locator.detect("screen_current.png")
        # print(elements)
        try:
            for el in elements:
                if el["text"] == text:
                    x,y=el['center'][0], el['center'][1]
                elif el["type"] == "input":
                    x,y=el['center'][0], el['center'][1]
            self.tap(x,y)
            time.sleep(1)
            self.tap(674, 2343)
            time.sleep(1)
        except Exception as e:
            print(e)
        
        
    def _auto_send(self):
        """输入完成后自动发送：优先找发送按钮，找不到用 ADB 回车"""
        import subprocess
        time.sleep(0.1)
        try:
            for label in ["Send", "发送"]:
                btn = self.device(text=label)
                if btn.exists:
                    btn.click()
                    print(f"自动发送：点击'{label}'按钮")
                    time.sleep(0.8)
                    return
        except Exception:
            pass

        # 找不到按钮，用 ADB 回车
        subprocess.run(["adb", "shell", "input", "keyevent", "66"])
        print("自动发送：ADB 回车")
        time.sleep(1.5)


    
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
        except Exception:
            pass

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
    
    
