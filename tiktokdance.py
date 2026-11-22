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

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODEL_PATH = SCRIPT_DIR / "pose_landmarker_heavy (1).task"
print("Model path:", MODEL_PATH)
print("Model exists:", MODEL_PATH.exists())
print("Model size:", MODEL_PATH.stat().st_size / (1024 * 1024), "MB" if MODEL_PATH.exists() else "N/A")

body_parts = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31),
    (27, 31), (24, 26), (26, 28), (28, 30), (30, 32),
    (28, 32)
]

detected_people = []

import cv2

camera = cv2.VideoCapture(0)

dances = [
    "Dance 1",
    "Dance 2",
    "Dance 3",
    "Dance 4",
    "Dance 5"
]

current_dance = 0





def draw_skeleton(frame, landmarks, color):
    height, width, _ = frame.shape

    points = {}

    for index, landmark in enumerate(landmarks):
        x = int(landmark.x * width)
        y = int(landmark.y * height)
        points[index] = (x, y)

    

    for point in points.values():
        cv2.circle(frame, point, 5, color, -1)
    
options = PoseLandmarkerOptions( #load model
    base_options=BaseOptions(
        model_asset_path=str(MODEL_PATH)
    ),
    running_mode=VisionRunningMode.VIDEO,
    num_poses=2
)




camera = cv2.VideoCapture(0)

start_time = time.monotonic()


elsa_color = (255, 180, 0)#color dif
anna_color = (0, 180, 255)


with PoseLandmarker.create_from_options(options) as detector:

    while camera.isOpened():

        success, frame = camera.read()

        if not success:
            print("Camera frame failed.")
            break

        #mirror

        frame = cv2.flip(frame, 1)

        height, width, _ = frame.shape
        middle = width // 2

       
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        mp_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=rgb_frame
        )

        timestamp_ms = int((time.monotonic() - start_time) * 1000)


        results = detector.detect_for_video(
            mp_image,
            timestamp_ms
        )

        cv2.line(
            frame,
            (middle, 0),
            (middle, height),
            (255, 255, 255),
            2
        )

        cv2.putText(
            frame,
            "ELSA - PLAYER 1",
            (30, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            elsa_color,
            2
        )

        cv2.putText(
            frame,
            "ANNA - PLAYER 2",
            (middle + 30, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            anna_color,
            2
        )

        detected_people = []

        for landmarks in results.pose_landmarks:

            body_center = (
                landmarks[11].x +
                landmarks[12].x +
                landmarks[23].x +
                landmarks[24].x
            ) / 4

            detected_people.append(
                (body_center, landmarks)
            )


        detected_people.sort(
            key=lambda person: person[0]
        )

        for i, (center, landmarks) in enumerate(detected_people):

            if i == 0:
                draw_skeleton(frame, landmarks, elsa_color)

            elif i == 1:
                draw_skeleton(frame, landmarks, anna_color)

     
        cv2.putText(
            frame,
            f"Players detected: {len(detected_people)}",
            (30, height - 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2
        )
        
        cv2.imshow("DeFrost - Player Tracking", frame)

        #press q to exit
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break


