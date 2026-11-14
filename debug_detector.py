# debug_detector.py
from ultralytics import YOLO
from PIL import Image
import config

print(f"模型路径：{config.INPUT_BOX_MODEL}")

model = YOLO(config.INPUT_BOX_MODEL)

# 直接用正式程序截的图测试
for img_path in ["screen_current.png", "screen_for_detection.png"]:
    try:
        img = Image.open(img_path)
        print(f"\n测试 {img_path}：尺寸={img.size} 模式={img.mode}")

        results = model(img_path, conf=0.3, verbose=False)
        boxes = results[0].boxes
        print(f"  检测到 {len(boxes)} 个框")
        for b in boxes:
            print(f"  置信度:{b.conf[0]:.3f}  bbox:{[int(x) for x in b.xyxy[0].tolist()]}")
    except Exception as e:
        print(f"  错误：{e}")