import json
import math
import random
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np

try:
    import pygame
except ImportError:
    pygame = None

try:
    import winsound
except ImportError:
    winsound = None

VERSION = "test build v5"

SCRIPT_DIR = Path(__file__).resolve().parent
DANCE_DIR = SCRIPT_DIR / "dances_videos"
PHOTO_DIR = SCRIPT_DIR / "photos"
LEADERBOARD_FILE = SCRIPT_DIR / "leaderboard.json"

CAMERA_INDEX = 0
#how long each round is, learning and challenge
CLIP_SECONDS = 15
SLOW_ROUNDS = 1
NORMAL_ROUNDS = 1
MAX_EXTRA_REPLAYS = 3
SLOW_SPEED = 0.75

#80% = 4 stars, 4 stars to pass
STAR_THRESHOLDS = [0.50, 0.60, 0.70, 0.80, 0.90]
PASS_STARS = 4

#these videos have two dancers, left dancer = player 1 and right dancer = player 2
DUO_DANCES = ("dance6", "dance7")

#forgiving on purpose, its a booth lol
PERFECT_DEG = 15
FALLOFF_DEG = 60
MIN_VISIBILITY = 0.35
MIN_SEGMENTS = 3
TIME_SLACK_FRAMES = (-6, -3, 0, 3, 6)

#a moment counts as "doing well" (star pops up) at this score
GOOD_SCORE = 0.80
STAR_FLY_SECONDS = 1.2

#smaller picture into mediapipe = way faster
DETECT_MAX_SIDE = 640

H = 480
TOP = 80
BOTTOM = 56
SIDE = 70
REF_W = 360
CAM_W = 853
WINDOW_W = REF_W + CAM_W
WINDOW = "Dance Challenge"
SCREEN_W = 1920
SCREEN_H = 1080

PLAYER_LABELS = ("PLAYER 1", "PLAYER 2")
#color dif
P1_COLOR = (255, 180, 0)
P2_COLOR = (0, 180, 255)

GOOD_COLOR = (80, 220, 80)
OK_COLOR = (0, 215, 255)
BAD_COLOR = (60, 60, 255)
IDLE_COLOR = (120, 120, 120)

BODY_PARTS = [
    (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
    (11, 23), (12, 24), (23, 24),
    (23, 25), (25, 27), (27, 29), (29, 31), (27, 31),
    (24, 26), (26, 28), (28, 30), (30, 32), (28, 32),
]

SEGMENTS = [(11, 13), (13, 15), (12, 14), (14, 16),
            (23, 25), (25, 27), (24, 26), (26, 28),
            (11, 12), (23, 24)]
WEIGHTS = np.array([1.0, 1.0, 1.0, 1.0, 0.7, 0.7, 0.7, 0.7, 0.4, 0.4])

BaseOptions = mp.tasks.BaseOptions
PoseLandmarker = mp.tasks.vision.PoseLandmarker
PoseLandmarkerOptions = mp.tasks.vision.PoseLandmarkerOptions
VisionRunningMode = mp.tasks.vision.RunningMode


def find_model():
    matches = sorted(SCRIPT_DIR.glob("pose_landmarker_heavy*.task"))
    if not matches:
        raise FileNotFoundError(
            f"No pose_landmarker_heavy*.task file found in {SCRIPT_DIR}")
    return matches[0]


def shrink(frame, max_side=DETECT_MAX_SIDE):
    h, w = frame.shape[:2]
    s = max_side / max(h, w)
    if s >= 1:
        return frame
    return cv2.resize(frame, (int(w * s), int(h * s)))


class Detector:

    def __init__(self, model_path, num_poses):
        #load model
        options = PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=VisionRunningMode.VIDEO,
            num_poses=num_poses,
        )
        self.landmarker = PoseLandmarker.create_from_options(options)
        self.last_ts = -1
        self.t0 = time.monotonic()

    def detect(self, bgr, ts_ms=None):
        rgb = cv2.cvtColor(shrink(bgr), cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        if ts_ms is None:
            ts_ms = int((time.monotonic() - self.t0) * 1000)
        #mediapipe needs the timestamps to keep going up
        if ts_ms <= self.last_ts:
            ts_ms = self.last_ts + 1
        self.last_ts = ts_ms
        return self.landmarker.detect_for_video(image, ts_ms).pose_landmarks

    def close(self):
        self.landmarker.close()


class AsyncDetector:
    #runs mediapipe in its own thread so the camera picture never waits on it

    def __init__(self, detector):
        self.detector = detector
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.frame = None
        self.result = None
        self.result_id = 0
        self.gen = 0
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit(self, frame):
        with self.lock:
            self.frame = frame
        self.wake.set()

    def latest(self):
        with self.lock:
            return self.result, self.result_id

    def reset(self):
        with self.lock:
            self.gen += 1
            self.result = None
            self.frame = None

    def _loop(self):
        while self.running:
            self.wake.wait(0.1)
            with self.lock:
                frame, gen = self.frame, self.gen
                self.frame = None
                self.wake.clear()
            if frame is None:
                continue
            try:
                poses = self.detector.detect(frame)
            except Exception as exc:
                print("Detection error:", exc)
                poses = []
            with self.lock:
                if gen == self.gen:
                    self.result = poses
                    self.result_id += 1

    def close(self):
        self.running = False
        self.wake.set()
        self.thread.join(timeout=2)
        self.detector.close()


class QuitApp(Exception):
    pass


class Dance:
    def __init__(self, video):
        self.video = video
        self.name = video.stem
        self.duo = self.name in DUO_DANCES
        self.audio = video.with_suffix(".mp3")
        self.audio_slow = video.with_name(video.stem + "_slow.mp3")


def load_dances():
    dances = [Dance(p) for p in sorted(DANCE_DIR.glob("dance*.mp4"))]
    if not dances:
        raise FileNotFoundError(f"No dance*.mp4 videos found in {DANCE_DIR}")
    return dances


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def make_audio(video, out_path, speed):
    exe = find_ffmpeg()
    if exe is None:
        return False
    cmd = [exe, "-y", "-t", str(CLIP_SECONDS), "-i", str(video), "-vn"]
    if speed != 1.0:
        cmd += ["-filter:a", f"atempo={speed}"]
    cmd.append(str(out_path))
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    good = result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000
    if not good and out_path.exists():
        out_path.unlink()
    return good


def prepare_audio(dance):
    global AUDIO_PROBLEM
    #no mp3 next to the video? pull the sound out of the video with ffmpeg
    for attr, speed, suffix in (("audio", 1.0, ""), ("audio_slow", SLOW_SPEED, "_slow")):
        mp3 = dance.video.with_name(dance.name + suffix + ".mp3")
        wav = mp3.with_suffix(".wav")
        if mp3.exists():
            setattr(dance, attr, mp3)
        elif wav.exists() and wav.stat().st_size > 1000:
            setattr(dance, attr, wav)
        elif make_audio(dance.video, wav, speed):
            setattr(dance, attr, wav)
        else:
            print(f"No audio for {dance.name}{suffix}")
            AUDIO_PROBLEM = "NO SOUND: run  pip install imageio-ffmpeg  or put danceN.mp3 next to the videos"
            setattr(dance, attr, None)
        print(f"audio {dance.name}{suffix}:", getattr(dance, attr))


AUDIO_OK = False
AUDIO_PROBLEM = ""
native_proc = None


def native_player():
    if winsound is not None:
        return "winsound"
    for name, args in (("afplay", []),
                       ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
                       ("aplay", ["-q"])):
        exe = shutil.which(name)
        if exe:
            return [exe] + args
    return None


def init_audio():
    global AUDIO_OK, AUDIO_PROBLEM
    #pygame first, if that doesnt work use the computers own player
    if pygame is not None:
        try:
            pygame.mixer.init()
            pygame.mixer.music.set_volume(1.0)
            AUDIO_OK = True
            print("audio: pygame")
            return
        except Exception as exc:
            print("pygame audio failed:", exc)
    if native_player() is None:
        AUDIO_PROBLEM = "NO SOUND: run  pip install pygame-ce"
    else:
        print("audio: system player")


def play_audio(path):
    global native_proc
    stop_audio()
    if path is None or not path.exists():
        return
    if AUDIO_OK:
        try:
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.play()
            return
        except Exception as exc:
            print("pygame could not play:", exc)
    player = native_player()
    if player == "winsound":
        if path.suffix.lower() == ".wav":
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    elif player:
        native_proc = subprocess.Popen(player + [str(path)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_audio():
    global native_proc
    if AUDIO_OK:
        pygame.mixer.music.stop()
    if winsound is not None:
        winsound.PlaySound(None, winsound.SND_PURGE)
    if native_proc is not None and native_proc.poll() is None:
        native_proc.terminate()
    native_proc = None


def pose_vector(landmarks, w, h):
    #angle of each arm and leg so body size and position dont matter
    vec = np.full(len(SEGMENTS), np.nan)
    for i, (a, b) in enumerate(SEGMENTS):
        la, lb = landmarks[a], landmarks[b]
        va = la.visibility if la.visibility is not None else 1.0
        vb = lb.visibility if lb.visibility is not None else 1.0
        if min(va, vb) < MIN_VISIBILITY:
            continue
        dx = (lb.x - la.x) * w
        dy = (lb.y - la.y) * h
        vec[i] = math.degrees(math.atan2(dy, dx))
    return vec


def pose_similarity(player_vec, ref_candidates):
    best = 0.0
    for ref in ref_candidates:
        diff = np.abs(player_vec - ref)
        diff = np.minimum(diff, 360 - diff)
        ok = ~np.isnan(diff)
        if ok.sum() < MIN_SEGMENTS:
            continue
        sim = np.clip(1 - np.maximum(diff[ok] - PERFECT_DEG, 0) / FALLOFF_DEG, 0, 1)
        best = max(best, float(np.average(sim, weights=WEIGHTS[ok])))
    return best


def center_x(landmarks):
    return (landmarks[11].x + landmarks[12].x + landmarks[23].x + landmarks[24].x) / 4


def assign_players(poses):
    #player 1 = left side, player 2 = right side
    slots = [None, None]
    for lms in sorted(poses, key=center_x):
        side = 0 if center_x(lms) < 0.5 else 1
        if slots[side] is None:
            slots[side] = lms
    return slots


def torso_size(landmarks):
    return abs((landmarks[23].y + landmarks[24].y) / 2 - (landmarks[11].y + landmarks[12].y) / 2)


def assign_duo(poses):
    #two dancers in the video: the left one is player 1, the right one is player 2
    if len(poses) > 2:
        poses = sorted(poses, key=torso_size, reverse=True)[:2]
    if len(poses) == 2:
        return sorted(poses, key=center_x)
    return assign_players(poses)


def load_reference(dance, detector_factory, progress=None):
    #duo dances save one row of angles per dancer, solo dances save one row total
    suffix = "_ref_duo.npy" if dance.duo else "_ref.npy"
    cache = dance.video.with_name(dance.name + suffix)
    #only analyze the video once, after that we reuse the saved file
    if cache.exists():
        return np.load(cache)

    print(f"Analyzing {dance.name} for the first time (cached afterwards)...")
    cap = cv2.VideoCapture(str(dance.video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    max_frames = int(CLIP_SECONDS * fps)
    det = detector_factory(2 if dance.duo else 1)
    rows = []
    #every 2nd frame is plenty, the scoring has a little slack anyway
    for i in range(0, max_frames, 2):
        ok, frame = cap.read()
        if not ok:
            break
        cap.grab()
        poses = det.detect(frame, int(i * 1000 / fps))
        if dance.duo:
            row = np.full((2, len(SEGMENTS)), np.nan)
            for s, lms in enumerate(assign_duo(poses)):
                if lms is not None:
                    row[s] = pose_vector(lms, w, h)
        else:
            row = pose_vector(poses[0], w, h) if poses else np.full(len(SEGMENTS), np.nan)
        rows += [row, row]
        if progress and i % 8 == 0:
            progress(i / max_frames)
    cap.release()
    det.close()
    arr = np.array(rows)
    np.save(cache, arr)
    return arr


def ref_candidates(ref, idx, player=0):
    out = []
    for o in TIME_SLACK_FRAMES:
        j = idx + o
        if 0 <= j < len(ref):
            row = ref[j, player] if ref.ndim == 3 else ref[j]
            if not np.all(np.isnan(row)):
                out.append(row)
    return out


def stars_for(acc):
    return sum(acc >= t for t in STAR_THRESHOLDS)


class ClipPlayer:
    def __init__(self, path, speed=1.0, read_frames=True):
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video: {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.speed = speed
        self.read_frames = read_frames
        self.max_frames = int(CLIP_SECONDS * self.fps)
        self.index = -1
        self.frame = None
        self.start = None

    def start_clock(self):
        self.start = time.monotonic()

    def elapsed(self):
        # Slow down video by increasing frame display time
        return (time.monotonic() - self.start) * self.speed

    def advance(self):
        #skip ahead if we fall behind so the video stays with the music
        target = int(self.elapsed() * self.fps)
        if target >= self.max_frames:
            return False
        if not self.read_frames:
            self.index = target
            return True
        while self.index < target:
            ok, f = self.cap.read()
            if not ok:
                return False
            self.index += 1
            self.frame = f
        return self.frame is not None

    def release(self):
        self.cap.release()


def first_frame(dance):
    cap = cv2.VideoCapture(str(dance.video))
    ok, f = cap.read()
    cap.release()
    return f if ok else None


def set_layout(frame):
    #size the display from the webcam's real shape so nothing gets stretched
    global CAM_W, WINDOW_W
    ch, cw = frame.shape[:2]
    CAM_W = int(H * cw / ch)
    WINDOW_W = REF_W + CAM_W


def fit_contain(img, w, h):
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = int(iw * s), int(ih * s)
    small = cv2.resize(img, (nw, nh))
    if (nw, nh) == (w, h):
        return small
    canvas = np.zeros((h, w, 3), np.uint8)
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = small
    return canvas


def prep_cam(frame):
    #mirror cams
    return cv2.flip(frame, 1)


def to_display(cam):
    return cv2.resize(cam, (CAM_W, H))


def text_center(img, text, y, scale=1.0, color=(255, 255, 255), thick=2, x_center=None):
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, _), _ = cv2.getTextSize(text, font, scale, thick)
    cx = img.shape[1] // 2 if x_center is None else x_center
    x = cx - tw // 2
    cv2.putText(img, text, (x, y), font, scale, (0, 0, 0), thick + 3, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), font, scale, color, thick, cv2.LINE_AA)


def text_left(img, text, x, y, scale=0.7, color=(255, 255, 255), thick=2):
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, text, (x, y), font, scale, (0, 0, 0), thick + 3, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), font, scale, color, thick, cv2.LINE_AA)


def draw_star(img, cx, cy, r, color, filled=True):
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((int(cx + rad * math.cos(ang)), int(cy + rad * math.sin(ang))))
    pts = np.array(pts, np.int32)
    if filled:
        cv2.fillPoly(img, [pts], color)
    cv2.polylines(img, [pts], True, (0, 0, 0) if filled else color, 2)


def draw_star_row(img, cx, cy, count, r=16, gap=40):
    start = cx - 2 * gap
    for i in range(5):
        if i < count:
            draw_star(img, start + i * gap, cy, r, (0, 215, 255), True)
        else:
            draw_star(img, start + i * gap, cy, r, (150, 150, 150), False)


def draw_skeleton(frame, landmarks, color):
    h, w = frame.shape[:2]
    pts = {i: (int(l.x * w), int(l.y * h)) for i, l in enumerate(landmarks)}
    for a, b in BODY_PARTS:
        cv2.line(frame, pts[a], pts[b], color, 3, cv2.LINE_AA)
    for p in pts.values():
        cv2.circle(frame, p, 5, color, -1)


def compose(ref_frame, cam_disp):
    if ref_frame is None:
        return cam_disp
    return np.hstack([fit_contain(ref_frame, REF_W, H), cam_disp])


def get_screen_size():
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        size = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        return size
    except Exception:
        return 1920, 1080


def display(img):
    #scale to the whole screen without stretching, black bars if the shapes dont match
    cv2.imshow(WINDOW, fit_contain(img, SCREEN_W, SCREEN_H))


def show(img, delay=1):
    display(img)
    k = cv2.waitKey(delay) & 0xFF
    #press q to exit
    if k in (ord("q"), 27):
        raise QuitApp
    return k


def read_cam(camera):
    ok, frame = camera.read()
    if not ok:
        raise RuntimeError("Camera frame failed")
    return frame


def message_canvas(camera):
    cam = to_display(prep_cam(read_cam(camera)))
    canvas = np.hstack([np.zeros((H, REF_W, 3), np.uint8), cam])
    return (canvas * 0.45).astype(np.uint8)


def wait_for_key(camera, lines, keys):
    while True:
        canvas = message_canvas(camera)
        y = H // 2 - 20 * (len(lines) - 1)
        for i, (text, scale, color) in enumerate(lines):
            text_center(canvas, text, y + i * 45, scale, color, 2)
        k = show(canvas)
        ch = chr(k).lower() if k < 128 else ""
        if ch in keys:
            return ch


def text_input(camera, prompt, default, max_len=16):
    text = ""
    while True:
        canvas = message_canvas(camera)
        text_center(canvas, prompt, H // 2 - 40, 1.0, (255, 255, 255))
        shown = text + ("_" if int(time.time() * 2) % 2 == 0 else " ")
        text_center(canvas, shown, H // 2 + 20, 1.3, (0, 215, 255), 3)
        text_center(canvas, f"Press ENTER (empty = \"{default}\")", H - 40, 0.6, (200, 200, 200), 1)
        display(canvas)
        k = cv2.waitKey(1) & 0xFF
        if k == 255:
            continue
        if k in (13, 10):
            return text.strip() or default
        if k in (8, 127):
            text = text[:-1]
        elif k == 27:
            raise QuitApp
        elif 32 <= k <= 126 and len(text) < max_len:
            text += chr(k)


def progress_screen(text, frac):
    canvas = np.zeros((H, WINDOW_W, 3), np.uint8)
    text_center(canvas, text, H // 2 - 20, 0.9)
    text_center(canvas, "first time only, this gets saved", H // 2 + 20, 0.6, (200, 200, 200), 1)
    x0, x1 = WINDOW_W // 4, 3 * WINDOW_W // 4
    cv2.rectangle(canvas, (x0, H // 2 + 50), (x1, H // 2 + 70), (120, 120, 120), 2)
    cv2.rectangle(canvas, (x0, H // 2 + 50), (x0 + int((x1 - x0) * frac), H // 2 + 70), (0, 215, 255), -1)
    show(canvas)


def sound_test(camera, dance):
    play_audio(dance.audio)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 4:
        canvas = message_canvas(camera)
        text_center(canvas, "SOUND TEST - can you hear it?", H // 2, 0.9, (0, 255, 0))
        show(canvas)
    stop_audio()


def countdown(camera, ref_first, title, seconds=3):
    t0 = time.monotonic()
    while True:
        left = seconds - int(time.monotonic() - t0)
        if left <= 0:
            break
        disp = to_display(prep_cam(read_cam(camera)))
        text_center(disp, str(left), H // 2 + 50, 5, (255, 255, 255), 8)
        text_center(disp, title, 50, 1.0, (255, 255, 255), 2)
        show(compose(ref_first, disp))
    for _ in range(10):
        disp = to_display(prep_cam(read_cam(camera)))
        text_center(disp, "GO!", H // 2 + 40, 4, (0, 255, 0), 7)
        show(compose(ref_first, disp))


def load_leaderboard():
    if LEADERBOARD_FILE.exists():
        try:
            return json.loads(LEADERBOARD_FILE.read_text())
        except Exception:
            return []
    return []


def best_per_pair(entries):
    best = {}
    for e in entries:
        k = e["pair"].lower()
        if k not in best or e["accuracy"] > best[k]["accuracy"]:
            best[k] = e
    return sorted(best.values(), key=lambda e: -e["accuracy"])


def add_leaderboard_entry(pair, dance, accuracy, stars):
    entries = load_leaderboard()
    prev = [e for e in entries if e["pair"].lower() == pair.lower()]
    prev_best = max((e["accuracy"] for e in prev), default=None)
    #save every try so we can see who gets better over time
    entries.append({
        "pair": pair, "dance": dance, "accuracy": round(accuracy, 4),
        "stars": stars, "time": datetime.now().isoformat(timespec="seconds"),
    })
    LEADERBOARD_FILE.write_text(json.dumps(entries, indent=2))
    new_best = prev_best is None or accuracy > prev_best
    return entries, new_best


def leaderboard_panel(entries, current_pair, new_best, passed):
    w = WINDOW_W - CAM_W
    p = np.full((H, w, 3), (50, 50, 50), np.uint8)
    text_center(p, "LEADERBOARD", 40, 0.95, (255, 255, 255), 2)
    ranked = best_per_pair(entries)
    for i, e in enumerate(ranked[:5]):
        y = 90 + i * 45
        mine = e["pair"].lower() == current_pair.lower()
        color = (0, 215, 255) if mine else (255, 255, 255)
        text_left(p, f"{i + 1}. {e['pair'][:14]}", 15, y, 0.7, color)
        text_left(p, f"{e['accuracy'] * 100:.0f}%", w - 85, y, 0.7, color)
    rank = next((i + 1 for i, e in enumerate(ranked) if e["pair"].lower() == current_pair.lower()), None)
    if rank and rank > 5:
        text_left(p, f"You: #{rank} ({ranked[rank - 1]['accuracy'] * 100:.0f}%)", 15, 340, 0.65, (0, 215, 255))
    if new_best:
        text_center(p, "NEW PERSONAL BEST!", 380, 0.7, (0, 255, 0), 2)
    keys = "SPACE: next pair"
    if passed:
        keys += "   N: another dance"
    text_center(p, keys, H - 30, 0.55, (220, 220, 220), 1)
    return p


def play_learning_clip(camera, dance, label, speed, audio):
    player = ClipPlayer(dance.video, speed)
    play_audio(audio)
    player.start_clock()
    while player.advance():
        cam = prep_cam(read_cam(camera))
        #resize both frames
        disp = to_display(cam)
        text_left(disp, "YOUR CAMERA - copy the dancer!", 20, 35, 0.8)
        text_left(disp, f"{label}  {max(CLIP_SECONDS - player.elapsed(), 0):.0f}s", 20, 70, 0.7)
        if dance.duo:
            text_left(disp, "DUO: copy the dancer on your side", 20, 105, 0.6, (0, 215, 255))
        # split webcam and dance vid
        show(compose(player.frame, disp))
    stop_audio()
    player.release()


def learn_phase(camera, dance):
    ref0 = first_frame(dance)
    #first viewing is slow
    rounds = [("SLOW", SLOW_SPEED, dance.audio_slow)] * SLOW_ROUNDS
    #next viewing is fast
    rounds += [("NORMAL", 1.0, dance.audio)] * NORMAL_ROUNDS
    for i, (label, speed, audio) in enumerate(rounds, 1):
        countdown(camera, ref0, f"Learn it: {label} ({i}/{len(rounds)})")
        play_learning_clip(camera, dance, f"{label} {i}/{len(rounds)}", speed, audio)

    #optional replays
    replays = 0
    while True:
        left = MAX_EXTRA_REPLAYS - replays
        lines = [("Ready for the challenge?", 1.1, (255, 255, 255)),
                 (f"{PLAYER_LABELS[0]} = LEFT side    {PLAYER_LABELS[1]} = RIGHT side", 0.8, (0, 215, 255)),
                 ("SPACE = start!", 0.9, (0, 255, 0))]
        if dance.duo:
            lines.insert(2, (f"{PLAYER_LABELS[0]} copies the LEFT dancer, {PLAYER_LABELS[1]} copies the RIGHT dancer",
                             0.7, (0, 215, 255)))
        keys = {" "}
        if left > 0:
            lines.append((f"R = watch the dance again ({left} left)", 0.8, (255, 255, 255)))
            keys.add("r")
        if wait_for_key(camera, lines, keys) != "r":
            return
        replays += 1
        countdown(camera, ref0, "Replay")
        play_learning_clip(camera, dance, f"REPLAY {replays}", 1.0, dance.audio)


def quality_color(score, present):
    if not present:
        return IDLE_COLOR
    if score >= GOOD_SCORE:
        return GOOD_COLOR
    if score >= 0.5:
        return OK_COLOR
    return BAD_COLOR


def build_challenge_frame(disp, live, inst, present, hits, star_fx, elapsed, now):
    #everything goes in the margins around the webcam so it never covers the mediapipe dots
    fw = CAM_W + 2 * SIDE
    fh = TOP + H + BOTTOM
    frame = np.full((fh, fw, 3), (35, 35, 35), np.uint8)
    frame[TOP:TOP + H, SIDE:SIDE + CAM_W] = disp
    half = CAM_W // 2
    colors = (P1_COLOR, P2_COLOR)

    #colored border around each side, green = nailing it, yellow = ok, red = off
    for s in (0, 1):
        col = quality_color(inst[s], present[s])
        if s == 0:
            x0, x1 = SIDE - 4, SIDE + half - 3
        else:
            x0, x1 = SIDE + half + 3, SIDE + CAM_W + 4
        cv2.rectangle(frame, (x0, TOP - 4), (x1, TOP + H + 3), col, 6)

    #side rails: a meter for how well they match right now, stars float up next to it
    for s in (0, 1):
        col = quality_color(inst[s], present[s])
        rx = 0 if s == 0 else SIDE + CAM_W
        mx = rx + (8 if s == 0 else SIDE - 24)
        cv2.rectangle(frame, (mx, TOP), (mx + 16, TOP + H), (70, 70, 70), -1)
        fill = int(H * min(max(inst[s], 0.0), 1.0))
        if fill > 0:
            cv2.rectangle(frame, (mx, TOP + H - fill), (mx + 16, TOP + H), col, -1)
        cv2.rectangle(frame, (mx, TOP), (mx + 16, TOP + H), (200, 200, 200), 1)

    for fx in star_fx:
        age = (now - fx["born"]) / STAR_FLY_SECONDS
        if age >= 1:
            continue
        x = (44 if fx["s"] == 0 else SIDE + CAM_W + 26) + fx["dx"]
        y = TOP + H - 16 - int(age * (H - 32))
        fade = 1 - age ** 2
        draw_star(frame, x, y, 11, tuple(int(c * fade) for c in (0, 215, 255)), True)

    #top bar: each player's name, score and stars, challenge label in the middle
    for s in (0, 1):
        cx = SIDE + CAM_W // 4 + s * (CAM_W // 2)
        text_center(frame, f"{PLAYER_LABELS[s]} {live[s] * 100:.0f}%", 32, 0.8, colors[s], 2, x_center=cx)
        draw_star_row(frame, cx, 60, stars_for(live[s]), r=11, gap=28)
    cx = fw // 2
    if int(elapsed * 2) % 2 == 0:
        cv2.circle(frame, (cx - 95, 24), 8, (0, 0, 255), -1)
    text_center(frame, "CHALLENGE", 32, 0.8, (255, 255, 255), 2, x_center=cx + 10)
    text_center(frame, f"{max(CLIP_SECONDS - elapsed, 0):.0f}s", 62, 0.8, (255, 255, 255), 2, x_center=cx)

    #bottom bar: time progress and how many good moments each player has hit
    prog = int(CAM_W * min(elapsed / CLIP_SECONDS, 1))
    cv2.rectangle(frame, (SIDE, TOP + H + 10), (SIDE + CAM_W, TOP + H + 20), (80, 80, 80), -1)
    cv2.rectangle(frame, (SIDE, TOP + H + 10), (SIDE + prog, TOP + H + 20), OK_COLOR, -1)
    for s in (0, 1):
        cx = SIDE + CAM_W // 4 + s * (CAM_W // 2)
        text_center(frame, f"STAR HITS {hits[s]}", TOP + H + 46, 0.6, colors[s], 2, x_center=cx)
    return frame


def run_challenge(camera, async_det, dance, ref):
    countdown(camera, None, "CHALLENGE!")

    #video stays off during the challenge, only the audio plays
    player = ClipPlayer(dance.video, 1.0, read_frames=False)
    async_det.reset()
    _, last_id = async_det.latest()
    play_audio(dance.audio)
    player.start_clock()

    sums = np.zeros(2)
    scored = np.zeros(2)
    hits = [0, 0]
    inst = [0.0, 0.0]
    present = [False, False]
    last_spawn = [0.0, 0.0]
    star_fx = []
    ema, best_ema, best_frame = 0.0, -1.0, None
    colors = (P1_COLOR, P2_COLOR)
    slots = [None, None]

    while player.advance():
        #mirror
        cam = prep_cam(read_cam(camera))
        async_det.submit(cam)
        poses, rid = async_det.latest()
        new_result = poses is not None and rid != last_id
        now = time.monotonic()

        sims = [0.0, 0.0]
        if new_result:
            last_id = rid
            slots = assign_players(poses)
            #solo dance: both copy the same dancer, duo dance: each copies the dancer on their side
            cands = [ref_candidates(ref, player.index, s) for s in (0, 1)]
            #score both players separately
            for s in (0, 1):
                if slots[s] is not None and cands[s]:
                    vec = pose_vector(slots[s], cam.shape[1], cam.shape[0])
                    sims[s] = pose_similarity(vec, cands[s])
                if cands[s]:
                    sums[s] += sims[s]
                    scored[s] += 1
            for s in (0, 1):
                present[s] = slots[s] is not None
                inst[s] = 0.6 * inst[s] + 0.4 * sims[s]
                #doing well on this comparison = a star floats up their side
                if cands[s] and sims[s] >= GOOD_SCORE:
                    hits[s] += 1
                    if now - last_spawn[s] > 0.12:
                        last_spawn[s] = now
                        star_fx.append({"s": s, "born": now, "dx": random.randint(-6, 6)})

        disp = to_display(cam)
        for s in (0, 1):
            if slots[s] is not None:
                draw_skeleton(disp, slots[s], colors[s])

        #keep track of the best moment for the photo
        if new_result:
            ema = 0.8 * ema + 0.2 * (sims[0] + sims[1]) / 2
            if scored.max() > 10 and slots[0] is not None and slots[1] is not None and ema > best_ema:
                best_ema = ema
                best_frame = disp.copy()

        cv2.line(disp, (CAM_W // 2, 0), (CAM_W // 2, H), (255, 255, 255), 2)
        for s in (0, 1):
            if not present[s]:
                text_center(disp, f"{PLAYER_LABELS[s]}: step onto your side!", H // 2, 0.6,
                            (0, 0, 255), 2, x_center=CAM_W // 4 + s * CAM_W // 2)

        star_fx = [f for f in star_fx if now - f["born"] < STAR_FLY_SECONDS]
        live = sums / np.maximum(scored, 1)
        show(build_challenge_frame(disp, live, inst, present, hits, star_fx, player.elapsed(), now))

    stop_audio()
    player.release()
    acc = sums / np.maximum(scored, 1)
    if best_frame is None:
        best_frame = np.zeros((H, CAM_W, 3), np.uint8)
    return acc, best_frame


def make_photo(best_frame, pair_name, accs, pair_acc, stars, passed):
    img = best_frame.copy()
    cv2.line(img, (CAM_W // 2, 0), (CAM_W // 2, H), (255, 255, 255), 1)
    banner = img[H - 120:H]
    img[H - 120:H] = cv2.addWeighted(banner, 0.35, np.zeros_like(banner), 0.65, 0)
    draw_star_row(img, CAM_W // 2, 35, stars, r=16, gap=38)
    title = "PASSED!" if passed else "TRY AGAIN"
    text_center(img, title, 90, 1.2, (0, 255, 0) if passed else (200, 200, 200), 3)
    text_center(img, pair_name, 125, 0.7, (255, 255, 255), 2)
    text_center(img, f"{PLAYER_LABELS[0]} {accs[0] * 100:.0f}%", H - 65, 0.8, P1_COLOR, 2, x_center=CAM_W // 6)
    text_center(img, f"{PLAYER_LABELS[1]} {accs[1] * 100:.0f}%", H - 65, 0.8, P2_COLOR, 2, x_center=CAM_W - CAM_W // 6)
    text_center(img, f"{pair_acc * 100:.0f}%", H - 65, 1.3, (255, 255, 255), 3)
    #save the photo with the scores on it
    PHOTO_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", pair_name)[:20] or "pair"
    path = PHOTO_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.jpg"
    cv2.imwrite(str(path), img)
    print("Saved photo:", path)
    return img, path


def results_screen(photo, entries, pair_name, new_best, passed):
    right = leaderboard_panel(entries, pair_name, new_best, passed)
    screen = np.hstack([photo, right])
    while True:
        k = show(screen, delay=20)
        ch = chr(k).lower() if k < 128 else ""
        if ch == " ":
            return "space"
        if ch == "n" and passed:
            return "n"


def play_pair(camera, async_det, dances, ref_cache, pair_name):
    dance = random.choice(dances)
    while True:
        learn_phase(camera, dance)
        accs, best_frame = run_challenge(camera, async_det, dance, ref_cache[dance.name])

        pair_acc = float(accs.mean())
        stars = stars_for(pair_acc)
        passed = stars >= PASS_STARS
        print(f"{pair_name}: P1 {accs[0]:.0%}  P2 {accs[1]:.0%}  "
              f"pair {pair_acc:.0%}  stars {stars}  passed={passed}")

        photo, _ = make_photo(best_frame, pair_name, accs, pair_acc, stars, passed)
        entries, new_best = add_leaderboard_entry(pair_name, dance.name, pair_acc, stars)
        choice = results_screen(photo, entries, pair_name, new_best, passed)
        if choice == "n" and passed:
            others = [d for d in dances if d.name != dance.name] or dances
            dance = random.choice(others)
            continue
        return


def main():
    global SCREEN_W, SCREEN_H
    print("tiktokdance", VERSION)
    SCREEN_W, SCREEN_H = get_screen_size()
    model_path = find_model()
    print("Model:", model_path)
    dances = load_dances()
    print("Dances:", [d.name for d in dances])

    init_audio()
    for d in dances:
        prepare_audio(d)

    camera = cv2.VideoCapture(CAMERA_INDEX)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if not camera.isOpened():
        raise RuntimeError("Could not open webcam")
    set_layout(read_cam(camera))

    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    async_det = None
    try:
        factory = lambda n: Detector(model_path, num_poses=n)
        ref_cache = {}
        for n, d in enumerate(dances, 1):
            ref_cache[d.name] = load_reference(
                d, factory,
                lambda f, n=n, d=d: progress_screen(f"Getting {d.name} ready ({n}/{len(dances)})", f))

        async_det = AsyncDetector(Detector(model_path, num_poses=2))

        while True:
            #waits here till the next pair steps up
            lines = [
                ("DANCE CHALLENGE", 1.8, (255, 255, 255)),
                (f"{PLAYER_LABELS[0]}: LEFT side   |   {PLAYER_LABELS[1]}: RIGHT side", 0.8, (0, 215, 255)),
                ("Press SPACE to start", 0.9, (0, 255, 0)),
                ("T = test the sound", 0.6, (220, 220, 220)),
            ]
            if AUDIO_PROBLEM:
                lines.append((AUDIO_PROBLEM, 0.6, (0, 0, 255)))
            if wait_for_key(camera, lines, {" ", "t"}) == "t":
                sound_test(camera, dances[0])
                continue
            pair_name = text_input(camera, "Enter your pair name:", "Pair")
            play_pair(camera, async_det, dances, ref_cache, pair_name)
    except QuitApp:
        pass
    finally:
        camera.release()
        if async_det is not None:
            async_det.close()
        stop_audio()
        if AUDIO_OK:
            pygame.mixer.quit()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
