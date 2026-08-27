from ultralytics import YOLO

model = YOLO("yolo26n.pt")

results = model("product2.jpeg")

for result in results:
    result.show()