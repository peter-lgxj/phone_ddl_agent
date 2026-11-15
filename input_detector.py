from PIL import Image
import config


class InputDetector:
    def __init__(self):
        # 延迟导入：ultralytics/torch 只在真正加载 YOLO 时才需要
        from ultralytics import YOLO

        self.model = YOLO(config.INPUT_BOX_MODEL)
        print(f"InputDetector 加载：{config.INPUT_BOX_MODEL}")

    def detect(self, image_path: str, conf: float = 0.3) -> list[dict]:
        """和 test_detector.py 完全一致的检测方式"""
        img = Image.open(image_path)
        w, h = img.size

        # 直接传路径给 YOLO，不做任何预处理
        results = self.model(image_path, conf=conf, verbose=False)
        boxes = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx = int((x1 + x2) / 2)
            cy = int((y1 + y2) / 2)
            boxes.append({
                "center": (cx, cy),
                "bbox": (int(x1), int(y1), int(x2), int(y2)),
                "confidence": round(box.conf[0].item(), 3),
            })
        boxes.sort(key=lambda b: b["confidence"], reverse=True)
        return boxes

    def find_best(self, image_path: str, conf: float = 0.3) -> dict | None:
        boxes = self.detect(image_path, conf)
        if boxes:
            print(f"YOLO 检测到输入框，置信度:{boxes[0]['confidence']}  坐标:{boxes[0]['center']}")
            return boxes[0]
        print("YOLO 未检测到输入框")
        return None