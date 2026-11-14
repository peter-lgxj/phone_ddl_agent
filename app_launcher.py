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

    def launch(self, app_name: str) -> bool:
        """
        启动 App。
        支持中文名（查表）或直接传包名。
        返回 True 表示成功发起启动命令。
        """
        import subprocess, time

        # 先查表，找不到就把 app_name 当包名直接用
        package = self.KNOWN_APPS.get(app_name, app_name)

        print(f"  🚀  启动 App：{app_name}（{package}）")

        result = subprocess.run([
            "adb", "shell", "monkey",
            "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1"
        ], capture_output=True, text=True)

        if "No activities found" in result.stderr or "Error" in result.stderr:
            print(f"  ❌  启动失败，包名可能不对：{package}")
            print(f"      可以运行 python app_launcher.py 查询正确包名")
            return False

        time.sleep(2.5)  # 等待 App 启动
        return True

    def get_package(self, app_name: str) -> str | None:
        """返回 App 包名，找不到返回 None"""
        return self.KNOWN_APPS.get(app_name)

    def search_installed_package(self, keyword: str):
        """
        在手机已安装的 App 里搜索包名（关键词匹配）
        用法：python app_launcher.py 微信
        """
        import subprocess
        result = subprocess.run(
            ["adb", "shell", "pm", "list", "packages"],
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
        result = subprocess.run(
            ["adb", "shell", "pm", "list", "packages"],
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