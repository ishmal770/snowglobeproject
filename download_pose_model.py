"""Download the official MediaPipe Lite Pose Landmarker model for deployment."""
from pathlib import Path
from urllib.request import urlopen
URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"
DEST = Path(__file__).resolve().parent / "pose_landmarker_lite.task"
if not DEST.exists() or DEST.stat().st_size < 1_000_000:
    print("Downloading MediaPipe Pose Landmarker Lite model...")
    with urlopen(URL, timeout=90) as response, DEST.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
print("Pose model ready:", DEST, DEST.stat().st_size, "bytes")
