import os
import subprocess
import xml.etree.ElementTree as ET
import re
import warnings

import config as _config

warnings.filterwarnings("ignore")


def map_point(cx, cy, inv_scale, width, height):
    """缩放图坐标 → 原图坐标（四舍五入并裁到图像范围内）"""
    x = int(round(cx * inv_scale))
    y = int(round(cy * inv_scale))
    return (min(max(x, 0), width - 1), min(max(y, 0), height - 1))


def map_bbox(x1, y1, x2, y2, inv_scale, width, height):
    """缩放图检测框 → 原图检测框（四舍五入并裁到图像范围内）"""
    nx1 = min(max(int(round(x1 * inv_scale)), 0), width - 1)
    ny1 = min(max(int(round(y1 * inv_scale)), 0), height - 1)
    nx2 = min(max(int(round(x2 * inv_scale)), 0), width - 1)
    ny2 = min(max(int(round(y2 * inv_scale)), 0), height - 1)
    return (min(nx1, nx2), min(ny1, ny2), max(nx1, nx2), max(ny1, ny2))


os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"


class OCRLocator:
    """
    屏幕元素定位器：OCR + XML 双通道融合
    - OCR：识别所有文字和坐标，通用，任何 App 都能用
    - XML：获取精确控件坐标，仅支持未屏蔽控件树的 App
    - 两者结果合并去重后输出，互相补充
    """

    def __init__(self, engine: str = None, device="auto"):
        # 引擎默认 rapidocr（ONNX，CPU 上通常 1-2 秒）；easyocr 保留作对照与兜底
        self._engine_name = (engine or getattr(_config, "OCR_ENGINE", "rapidocr")).lower()
        self.reader = None          # EasyOCR，按需加载
        self._rapid_reader = None   # RapidOCR，按需加载
        self.last_meta = {}         # 最近一次 OCR 的引擎/缩放比例/图尺寸

        # 初始化 u2 设备连接，默认连接第一个 USB 设备或模拟器
        # device="auto" 时自动连接；传 None/False 可跳过（单测用）
        self.d = None
        if device == "auto":
            import uiautomator2 as u2
            try:
                self.d = u2.connect()
            except Exception as e:
                print(f"Warning: Could not connect to device via uiautomator2: {e}")
        else:
            self.d = device

    # ------------------------------------------------------------------ #
    #  OCR 通道
    # ------------------------------------------------------------------ #
    def _ocr_detect(self, image_path: str) -> list[dict]:
        """按 OCR_ENGINE 选择引擎；按 OCR_SCALE 缩放识别，再把框映射回原图坐标"""
        scale = float(getattr(_config, "OCR_SCALE", 1.0) or 1.0)
        scale = min(max(scale, 0.1), 1.0)

        if self._engine_name == "easyocr":
            elements, meta = self._easyocr_detect(image_path, scale)
        else:
            elements, meta = self._rapidocr_detect(image_path, scale)

        meta.update({"engine": self._engine_name, "elements": len(elements)})
        self.last_meta = meta
        return elements

    def _load_scaled(self, image_path: str, scale: float):
        """读图并按 scale 缩放；返回 (图像, 原尺寸, 缩放后尺寸)"""
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return None, None, None
        h, w = img.shape[:2]
        if scale != 1.0:
            nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        else:
            nw, nh = w, h
        return img, (w, h), (nw, nh)

    def _easyocr_detect(self, image_path: str, scale: float = 1.0):
        """EasyOCR 识别文字元素（旧引擎，保留作对照）"""
        import cv2
        if self.reader is None:
            import easyocr
            self.reader = easyocr.Reader(['ch_sim', 'en'], verbose=False)

        img, orig_size, scaled_size = self._load_scaled(image_path, scale)
        if img is None:
            return [], {"scale": scale, "original_size": None, "scaled_size": None}
        inv = 1.0 / scale
        w, h = orig_size
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        results = self.reader.readtext(img_rgb)
        elements = []
        for (box, text, confidence) in results:
            if confidence < 0.4 or not text.strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = sum(xs) / 4
            cy = sum(ys) / 4
            elements.append({
                "text": text.strip(),
                "center": map_point(cx, cy, inv, w, h),
                "raw_center": (int(round(cx)), int(round(cy))) if scale != 1.0 else None,
                "bbox": map_bbox(min(xs), min(ys), max(xs), max(ys), inv, w, h),
                "confidence": round(confidence, 2),
                "source": "ocr",
                "type": "text",
            })
        return elements, {"scale": scale, "original_size": orig_size,
                          "scaled_size": scaled_size}

    def _rapidocr_detect(self, image_path: str, scale: float = 1.0):
        """RapidOCR（ONNX Runtime）识别文字元素"""
        if self._rapid_reader is None:
            from rapidocr_onnxruntime import RapidOCR
            self._rapid_reader = RapidOCR()

        # 手机截图都是正向的，跳过方向分类（实测省约 1.7s，识别结果一致）
        img, orig_size, scaled_size = self._load_scaled(image_path, scale)
        if img is None:
            return [], {"scale": scale, "original_size": None, "scaled_size": None}
        result, _ = self._rapid_reader(img, use_cls=False)
        elements = []
        inv = 1.0 / scale
        w, h = orig_size
        for item in result or []:
            box, text, confidence = item[0], item[1], item[2]
            text = (text or "").strip()
            if confidence is None or confidence < 0.4 or not text:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = sum(xs) / 4
            cy = sum(ys) / 4
            elements.append({
                "text": text,
                "center": map_point(cx, cy, inv, w, h),
                "raw_center": (int(round(cx)), int(round(cy))) if scale != 1.0 else None,
                "bbox": map_bbox(min(xs), min(ys), max(xs), max(ys), inv, w, h),
                "confidence": round(float(confidence), 2),
                "source": "ocr",
                "type": "text",
            })
        return elements, {"scale": scale, "original_size": orig_size,
                          "scaled_size": scaled_size}

    def detect_tree(self) -> list[dict]:
        """只取控件树元素（不做 OCR），供「控件树优先」路径使用"""
        return self._xml_detect()

    def detect_ocr(self, image_path: str) -> list[dict]:
        """只跑 OCR（不取控件树），供「OCR 兜底」路径使用"""
        return self._ocr_detect(image_path)

    # ------------------------------------------------------------------ #
    #  XML 通道 (已修改为使用 uiautomator2)
    # ------------------------------------------------------------------ #
    def _xml_detect(self) -> list[dict]:
        """
        使用 uiautomator2 获取控件树，提取可交互元素。
        相比 adb shell dump，这种方式更稳定且无需文件中转。
        """
        if not self.d:
            return []
        
        try:
            # 直接获取当前的 UI 层级结构 (XML 字符串)
            # 注意：dump_hierarchy 不接受 timeout 参数，u2 内部有默认超时
            xml_content = self.d.dump_hierarchy()
            
            if not xml_content:
                return []

            # 解析 XML 字符串
            root = ET.fromstring(xml_content)
            elements = []
            self._parse_xml_node(root, elements)
            return elements

        except Exception as e:
            # 打印错误以便调试，但返回空列表以降级到 OCR
            print(f"XML Detect Error: {e}")
            return []
        # return self.d.dump_hierarchy()
    def _parse_xml_node(self, node, elements: list):
        """递归解析 XML 节点，提取有用的可交互元素"""
        # 注意：uiautomator2 生成的 XML 属性名可能略有不同，但通常兼容
        # 例如：content-desc 可能在某些版本中是 contentDescription，但 ET 解析通常保持原样
        text = node.get("text", "").strip()
        content_desc = node.get("content-desc", "").strip()
        
        # uiautomator2 dump 出来的 clickable 可能是 "true"/"false" 字符串
        clickable = node.get("clickable", "false") == "true"
        focusable = node.get("focusable", "false") == "true"
        
        node_class = node.get("class", "").split(".")[-1]
        resource_id = node.get("resource-id", "")
        bounds = node.get("bounds", "")

        label = text or content_desc
        is_input = "EditText" in node_class

        # 保留：有文字的可点击元素 + 所有输入框
        if bounds and (is_input or (label and (clickable or focusable))):
            center = self._bounds_to_center(bounds)
            if center:
                elem_type = "input" if is_input else "control"
                elements.append({
                    "text": label or f"[{node_class}]",
                    "center": center,
                    "bbox": self._bounds_to_box(bounds),
                    "confidence": 1.0,   # XML 坐标精确，置信度设为 1
                    "source": "xml",
                    "type": elem_type,
                    "class": node_class,
                    "resource_id": resource_id,
                    "bounds": bounds,
                })

        for child in node:
            self._parse_xml_node(child, elements)

    def _bounds_to_center(self, bounds_str: str):
        nums = re.findall(r"\d+", bounds_str)
        if len(nums) == 4:
            x1, y1, x2, y2 = map(int, nums)
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    def _bounds_to_box(self, bounds_str: str):
        nums = re.findall(r"\d+", bounds_str)
        if len(nums) == 4:
            x1, y1, x2, y2 = map(int, nums)
            return (x1, y1, x2, y2)
        return None

    # ------------------------------------------------------------------ #
    #  合并去重
    # ------------------------------------------------------------------ #
    def _merge(self, ocr_elements: list, xml_elements: list) -> list[dict]:
        """
        合并 OCR 和 XML 结果，去重。
        去重策略：如果 XML 元素和 OCR 元素坐标很近（距离 < 40px），
        保留 XML 的（坐标更精确），丢弃 OCR 的。
        """
        merged = list(xml_elements)  # XML 结果直接保留
        xml_centers = [e["center"] for e in xml_elements]

        for ocr_el in ocr_elements:
            cx, cy = ocr_el["center"]
            # 检查是否和已有 XML 元素重叠
            too_close = any(
                abs(cx - xc) < 40 and abs(cy - yc) < 40
                for xc, yc in xml_centers
            )
            if not too_close:
                merged.append(ocr_el)

        return merged

    # ------------------------------------------------------------------ #
    #  对外接口
    # ------------------------------------------------------------------ #
    def detect(self, image_path: str) -> list[dict]:
        """
        双通道检测，返回合并后的元素列表。
        XML 失败时自动降级为纯 OCR。
        """
        ocr_elements = self._ocr_detect(image_path)
        xml_elements = self._xml_detect()

        if xml_elements:
            merged = self._merge(ocr_elements, xml_elements)
            print(f"  OCR:{len(ocr_elements)} + XML:{len(xml_elements)} → 合并:{len(merged)}")
            return merged
        else:
            return ocr_elements

    def find(self, image_path: str, keyword: str) -> dict | None:
        """查找包含 keyword 的元素"""
        for el in self.detect(image_path):
            if keyword.strip().lower() in el["text"].lower():
                return el
        return None

    def format_for_prompt(self, image_path: str) -> str:
        """格式化给 AI 看，XML 来源的标注更精确"""
        elements = self.detect(image_path)
        # print(elements)
        if not elements:
            return "未识别到元素"

        lines = []
        for el in elements:
            src = "【精确】" if el["source"] == "xml" else ""
            typ = "📝输入框 " if el["type"] == "input" else ""
            lines.append(
                f"{src}{typ}「{el['text']}」坐标:({el['center'][0]}, {el['center'][1]})"
            )
        return "\n".join(lines)


# ── 按需 OCR：点击坐标吸附 ─────────────────────────────────────
# 只在模型给出 tap（需要精确落点）时跑 OCR/控件树，把落点吸到最近的元素中心；
# 其余动作（open_app/back/home/wait/type/send/swipe/finish）不需要逐字定位，直接走视觉结果。

OCR_TRIGGER_ACTIONS = ("tap",)


def should_trigger_ocr(mode: str, action_type: str) -> bool:
    """on_demand 模式下只对需要精确坐标的动作触发 OCR。"""
    return mode == "on_demand" and action_type in OCR_TRIGGER_ACTIONS


def nearest_element(point, elements, max_distance):
    """返回 (element, distance) 或 None；点落在 bbox 内时距离记 0。"""
    px, py = point
    best = None
    for el in elements:
        center = el.get("center")
        if not center:
            continue
        cx, cy = center
        bbox = el.get("bbox")
        inside = bool(bbox) and bbox[0] <= px <= bbox[2] and bbox[1] <= py <= bbox[3]
        distance = 0.0 if inside else ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
        if distance <= max_distance and (best is None or distance < best[1]):
            best = (el, distance)
    return best


def element_matches_target(element, target: str) -> bool:
    """判断元素是否与目标文字/描述匹配（text、resource-id、content-desc）。"""
    target = (target or "").strip().lower()
    if not target:
        return False
    for key in ("text", "resource_id", "content_desc"):
        value = str(element.get(key) or "").strip().lower()
        if value and (target in value or value in target):
            return True
    return False


def snap_action_coords(action, elements, max_distance=80):
    """把 tap 的 x/y 吸附到最近的元素中心；返回 (新 action, 移动记录)。

    给了 target 时优先在文字/resource-id/content-desc 匹配的元素里找；
    匹配不到合适元素再退回全部元素按距离找。距离超过 max_distance 时不吸附。
    """
    if action.get("action_type") not in OCR_TRIGGER_ACTIONS or not elements:
        return action, []
    x, y = action.get("x"), action.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return action, []

    candidates = elements
    filtered = False
    target = str(action.get("target") or "").strip()
    if target:
        matched = [el for el in elements if element_matches_target(el, target)]
        if matched:
            candidates = matched
            filtered = True

    found = nearest_element((x, y), candidates, max_distance)
    if not found:
        return action, []
    element, distance = found
    nx, ny = element["center"]
    if (nx, ny) == (x, y):
        return action, []

    snapped = dict(action)
    snapped["x"], snapped["y"] = nx, ny
    return snapped, [{
        "from": [x, y],
        "to": [nx, ny],
        "raw_center": element.get("raw_center"),
        "text": element.get("text", ""),
        "source": element.get("source", ""),
        "distance": round(distance, 1),
        "matched_target": filtered,
    }]


# ===== 测试 =====
if __name__ == "__main__":
    from phone_controller import PhoneController
    locator = OCRLocator()
    phone = PhoneController(locator)
    phone.screenshot("screen_current.png")
    
    print("双通道检测 screen_current.png ...\n")
    print(locator.format_for_prompt("screen_current.png"))
    # print(locator.detect("screen_current.png"))
    # print(locator._xml_detect())