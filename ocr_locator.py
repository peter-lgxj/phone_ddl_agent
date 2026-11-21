import os
import subprocess
import xml.etree.ElementTree as ET
import re
from difflib import SequenceMatcher
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
        self._tree_provider = None  # 可选：由当前驱动提供 dump_hierarchy()

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

    def set_tree_provider(self, provider) -> None:
        """绑定当前驱动的 dump_hierarchy()，让控件树通道跟随 u2 / adb_dump 切换。"""
        self._tree_provider = provider

    def detect_tree(self) -> list[dict]:
        """只取控件树元素（不做 OCR），供「控件树优先」路径使用"""
        return self._xml_detect()

    def detect_ocr(self, image_path: str) -> list[dict]:
        """只跑 OCR（不取控件树），供「OCR 兜底」路径使用"""
        return self._ocr_detect(image_path)

    def warmup(self) -> None:
        """提前加载模型，避免第一步把 7s 加载时间算进 OCR 耗时。"""
        if self._engine_name == "easyocr":
            if self.reader is None:
                import easyocr
                self.reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
            return
        if self._rapid_reader is None:
            from rapidocr_onnxruntime import RapidOCR
            self._rapid_reader = RapidOCR()
        import numpy as np
        self._rapid_reader(np.zeros((32, 32, 3), dtype=np.uint8), use_cls=False)

    # ------------------------------------------------------------------ #
    #  XML 通道 (已修改为使用 uiautomator2)
    # ------------------------------------------------------------------ #
    def _xml_detect(self) -> list[dict]:
        """
        使用 uiautomator2 获取控件树，提取可交互元素。
        相比 adb shell dump，这种方式更稳定且无需文件中转。
        """
        if not self._tree_provider and not self.d:
            return []

        try:
            # 优先用当前驱动的 dump_hierarchy（u2 或 adb_dump），保证回退后仍能取控件树
            if self._tree_provider is not None:
                xml_content = self._tree_provider()
            else:
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


def target_match_score(element, target: str) -> float:
    """给候选元素的文字、资源 ID 和无障碍描述打分。"""
    target = re.sub(r"[^\w]+", "", (target or "").lower())
    if not target:
        return 0.0
    best = 0.0
    for key in ("text", "resource_id", "content_desc"):
        value = re.sub(r"[^\w]+", "", str(element.get(key) or "").lower())
        if not value:
            continue
        if target == value:
            return 1.0
        if min(len(target), len(value)) >= 2 and (target in value or value in target):
            best = max(best, 0.8 + 0.2 * min(len(target), len(value)) / max(len(target), len(value)))
        elif min(len(target), len(value)) >= 4:
            ratio = SequenceMatcher(None, target, value).ratio()
            if ratio >= 0.82:
                best = max(best, ratio * 0.8)
    return best


def element_matches_target(element, target: str) -> bool:
    return target_match_score(element, target) > 0


def canonical_action_target(action) -> str:
    """Strip role words from model targets such as 联系人'昊' or 联系人 昊."""
    target = str(action.get("target") or "").strip()
    quoted = re.search(r"[‘’'\"“”「」]([^‘’'\"“”「」]+)[‘’'\"“”「」]", target)
    if quoted:
        return quoted.group(1).strip()
    role_prefixes = ("联系人", "群聊", "聊天项", "搜索结果", "搜索图标", "输入框")
    for prefix in role_prefixes:
        if target.startswith(prefix):
            tail = target[len(prefix):].strip(" :：'\"‘’“”「」")
            return tail or ("搜索" if prefix == "搜索图标" else target)
    return target


def action_element_role_matches(action, element) -> bool:
    """Use the requested action, not just its text, to distinguish inputs from results."""
    description = " ".join((str(action.get("description") or ""),
                            str(action.get("target") or "")))
    is_input = element.get("type") == "input"
    if "搜索图标" in description or "放大镜" in description:
        return element.get("type") == "search_icon" or (
            element.get("source") == "xml" and element.get("type") == "control")
    if any(word in description for word in ("联系人", "群聊", "聊天项", "搜索结果")):
        return not is_input
    if "输入框" in description or action.get("target_type") == "input_box":
        return is_input
    return True


def mark_text_in_input_boxes(ocr_elements, tree_elements):
    """Tag OCR text inside detected input bounds so it cannot mimic a result."""
    input_boxes = [element["bbox"] for element in tree_elements
                   if element.get("type") == "input" and element.get("bbox")]
    tagged = []
    for element in ocr_elements:
        center = element.get("center")
        if center and any(x1 <= center[0] <= x2 and y1 <= center[1] <= y2
                          for x1, y1, x2, y2 in input_boxes):
            element = dict(element, type="input")
        tagged.append(element)
    return tagged


def resolve_action_element(action, elements, max_distance=80):
    """One text match: use it directly. Multiple: choose nearest to model coords."""
    x, y = action.get("x"), action.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    target = canonical_action_target(action)
    eligible = [element for element in elements
                if element.get("center") and action_element_role_matches(action, element)]
    if target:
        scored = [(target_match_score(element, target), element) for element in eligible]
        best_score = max((score for score, _ in scored), default=0.0)
        matches = [element for score, element in scored if score == best_score and score > 0]
        if not matches:
            return None
        element = min(matches, key=lambda item:
                      (item["center"][0] - x) ** 2 + (item["center"][1] - y) ** 2)
        distance = ((element["center"][0] - x) ** 2
                    + (element["center"][1] - y) ** 2) ** 0.5
        return element, distance, True
    found = nearest_element((x, y), eligible, max_distance)
    if not found:
        return None
    element, distance = found
    return element, distance, False


_LIST_TARGET_WORDS = ("群聊", "群列表", "联系人", "聊天项", "会话", "搜索结果")


def enforce_tap_bounds(action, elements, screen_size):
    """用控件树 bounds 校正 tap 坐标；越界/歧义时返回 None 由调用方拒绝执行。

    判据：
    - 映射后坐标落在屏幕外 → 拒绝（``outside_screen``）；
    - 目标文字能唯一定位到一个 bbox → 直接用该 bbox 中心，不再相信比例换算；
    - 群列表/会话列表等界面目标歧义或无法定位 → 拒绝（``ambiguous_list_target`` /
      ``list_target_unresolved``），禁止退回比例坐标；
    - 非列表界面且目标无法定位 → 保留模型坐标（仅要求仍在屏幕内）。
    """
    if action.get("action_type") != "tap":
        return action, {"allowed": True, "reason": "not_tap"}
    x, y = action.get("x"), action.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return action, {"allowed": True, "reason": "no_coords"}
    width, height = screen_size
    if not (0 <= x < width and 0 <= y < height):
        return None, {
            "allowed": False,
            "reason": "outside_screen",
            "target": canonical_action_target(action),
            "point": [x, y],
            "screen_size": [width, height],
        }

    target = canonical_action_target(action)
    description = str(action.get("description") or "")
    is_list = any(word in target + description for word in _LIST_TARGET_WORDS)
    eligible = [element for element in elements
                if element.get("center") and action_element_role_matches(action, element)]

    if target:
        scored = [(target_match_score(element, target), element) for element in eligible]
        best = max((score for score, _ in scored), default=0.0)
        matches = [element for score, element in scored if score == best and score > 0]
        boxed = [element for element in matches if element.get("bbox")]
        boxes = {tuple(element["bbox"]) for element in boxed}
        if len(boxes) == 1:
            element = boxed[0]
            cx, cy = element["center"]
            return {**action, "x": cx, "y": cy}, {
                "allowed": True,
                "source": "bounds",
                "target": target,
                "bbox": list(next(iter(boxes))),
                "model_point": [x, y],
                "moved": [x, y] != [cx, cy],
            }
        if matches and not boxes:
            # OCR 结果只有文本框中心、没有 bbox：中心本身来自像素识别，
            # 不是比例换算；唯一命中时直接采用。
            centers = {tuple(element["center"]) for element in matches}
            if len(centers) == 1:
                cx, cy = next(iter(centers))
                return {**action, "x": cx, "y": cy}, {
                    "allowed": True,
                    "source": "ocr_center",
                    "target": target,
                    "model_point": [x, y],
                    "moved": [x, y] != [cx, cy],
                }
            if is_list:
                return None, {
                    "allowed": False,
                    "reason": "ambiguous_list_target",
                    "target": target,
                    "candidates": len(centers),
                }
        if len(boxes) > 1 and is_list:
            return None, {
                "allowed": False,
                "reason": "ambiguous_list_target",
                "target": target,
                "candidates": len(boxes),
            }

    if is_list:
        return None, {
            "allowed": False,
            "reason": "list_target_unresolved",
            "target": target,
        }
    return action, {"allowed": True, "source": "model_coords", "target": target}


def snap_action_coords(action, elements, max_distance=80):
    """把 tap 解析到候选元素；具名目标按匹配数量选择。"""
    if action.get("action_type") not in OCR_TRIGGER_ACTIONS or not elements:
        return action, []
    found = resolve_action_element(action, elements, max_distance)
    if not found:
        return action, []
    x, y = action["x"], action["y"]
    element, distance, filtered = found
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
