"""
get_packages.py — 获取手机所有已安装应用的中文名称和包名

原理：
  Android 系统可以通过 `cmd package` 接口返回 PackageInfo，
  但中文 label 必须在设备上运行代码才能解析资源 ID。
  最可靠的跨设备方案是：
    1. 把一个微型 shell 脚本推到手机上执行
    2. 脚本调用 `pm list packages` + `dumpsys` 读 applicationLabel
    3. 或者用 `aapt2 dump badging` 解析 APK（需要 aapt2 在 PATH）

  本脚本优先用方案①（monkey instrument），
  fallback 到方案②（aapt2），
  再 fallback 到③（包名猜测）。

用法：
    python get_packages.py              # 列出全部第三方应用
    python get_packages.py -s 点评      # 搜索关键词
    python get_packages.py -o apps.txt  # 导出到文件
    python get_packages.py --all        # 包含系统应用
"""

import subprocess
import sys
import re
import argparse
import os
import tempfile


# ── ADB 工具 ──────────────────────────────────────────
def adb(*args) -> str:
    r = subprocess.run(["adb", "shell"] + list(args),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip()

def adb_host(*args) -> str:
    r = subprocess.run(["adb"] + list(args),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip()


# ── 方案 A：在设备上运行 am instrument 读取 label ─────
#   最准确，支持中文，但需要设备有 sh 权限
SHELL_SCRIPT = r"""
for pkg in $(pm list packages -3 | sed 's/package://'); do
  label=$(dumpsys package "$pkg" 2>/dev/null | grep -m1 "    applicationInfo" | grep -oP 'label=\K[^ ]+' | tr -d '"')
  apk=$(pm path "$pkg" 2>/dev/null | sed 's/package://')
  echo "$pkg|||$label|||$apk"
done
"""

def get_labels_via_shell() -> dict[str, str]:
    """在设备 shell 里批量读取 label（资源 ID 形式，非真实中文名）"""
    # 写脚本到手机临时目录
    tmp = "/data/local/tmp/_getpkg.sh"
    proc = subprocess.run(
        ["adb", "push", "-", tmp],
        input=SHELL_SCRIPT.encode(),
        capture_output=True
    )
    adb_host("shell", "chmod", "+x", tmp)
    raw = adb_host("shell", "sh", tmp)
    adb_host("shell", "rm", tmp)

    result = {}
    for line in raw.splitlines():
        parts = line.split("|||")
        if len(parts) >= 2:
            pkg   = parts[0].strip()
            label = parts[1].strip()
            result[pkg] = label
    return result


# ── 方案 B：用 aapt2 解析 APK ─────────────────────────
def get_label_via_aapt(apk_path_on_device: str, pkg: str) -> str:
    """把 APK pull 到本地，用 aapt2 读取 label（慢，但准确）"""
    # 检查本地有没有 aapt2
    r = subprocess.run(["aapt2", "version"], capture_output=True)
    if r.returncode != 0:
        return ""
    # pull APK
    with tempfile.NamedTemporaryFile(suffix=".apk", delete=False) as f:
        tmp_apk = f.name
    subprocess.run(["adb", "pull", apk_path_on_device, tmp_apk],
                   capture_output=True)
    # aapt2 dump badging
    r = subprocess.run(["aapt2", "dump", "badging", tmp_apk],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    os.unlink(tmp_apk)
    m = re.search(r"application-label(?:-zh[-\w]*):'([^']+)'", r.stdout)
    if m:
        return m.group(1)
    m = re.search(r"application-label:'([^']+)'", r.stdout)
    if m:
        return m.group(1)
    return ""


# ── 方案 C：用 cmd activity 接口（Android 8+ 支持）──────
def get_labels_via_cmd() -> dict[str, str]:
    """
    Android 提供了 cmd package 接口，可以查 applicationInfo，
    但返回的也是资源 ID，不是字符串。
    改用：通过 wm / am 的 start-activity-for-result 触发，太复杂。
    改用：dumpsys activity packages 批量解析。
    """
    print("   [方案C] 解析 dumpsys activity packages...")
    raw = adb("dumpsys", "activity", "packages")
    result = {}
    cur_pkg = None
    for line in raw.splitlines():
        # Package [com.xxx] (xxxxx):
        m = re.match(r'\s+Package \[(.+?)\]', line)
        if m:
            cur_pkg = m.group(1)
        if cur_pkg:
            # labelRes= 是资源 ID，nonLocalizedLabel= 是真实字符串（如果有的话）
            m2 = re.search(r'nonLocalizedLabel=(.+)', line)
            if m2:
                val = m2.group(1).strip()
                if val and val != "null":
                    result[cur_pkg] = val
    return result


# ── 方案 D：直接用 pm list + apk 路径 + strings 命令 ───
def get_all_apk_paths(user_only=True) -> dict[str, str]:
    """返回 {包名: apk路径} 映射"""
    flag = "-3" if user_only else ""
    cmd = ["pm", "list", "packages", "-f"]
    if flag:
        cmd.append(flag)
    raw = adb(*cmd)
    result = {}
    for line in raw.splitlines():
        m = re.match(r"package:(.+)=(.+)", line.strip())
        if m:
            result[m.group(2).strip()] = m.group(1).strip()
    return result


# ── 方案 E：strings 解析 APK（无需 aapt2）─────────────
def get_label_via_strings(pkg: str, apk_path: str) -> str:
    """
    在设备上直接用 strings 命令从 apk 中暴力搜索应用名。
    对中文有效，但可能有误判。
    """
    # 从 apk 里提取字符串，找中文名（一般在前几百行）
    raw = adb("strings", apk_path, "2>/dev/null")
    if not raw:
        # strings 命令不存在，尝试用 grep
        raw = adb(f"cat {apk_path} | strings 2>/dev/null | head -500")
    return ""


# ── 主方案：用 dumpsys package 单个查询 ──────────────────
def get_label_single(pkg: str) -> str:
    """
    对单个包查询 nonLocalizedLabel（如果 App 开发者硬编码了名字）
    或者从 Activity 的 android:label 里找。
    """
    raw = adb("dumpsys", "package", pkg)

    # 优先找 nonLocalizedLabel（硬编码字符串，最准）
    for line in raw.splitlines():
        m = re.search(r'nonLocalizedLabel=(.+)', line)
        if m:
            val = m.group(1).strip()
            if val and val not in ("null", "0", "") and not val.startswith("0x"):
                return val

    # 找 applicationInfo 行里的 label（通常是资源 ID，跳过）
    # 找 Activities 块，提取 android:label
    in_act = False
    for line in raw.splitlines():
        if "Activities:" in line:
            in_act = True
        if in_act and "android:label" in line:
            m = re.search(r'android:label="?([^"=\s]+)"?', line)
            if m:
                val = m.group(1).strip()
                if val and not val.startswith("0x") and val != "null":
                    return val

    return ""


# ── 最终方案：通过 adb 运行 monkey 触发，再读 logcat ──
# 太复杂，改用最简单可靠的方式：
# 直接解析 /data/system/packages.xml（需要 root）
# 或者使用 content query launcher db

def get_label_from_launcher_db(pkg: str) -> str:
    """查询 Launcher 数据库里的应用名（不需要 root，但部分设备限制访问）"""
    # 小米/华为等定制 ROM 的 launcher db 路径不同，跳过
    return ""


# ── 备用：用 cmd package resolve-activity 获取主 Activity 名 ──
def get_main_activity(pkg: str) -> str:
    raw = adb("cmd", "package", "resolve-activity", "--brief",
              "-c", "android.intent.category.LAUNCHER", pkg)
    for line in raw.splitlines():
        line = line.strip()
        if line and "/" in line and "No activity found" not in line:
            return line
    return ""


# ══════════════════════════════════════════════════════
#  真正可用的中文名获取方法：
#  在手机上执行一个临时 app 读取 PackageManager.getApplicationLabel()
#  最简便替代：pm list packages -f + aapt dump（本地 aapt）
#  或者：使用 adb exec-out + 自定义 jar
#
#  实际上最简单有效的方式：
#  直接 pull /data/app/包名/base.apk，用 aapt 读
#  如果没有 aapt，用 Python 直接解析 APK 的 resources.arsc
# ══════════════════════════════════════════════════════

def get_label_from_apk_python(pkg: str, apk_path_on_device: str) -> str:
    """
    把 APK pull 到本地，用 Python 直接解析 resources.arsc 获取 label。
    依赖 androguard 或 apk-parser，如果没装则跳过。
    """
    try:
        import zipfile
        import io

        # pull APK 内容到内存
        r = subprocess.run(["adb", "exec-out", f"cat {apk_path_on_device}"],
                           capture_output=True, timeout=30)
        if r.returncode != 0 or not r.stdout:
            return ""

        apk_bytes = r.stdout
        with zipfile.ZipFile(io.BytesIO(apk_bytes)) as z:
            # 读 AndroidManifest.xml（二进制 XML，不能直接解析）
            # 尝试直接从 apk 里用字符串搜索应用名
            # 这不准确，跳过
            pass

    except Exception:
        pass
    return ""


def get_label_best_effort(pkg: str, apk_path: str) -> str:
    """
    综合多种方法获取应用名，按可靠性排序尝试。
    """
    # 1. nonLocalizedLabel（最准，部分app有）
    name = get_label_single(pkg)
    if name:
        return name

    # 2. 尝试从包名猜测（规则：取最后一段，首字母大写）
    # 例：com.tencent.mm → mm（不好），com.dianping.v1 → DianPing（凑合）
    # 这个不准，只在找不到时用
    return ""


# ── 常用应用包名硬编码表（兜底用）────────────────────────
KNOWN_APPS = {
    "com.tencent.mm":                   "微信",
    "com.tencent.mobileqq":             "QQ",
    "com.sina.weibo":                   "微博",
    "com.eg.android.AlipayGphone":      "支付宝",
    "com.ss.android.ugc.aweme":         "抖音",
    "com.smile.gifmaker":               "快手",
    "tv.danmaku.bili":                  "哔哩哔哩",
    "com.autonavi.minimap":             "高德地图",
    "com.baidu.BaiduMap":               "百度地图",
    "com.sankuai.meituan":              "美团",
    "me.ele":                           "饿了么",
    "com.dianping.v1":                  "大众点评",
    "com.sdu.didi.psnger":              "滴滴出行",
    "com.taobao.taobao":                "淘宝",
    "com.jingdong.app.mall":            "京东",
    "com.xunmeng.pinduoduo":            "拼多多",
    "com.netease.cloudmusic":           "网易云音乐",
    "com.tencent.qqmusic":              "QQ音乐",
    "com.qiyi.video":                   "爱奇艺",
    "com.youku.phone":                  "优酷",
    "com.alibaba.android.rimet":        "钉钉",
    "com.ss.android.lark":              "飞书",
    "com.baidu.searchbox":              "百度",
    "cn.wps.moffice_eng":               "WPS Office",
    "com.tencent.wemeet.app":           "腾讯会议",
    "com.netease.nim.qiyukf":           "网易七鱼",
    "com.moji.MojiWeather":             "墨迹天气",
    "com.caocao.taxi":                  "曹操出行",
    "cn.damai":                         "大麦",
    "cmb.pb":                           "招商银行",
    "com.icbc.its.mobile":              "工商银行",
    "com.MobileTicket":                 "铁路12306",
    "com.miui.notes":                   "小米便签",
    "com.miui.gallery":                 "小米相册",
    "com.miui.calculator":              "计算器",
    "com.xiaomi.market":                "小米应用商店",
    "com.ss.android.article.news":      "今日头条",
    "com.tencent.news":                 "腾讯新闻",
    "com.netease.newsreader.activity":  "网易新闻",
    "bubei.tingshu":                    "懒人听书",
    "cc.forestapp":                     "Forest专注森林",
    "cn.gov.pbc.dcep":                  "数字人民币",
    "com.xiaomi.channel":               "澎湃新闻",
    "com.ximalaya.ting.android":        "喜马拉雅",
    "com.zhihu.android":                "知乎",
    "com.douban.frodo":                 "豆瓣",
    "com.tencent.qqlive":               "腾讯视频",
    "com.mango.tvideo":                 "芒果TV",
    "com.bilibili.anime":               "bilibili漫画",
    "com.heytap.market":                "应用商店(OPPO)",
    "com.vivo.appstore":                "应用商店(vivo)",
    "com.huawei.appmarket":             "华为应用市场",
    "com.samsung.android.app.galaxy":   "Galaxy Store",
    "com.google.android.youtube":       "YouTube",
    "com.google.android.gm":            "Gmail",
    "com.microsoft.office.word":        "Word",
    "com.microsoft.office.excel":       "Excel",
    "com.tencent.map":                  "腾讯地图",
    "com.quark.browser":                "夸克浏览器",
    "com.UCMobile":                     "UC浏览器",
    "com.android.chrome":               "Chrome",
    "com.opera.mini.native":            "Opera",
}


def main():
    parser = argparse.ArgumentParser(description="获取手机应用名称和包名")
    parser.add_argument("-s", "--search", default="", help="按关键词过滤")
    parser.add_argument("-o", "--output", default="", help="导出到文件")
    parser.add_argument("--all", action="store_true", help="包含系统应用")
    args = parser.parse_args()

    # 检查连接
    devices = adb_host("devices")
    connected = [l for l in devices.splitlines() if "\tdevice" in l]
    if not connected:
        print("❌  未检测到 ADB 设备，请先连接手机并开启 USB 调试")
        sys.exit(1)
    print(f"✅  已连接：{connected[0].split(chr(9))[0]}\n")

    # 获取包名 + APK 路径
    user_only = not args.all
    print(f"📦  获取{'第三方' if user_only else '全部'}应用列表...")
    apk_map = get_all_apk_paths(user_only)
    print(f"   共 {len(apk_map)} 个应用，正在查询名称...\n")

    # 批量从 dumpsys activity packages 里提取 nonLocalizedLabel
    print("   [1/2] 批量解析 dumpsys activity packages...")
    bulk_labels = get_labels_via_cmd()
    print(f"         成功获取 {len(bulk_labels)} 个应用名\n")

    # 合并结果
    results = []
    found_count = 0
    for i, (pkg, apk_path) in enumerate(sorted(apk_map.items()), 1):
        sys.stdout.write(f"\r   [2/2] 补充查询：{i}/{len(apk_map)}  ")
        sys.stdout.flush()

        # 优先级：已知表 > bulk_labels > 单包查询
        if pkg in KNOWN_APPS:
            name = KNOWN_APPS[pkg]
        elif pkg in bulk_labels and bulk_labels[pkg]:
            name = bulk_labels[pkg]
        else:
            name = get_label_single(pkg)

        if name:
            found_count += 1
        results.append((pkg, name, apk_path))

    print(f"\n   成功识别 {found_count}/{len(results)} 个应用名\n")

    # 过滤
    if args.search:
        kw = args.search.lower()
        results = [(p, n, a) for p, n, a in results
                   if kw in p.lower() or kw in n.lower()]
        print(f"🔍  搜索「{args.search}」匹配 {len(results)} 个\n")

    # 显示
    print(f"{'序号':<5} {'应用名':<18} {'包名':<45} APK路径")
    print("─" * 100)
    for i, (pkg, name, apk) in enumerate(results, 1):
        display = name if name else "─"
        # apk路径截短显示
        short_apk = apk if len(apk) <= 35 else "..." + apk[-32:]
        print(f"{i:<5} {display:<18} {pkg:<45} {short_apk}")

    # 未识别的单独列出
    unknown = [(p, a) for p, n, a in results if not n]
    if unknown:
        print(f"\n❓  未能识别名称的应用（{len(unknown)} 个）：")
        for p, a in unknown:
            print(f"     {p}")

    # 导出
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("应用名\t包名\tAPK路径\n")
            for pkg, name, apk in results:
                f.write(f"{name}\t{pkg}\t{apk}\n")
        print(f"\n💾  已导出：{args.output}")

    # 打印 app_launcher.py 可用的格式
    print("\n" + "═" * 70)
    print("📋  可复制到 app_launcher.py 的格式：")
    print("─" * 70)
    named = [(p, n) for p, n, _ in results if n]
    for pkg, name in named[:30]:  # 最多显示30条
        print(f'    "{name}": "{pkg}",')
    if len(named) > 30:
        print(f"    ... 还有 {len(named)-30} 个，请查看导出文件")


if __name__ == "__main__":
    main()