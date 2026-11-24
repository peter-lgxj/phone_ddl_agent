import re
import subprocess
import time

# 任务文本 → App 的确定性解析表。只收录「检索目标」会用到的 App，
# 避免把普通任务里的偶然词误判成目标 App。
# (别名, 展示名, 包名)；匹配时长别名优先（如「QQ音乐」先于「QQ」）。
_APP_ALIASES: list[tuple[str, str, str]] = [
    ("qq音乐", "QQ音乐", "com.tencent.qqmusic"),
    ("com.tencent.qqmusic", "QQ音乐", "com.tencent.qqmusic"),
    ("微信", "微信", "com.tencent.mm"),
    ("wechat", "微信", "com.tencent.mm"),
    ("com.tencent.mm", "微信", "com.tencent.mm"),
    ("qq", "QQ", "com.tencent.mobileqq"),
    ("com.tencent.mobileqq", "QQ", "com.tencent.mobileqq"),
    ("outlook", "Outlook", "com.microsoft.office.outlook"),
    ("com.microsoft.office.outlook", "Outlook", "com.microsoft.office.outlook"),
]

_FOCUS_RE = re.compile(r"mCurrentFocus=.*?\s([\w.]+)/")
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=.*?\s([\w.]+)/")
_PACKAGE_RE = re.compile(r"^[A-Za-z][\w]*(\.[\w]+)+$")


def detect_task_app(task: str) -> tuple[str, str] | None:
    """从任务文本解析目标 App，返回 ``(展示名, 包名)``；无法确定返回 None。

    这是「任务说要操作哪个 App」的唯一确定性来源，不依赖视觉模型判断。
    """
    text = (task or "").lower()
    for alias, label, package in sorted(
            _APP_ALIASES, key=lambda item: len(item[0]), reverse=True):
        if alias in text:
            return label, package
    return None


class AppLauncher:
    """
    常用 App 包名管理 + 启动
    遇到没收录的 App，提供自动查询方法
    """

    # 常用 App 包名表
    KNOWN_APPS = {
        # 社交通讯
        "微信":     "com.tencent.mm",
        "QQ":       "com.tencent.mobileqq",
        "微博":     "com.sina.weibo",
        "钉钉":     "com.alibaba.android.rimet",
        "飞书":     "com.ss.android.lark",

        # 邮件
        "Outlook":  "com.microsoft.office.outlook",

        # 外卖 / 购物
        "美团":     "com.sankuai.meituan",
        "饿了么":   "me.ele",
        "淘宝":     "com.taobao.taobao",
        "京东":     "com.jingdong.app.mall",
        "拼多多":   "com.xunmeng.pinduoduo",
        "大众点评": "com.dianping.v1",

        # 出行 / 地图
        "高德地图": "com.autonavi.minimap",
        "百度地图": "com.baidu.BaiduMap",
        "滴滴":     "com.sdu.didi.psnger",

        # 视频 / 娱乐
        "抖音":     "com.ss.android.ugc.aweme",
        "快手":     "com.smile.gifmaker",
        "bilibili": "tv.danmaku.bili",
        "爱奇艺":   "com.qiyi.video",
        "优酷":     "com.youku.phone",

        # 工具 / 系统
        "设置":     "com.android.settings",
        "相机":     "com.android.camera2",
        "浏览器":   "com.android.browser",
        "支付宝":   "com.eg.android.AlipayGphone",
        "百度":     "com.baidu.searchbox",

        # 音乐
        "网易云音乐": "com.netease.cloudmusic",
        "QQ音乐":     "com.tencent.qqmusic",
    }

    def __init__(self, phone_controller):
        self.phone = phone_controller

    def resolve_package(self, app_name: str) -> str | None:
        """把中文名/别名/包名统一解析成包名；无法确定返回 None。"""
        name = (app_name or "").strip()
        if not name:
            return None
        if name in self.KNOWN_APPS:
            return self.KNOWN_APPS[name]
        detected = detect_task_app(name)
        if detected is not None:
            return detected[1]
        if _PACKAGE_RE.match(name):
            return name
        return None

    def launch(self, app_name: str) -> bool:
        """
        启动 App。
        支持中文名（查表）或直接传包名。
        返回 True 表示成功发起启动命令。
        """
        package = self.resolve_package(app_name)
        if package is None:
            print(f"  ❌  无法识别 App「{app_name}」，已拒绝启动；"
                  "请使用中文名或完整包名")
            return False

        from adb_utils import ADB

        print(f"  🚀  启动 App：{app_name}（{package}）")

        result = subprocess.run([
            ADB, "shell", "monkey",
            "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1"
        ], capture_output=True, text=True)

        output = f"{result.stdout}\n{result.stderr}"
        if (result.returncode != 0 or "No activities found" in output
                or "Error" in output):
            print(f"  ❌  启动失败，包名可能不对：{package}")
            print(f"      可以运行 python app_launcher.py 查询正确包名")
            return False

        time.sleep(2.5)  # 等待 App 启动
        return True

    def current_package(self) -> str | None:
        """当前前台 App 的包名；读取失败返回 None。"""
        from adb_utils import ADB

        result = subprocess.run(
            [ADB, "shell", "dumpsys", "window"],
            capture_output=True, text=True,
        )
        text = result.stdout or ""
        match = _FOCUS_RE.search(text) or _FOCUSED_APP_RE.search(text)
        return match.group(1) if match else None

    def ensure_foreground(self, package: str, label: str = "",
                          timeout: float = 8.0) -> bool:
        """确保目标 App 在前台：已在前台则直接返回，否则启动并轮询确认。

        只在确认前台包名等于 ``package`` 时返回 True；否则返回 False，
        调用方必须停止，不能把任务放到上一个 App 里继续执行。
        """
        if self.current_package() == package:
            return True
        if not self.launch(package):
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.current_package() == package:
                return True
            time.sleep(0.5)
        return False

    def get_package(self, app_name: str) -> str | None:
        """返回 App 包名，找不到返回 None"""
        return self.KNOWN_APPS.get(app_name)

    def search_installed_package(self, keyword: str):
        """
        在手机已安装的 App 里搜索包名（关键词匹配）
        用法：python app_launcher.py 微信
        """
        import subprocess
        from adb_utils import ADB
        result = subprocess.run(
            [ADB, "shell", "pm", "list", "packages"],
            capture_output=True, text=True
        )
        lines = result.stdout.strip().split("\n")
        matches = [
            line.replace("package:", "").strip()
            for line in lines
            if keyword.lower() in line.lower()
        ]
        return matches

    def list_all_packages(self):
        """列出手机上所有已安装的包名"""
        import subprocess
        from adb_utils import ADB
        result = subprocess.run(
            [ADB, "shell", "pm", "list", "packages"],
            capture_output=True, text=True
        )
        return [
            line.replace("package:", "").strip()
            for line in result.stdout.strip().split("\n")
        ]


# ===== 命令行工具：查询手机上装了哪些 App =====
if __name__ == "__main__":
    import sys
    from phone_controller import PhoneController

    launcher = AppLauncher(PhoneController())

    if len(sys.argv) > 1:
        keyword = sys.argv[1]
        print(f"搜索包含 '{keyword}' 的包名：\n")
        matches = launcher.search_installed_package(keyword)
        if matches:
            for m in matches:
                print(f"  {m}")
        else:
            print("  未找到匹配的包名")
    else:
        print("用法：python app_launcher.py <关键词>")
        print("示例：python app_launcher.py wechat")
        print("      python app_launcher.py meituan")
        print("\n已收录的常用 App：")
        for name, pkg in AppLauncher.KNOWN_APPS.items():
            print(f"  {name:12s}  {pkg}")