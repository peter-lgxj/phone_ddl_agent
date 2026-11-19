import config


class InputDetector:
    def __init__(self):
        # 延迟导入：ultralytics/torch 只在真正加载 YOLO 时才需要
        from ultralytics import YOLO

        self.model = YOLO(config.INPUT_BOX_MODEL)
        print(f"InputDetector 加载：{config.INPUT_BOX_MODEL}")

    def detect(self, image_path: str, conf: float = 0.3) -> list[dict]:
        """Detect input boxes and search icons in device screenshot coordinates."""
        results = self.model(image_path, conf=conf, verbose=False)
        boxes = []
        for box in results[0].boxes:
            class_name = self.model.names[int(box.cls[0].item())]
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            cx = int((x1 + x2) / 2)
            cy = int((y1 + y2) / 2)
            boxes.append({
                "center": (cx, cy),
                "bbox": (int(x1), int(y1), int(x2), int(y2)),
                "confidence": round(box.conf[0].item(), 3),
                "class_name": class_name,
                "type": "input" if class_name == "input_box" else class_name,
                "text": "搜索" if class_name == "search_icon" else "输入框",
                "content_desc": "搜索 放大镜" if class_name == "search_icon" else "",
                "source": "yolo",
            })
        boxes.sort(key=lambda b: b["confidence"], reverse=True)
        return boxes

    def find_best(self, image_path: str, conf: float = 0.3) -> dict | None:
        boxes = [box for box in self.detect(image_path, conf)
                 if box["class_name"] == "input_box"]
        if boxes:
            print(f"YOLO 检测到输入框，置信度:{boxes[0]['confidence']}  坐标:{boxes[0]['center']}")
            return boxes[0]
        print("YOLO 未检测到输入框")
        return None
