import os
import subprocess
import xml.etree.ElementTree as ET
import re
import warnings
warnings.filterwarnings("ignore")
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
import easyocr
import uiautomator2 as u2  # 新增导入

class OCRLocator:
    """
    屏幕元素定位器：OCR + XML 双通道融合
    - OCR：识别所有文字和坐标，通用，任何 App 都能用
    - XML：获取精确控件坐标，仅支持未屏蔽控件树的 App
    - 两者结果合并去重后输出，互相补充
    """

    def __init__(self):
        self.reader = easyocr.Reader(['ch_sim', 'en'], verbose=False)
        # 初始化 u2 设备连接，默认连接第一个 USB 设备或模拟器
        # 如果需要指定设备，可以使用 u2.connect('serial_number')
        try:
            self.d = u2.connect()
        except Exception as e:
            print(f"Warning: Could not connect to device via uiautomator2: {e}")
            self.d = None

    # ------------------------------------------------------------------ #
    #  OCR 通道
    # ------------------------------------------------------------------ #
    def _ocr_detect(self, image_path: str) -> list[dict]:
        """EasyOCR 识别文字元素"""
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            return []
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        results = self.reader.readtext(img_rgb)
        elements = []
        for (box, text, confidence) in results:
            if confidence < 0.4 or not text.strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = int(sum(xs) / 4)
            cy = int(sum(ys) / 4)
            elements.append({
                "text": text.strip(),
                "center": (cx, cy),
                "confidence": round(confidence, 2),
                "source": "ocr",
                "type": "text",
            })
        return elements

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