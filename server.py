import atexit
import json
import math
import os
import random
import threading
import time
import wave
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

import tiktokdance as td

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "5000"))
STREAM_WIDTH = 960
JPEG_QUALITY = 80
STATE_HZ = 10
COUNTDOWN_SECONDS = 3
SERVER_DIR = Path(__file__).resolve().parent
#only these kinds of files are served to the browser (so your .py files, model and json never get exposed)
FRONTEND_TYPES = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
                  ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf"}


def frontend_dir():
    #looks for index.html in a folder called "frontend", then next to this file. FRONTEND_DIR env var overrides
    env = os.environ.get("FRONTEND_DIR")
    for folder in ([Path(env)] if env else [SERVER_DIR / "frontend", SERVER_DIR]):
        if (folder / "index.html").is_file():
            return folder
    return None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_500_000


class GameServer:

    def __init__(self):
        self.lock = threading.RLock()
        self.frame_lock = threading.Lock()
        self.running = True
        self.jpeg = None
        self.cap = None
        self.latest_cam = None
        self.initialized = False
        self.async_det = None
        self.thread = None
        self.thread_lock = threading.Lock()
        self.dances = {}
        self.fps = {}
        self.dance_seconds = {}
        self.round_seconds = {}
        self.ref = {}

        self.phase = "idle"
        self.dance = None
        self.pair = ""
        self.countdown_end = 0.0
        self.t0 = 0.0
        self.last_id = 0
        self.slots = [None, None]
        self.landmarks = [None, None]
        self.present = [False, False]
        self.clear_round()

    def clear_round(self):
        self.sums = np.zeros(2)
        self.scored = np.zeros(2)
        self.hits = [0, 0]
        self.inst = [0.0, 0.0]
        self.ema = 0.0
        self.best_ema = -1.0
        self.best_frame = None
        self.result = None

    def setup(self):
        if self.initialized:
            return
        model_path = td.find_model()
        self.dances = {d.name: d for d in td.load_dances()}

        # Create the normal and slowed audio sidecars for every dance.
        # Existing WAV/MP3 files are reused; missing ones are extracted from
        # the video's embedded soundtrack when imageio-ffmpeg is installed.
        for dance in self.dances.values():
            td.prepare_audio(dance)

        factory = lambda n: td.Detector(model_path, num_poses=n)
        for name, dance in self.dances.items():
            video = cv2.VideoCapture(str(dance.video))