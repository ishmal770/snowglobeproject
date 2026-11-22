import cv2
import mediapipe as mp
import numpy as np
import time
import sys
from pathlib import Path


BaseOptions = mp.tasks.BaseOptions
PoseLandmarker = mp.tasks.vision.PoseLandmarker
PoseLandmarkerOptions = mp.tasks.vision.PoseLandmarkerOptions
VisionRunningMode = mp.tasks.vision.RunningMode

MODEL_PATH = "pose_landmarker_heavy (1).task"

body_parts = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31),
    (27, 31), (24, 26), (26, 28), (28, 30), (30, 32),
    (28, 32)
]



def on_result(result, output_image: mp.Image, timestamp_ms: int):
    print(f"Detected {len(result.pose_landmarks)} pose(s) at {timestamp_ms} ms")

options = PoseLandmarkerOptions(
    base_options=BaseOptions(model_asset_path="pose_landmarker_heavy (1).task"),
    running_mode=VisionRunningMode.LIVE_STREAM,
    num_poses=1,
    result_callback=on_result,
)

with PoseLandmarker.create_from_options(options) as landmarker:
    # Async: automatically drops incoming camera frames when busy so results stay current
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
    landmarker.detect_async(mp_image, timestamp_ms)