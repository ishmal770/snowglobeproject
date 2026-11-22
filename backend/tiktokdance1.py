import json
import math
import random
import re
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

#80% = 4 stars and 4 stars frees olaf
STAR_THRESHOLDS = [0.50, 0.60, 0.70, 0.80, 0.90]
FREE_OLAF_STARS = 4

#forgiving on purpose, its a booth lol
PERFECT_DEG = 15
FALLOFF_DEG = 60
MIN_VISIBILITY = 0.35
MIN_SEGMENTS = 3
TIME_SLACK_FRAMES = (-6, -3, 0, 3, 6)

H = 432
CAM_W = 768
REF_W = 243
PANEL_W = 220
WINDOW_W = REF_W + CAM_W + PANEL_W
WINDOW = "DeFrost"

#color dif
ELSA_COLOR = (255, 180, 0)
ANNA_COLOR = (0, 180, 255)

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
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
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


class QuitApp(Exception):
    pass


class Dance:
    def __init__(self, video):
        self.video = video
        self.name = video.stem
        self.audio = video.with_suffix(".mp3")
        self.audio_slow = video.with_name(video.stem + "_slow.mp3")


def load_dances():
    dances = [Dance(p) for p in sorted(DANCE_DIR.glob("dance*.mp4"))]
    if not dances:
        raise FileNotFoundError(f"No dance*.mp4 videos found in {DANCE_DIR}")
    return dances


AUDIO_OK = False


def init_audio():
    global AUDIO_OK
    if pygame is None:
        print("pygame not installed - running without audio")
        return
    try:
        pygame.mixer.init()
        AUDIO_OK = True
    except Exception as exc:
        print("Audio disabled:", exc)


def play_audio(path):
    if AUDIO_OK and path.exists():
        try:
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.play()
        except Exception as exc:
            print("Could not play audio:", exc)


def stop_audio():
    if AUDIO_OK:
        pygame.mixer.music.stop()


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
    #elsa = left side, anna = right side
    slots = [None, None]
    for lms in sorted(poses, key=center_x):
        side = 0 if center_x(lms) < 0.5 else 1
        if slots[side] is None:
            slots[side] = lms
    return slots


def load_reference(dance, detector_factory):
    cache = dance.video.with_name(dance.name + "_ref.npy")
    #only analyze the video once, after that we reuse the saved file
    if cache.exists():
        return np.load(cache)

    print(f"Analyzing {dance.name} for the first time (cached afterwards)...")
    cap = cv2.VideoCapture(str(dance.video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    max_frames = int(CLIP_SECONDS * fps)
    det = detector_factory()
    rows = []
    for i in range(max_frames):
        ok, frame = cap.read()
        if not ok:
            break
        poses = det.detect(frame, int(i * 1000 / fps))
        rows.append(pose_vector(poses[0], w, h) if poses
                    else np.full(len(SEGMENTS), np.nan))
        if i % 60 == 0:
            print(f"  {i}/{max_frames}")
    cap.release()
    det.close()
    arr = np.array(rows)
    np.save(cache, arr)
    return arr


def ref_candidates(ref, idx):
    out = []
    for o in TIME_SLACK_FRAMES:
        j = idx + o
        if 0 <= j < len(ref) and not np.all(np.isnan(ref[j])):
            out.append(ref[j])
    return out


def stars_for(acc):
    return sum(acc >= t for t in STAR_THRESHOLDS)


class ClipPlayer:
    def __init__(self, path, speed=1.0):
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open video: {path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.speed = speed
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


def crop_to_aspect(img, aspect):
    h, w = img.shape[:2]
    if w / h > aspect:
        new_w = int(h * aspect)
        x0 = (w - new_w) // 2
        return img[:, x0:x0 + new_w]
    new_h = int(w / aspect)
    y0 = (h - new_h) // 2
    return img[y0:y0 + new_h]


def fit_cover(img, w, h):
    return cv2.resize(crop_to_aspect(img, w / h), (w, h))


def fit_contain(img, w, h):
    canvas = np.zeros((h, w, 3), np.uint8)
    ih, iw = img.shape[:2]
    s = min(w / iw, h / ih)
    nw, nh = int(iw * s), int(ih * s)
    small = cv2.resize(img, (nw, nh))
    x0, y0 = (w - nw) // 2, (h - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = small
    return canvas


def prep_cam(frame):
    #mirror cams
    return crop_to_aspect(cv2.flip(frame, 1), CAM_W / H)


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


CRACKS = [
    [(0.50, 0.00), (0.44, 0.18), (0.54, 0.32)],
    [(0.00, 0.35), (0.20, 0.42), (0.30, 0.60)],
    [(1.00, 0.25), (0.80, 0.38), (0.70, 0.55)],
    [(0.54, 0.32), (0.62, 0.50), (0.50, 0.68)],
    [(0.30, 0.60), (0.20, 0.80), (0.28, 1.00)],
    [(0.70, 0.55), (0.82, 0.76), (0.74, 1.00)],
    [(0.44, 0.18), (0.30, 0.15), (0.18, 0.05)],
    [(0.50, 0.68), (0.46, 0.84), (0.52, 1.00)],
]


def draw_olaf(img, cx, cy, s=1.0, ice_level=0.0, frozen=True, cheer=False):
    white, dark = (255, 255, 255), (40, 40, 40)

    def P(dx, dy):
        return int(cx + dx * s), int(cy + dy * s)

    arm_y = -30 if cheer else 12
    cv2.line(img, P(-18, 5), P(-52, arm_y), (30, 60, 110), 3)
    cv2.line(img, P(18, 5), P(52, arm_y), (30, 60, 110), 3)
    for dy, r in [(55, 28), (10, 22), (-32, 18)]:
        cv2.circle(img, P(0, dy), int(r * s), white, -1)
        cv2.circle(img, P(0, dy), int(r * s), dark, 2)
    cv2.circle(img, P(-6, -38), max(1, int(2 * s)), dark, -1)
    cv2.circle(img, P(6, -38), max(1, int(2 * s)), dark, -1)
    cv2.fillPoly(img, [np.array([P(0, -34), P(0, -29), P(18, -31)], np.int32)], (0, 140, 255))
    cv2.circle(img, P(0, 5), max(1, int(2 * s)), dark, -1)
    cv2.circle(img, P(0, 18), max(1, int(2 * s)), dark, -1)
    cv2.ellipse(img, P(0, -26), (int(7 * s), int(5 * s)), 0, 0, 180, dark, 2)

    if frozen:
        x1, y1 = P(-65, -62)
        x2, y2 = P(65, 95)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(img.shape[1], x2), min(img.shape[0], y2)
        roi = img[y1:y2, x1:x2]
        ice = np.full_like(roi, (255, 225, 170))
        img[y1:y2, x1:x2] = cv2.addWeighted(roi, 0.55, ice, 0.45, 0)
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 240, 200), 2)
        n = int(round(min(max(ice_level, 0), 1) * len(CRACKS)))
        for crack in CRACKS[:n]:
            pts = np.array([(int(x1 + px * (x2 - x1)), int(y1 + py * (y2 - y1)))
                            for px, py in crack], np.int32)
            cv2.polylines(img, [pts], False, (90, 60, 20), 2, cv2.LINE_AA)


def make_panel(title, subtitle, ice_level, stars, time_left=None):
    panel = np.full((H, PANEL_W, 3), (70, 45, 25), np.uint8)
    text_center(panel, title, 38, 0.95, (255, 255, 255), 2)
    text_center(panel, subtitle[:22], 64, 0.5, (220, 220, 220), 1)
    draw_olaf(panel, PANEL_W // 2, 215, 1.0, ice_level, frozen=True)
    draw_star_row(panel, PANEL_W // 2, 345, stars)
    if time_left is not None:
        text_center(panel, f"{max(time_left, 0):.0f}s", 410, 0.9, (255, 255, 255), 2)
    return panel


def compose(ref_frame, cam_disp, panel):
    if ref_frame is None:
        ref = np.zeros((H, REF_W, 3), np.uint8)
    else:
        ref = fit_contain(ref_frame, REF_W, H)
    return np.hstack([ref, cam_disp, panel])


def show(img, delay=1):
    cv2.imshow(WINDOW, img)
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


def wait_for_key(camera, lines, keys):
    while True:
        cam = fit_cover(prep_cam(read_cam(camera)), WINDOW_W, H)
        cam = (cam * 0.45).astype(np.uint8)
        y = H // 2 - 20 * (len(lines) - 1)
        for i, (text, scale, color) in enumerate(lines):
            text_center(cam, text, y + i * 45, scale, color, 2)
        k = show(cam)
        ch = chr(k).lower() if k < 128 else ""
        if ch in keys:
            return ch


def text_input(camera, prompt, default, max_len=16):
    text = ""
    while True:
        cam = (fit_cover(prep_cam(read_cam(camera)), WINDOW_W, H) * 0.45).astype(np.uint8)
        text_center(cam, prompt, H // 2 - 40, 1.0, (255, 255, 255))
        shown = (text or "") + ("_" if int(time.time() * 2) % 2 == 0 else " ")
        text_center(cam, shown, H // 2 + 20, 1.3, (0, 215, 255), 3)
        text_center(cam, f"Press ENTER (empty = \"{default}\")", H - 40, 0.6, (200, 200, 200), 1)
        cv2.imshow(WINDOW, cam)
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


def countdown(camera, ref_first, title, seconds=3):
    t0 = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        left = seconds - int(elapsed)
        if left <= 0:
            break
        cam = prep_cam(read_cam(camera))
        disp = cv2.resize(cam, (CAM_W, H))
        text_center(disp, str(left), H // 2 + 50, 5, (255, 255, 255), 8)
        text_center(disp, title, 50, 1.0, (255, 255, 255), 2)
        panel = make_panel("GET READY", "", 0, 0)
        show(compose(ref_first, disp, panel))
    for _ in range(10):
        cam = prep_cam(read_cam(camera))
        disp = cv2.resize(cam, (CAM_W, H))
        text_center(disp, "GO!", H // 2 + 40, 4, (0, 255, 0), 7)
        show(compose(ref_first, disp, make_panel("GO!", "", 0, 0)))


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


def leaderboard_panel(entries, current_pair, new_best, freed):
    w = WINDOW_W - CAM_W
    p = np.full((H, w, 3), (70, 45, 25), np.uint8)
    text_center(p, "LEADERBOARD", 38, 0.95, (255, 255, 255), 2)
    ranked = best_per_pair(entries)
    for i, e in enumerate(ranked[:5]):
        y = 85 + i * 42
        mine = e["pair"].lower() == current_pair.lower()
        color = (0, 215, 255) if mine else (255, 255, 255)
        text_left(p, f"{i + 1}. {e['pair'][:14]}", 15, y, 0.7, color)
        text_left(p, f"{e['accuracy'] * 100:.0f}%", w - 85, y, 0.7, color)
    rank = next((i + 1 for i, e in enumerate(ranked) if e["pair"].lower() == current_pair.lower()), None)
    if rank and rank > 5:
        text_left(p, f"You: #{rank} ({ranked[rank - 1]['accuracy'] * 100:.0f}%)", 15, 310, 0.65, (0, 215, 255))
    if new_best:
        text_center(p, "NEW PERSONAL BEST!", 340, 0.7, (0, 255, 0), 2)
    keys = "SPACE: next pair"
    if freed:
        keys += "   N: another dance"
    text_center(p, keys, H - 30, 0.55, (220, 220, 220), 1)
    return p


def spawn_confetti(n, w):
    colors = [(0, 215, 255), (255, 180, 0), (180, 105, 255), (0, 255, 150), (255, 255, 255)]
    return [{"x": random.uniform(0, w), "y": random.uniform(-40, 0),
             "vx": random.uniform(-2, 2), "vy": random.uniform(2, 6),
             "c": random.choice(colors), "s": random.randint(4, 9), "kind": "conf"}
            for _ in range(n)]


def spawn_shards(cx, cy, n=40):
    out = []
    for _ in range(n):
        ang = random.uniform(0, 2 * math.pi)
        sp = random.uniform(3, 12)
        out.append({"x": cx, "y": cy, "vx": math.cos(ang) * sp, "vy": math.sin(ang) * sp - 3,
                    "c": (255, 235, 200), "s": random.randint(6, 14), "kind": "shard"})
    return out


def step_particles(img, parts):
    h, w = img.shape[:2]
    alive = []
    for p in parts:
        p["x"] += p["vx"]
        p["y"] += p["vy"]
        p["vy"] += 0.25
        if p["y"] > h + 20 or p["x"] < -20 or p["x"] > w + 20:
            continue
        x, y, s = int(p["x"]), int(p["y"]), p["s"]
        if p["kind"] == "conf":
            cv2.rectangle(img, (x, y), (x + s, y + s // 2 + 2), p["c"], -1)
        else:
            tri = np.array([(x, y - s), (x - s, y + s), (x + s, y + s)], np.int32)
            cv2.fillPoly(img, [tri], p["c"])
            cv2.polylines(img, [tri], True, (200, 160, 90), 1)
        alive.append(p)
    return alive


def play_learning_clip(camera, dance, label, speed, audio):
    player = ClipPlayer(dance.video, speed)
    play_audio(audio)
    player.start_clock()
    while player.advance():
        cam = prep_cam(read_cam(camera))
        #resize both frames
        disp = cv2.resize(cam, (CAM_W, H))
        text_left(disp, "YOUR CAMERA - copy the dancer!", 20, 35, 0.8)
        # split webcam and dance vid
        panel = make_panel("LEARN", label, 0, 0, CLIP_SECONDS - player.elapsed())
        show(compose(player.frame, disp, panel))
    stop_audio()
    player.release()


def learn_phase(camera, dance):
    ref0 = first_frame(dance)
    #first two viewings are mandatory
    rounds = [("SLOW", SLOW_SPEED, dance.audio_slow)] * SLOW_ROUNDS
    #next two viewings are fast
    rounds += [("NORMAL", 1.0, dance.audio)] * NORMAL_ROUNDS
    for i, (label, speed, audio) in enumerate(rounds, 1):
        countdown(camera, ref0, f"Learn it: {label} ({i}/{len(rounds)})")
        play_learning_clip(camera, dance, f"{label} {i}/{len(rounds)}", speed, audio)

    #optional replays
    replays = 0
    while True:
        left = MAX_EXTRA_REPLAYS - replays
        lines = [("Ready for the challenge?", 1.1, (255, 255, 255)),
                 ("Elsa = LEFT tape    Anna = RIGHT tape", 0.8, (0, 215, 255)),
                 ("SPACE = start!", 0.9, (0, 255, 0))]
        keys = {" "}
        if left > 0:
            lines.append((f"R = replay ({left} left)", 0.7, (220, 220, 220)))
            keys.add("r")
        if wait_for_key(camera, lines, keys) != "r":
            return
        replays += 1
        countdown(camera, ref0, "Replay")
        play_learning_clip(camera, dance, f"REPLAY {replays}", 1.0, dance.audio)


def run_challenge(camera, detector, dance, ref):
    ref0 = first_frame(dance)
    countdown(camera, ref0, "CHALLENGE!")

    player = ClipPlayer(dance.video, 1.0)
    play_audio(dance.audio)
    player.start_clock()

    sums = np.zeros(2)
    scored = 0
    ema, best_ema, best_frame = 0.0, -1.0, None
    names = ("ELSA", "ANNA")
    colors = (ELSA_COLOR, ANNA_COLOR)

    while player.advance():
        #mirror
        cam = prep_cam(read_cam(camera))
        slots = assign_players(detector.detect(cam))
        cands = ref_candidates(ref, player.index)

        #score elsa and anna separately
        sims = [0.0, 0.0]
        for s in (0, 1):
            if slots[s] is not None and cands:
                vec = pose_vector(slots[s], cam.shape[1], cam.shape[0])
                sims[s] = pose_similarity(vec, cands)
        if cands:
            sums += sims
            scored += 1

        disp = cv2.resize(cam, (CAM_W, H))
        for s in (0, 1):
            if slots[s] is not None:
                draw_skeleton(disp, slots[s], colors[s])

        #keep track of the best moment for the photo
        ema = 0.9 * ema + 0.1 * (sims[0] + sims[1]) / 2
        if scored > 20 and slots[0] is not None and slots[1] is not None and ema > best_ema:
            best_ema = ema
            best_frame = disp.copy()

        live = sums / max(scored, 1)
        pair_live = float(live.mean())
        cv2.line(disp, (CAM_W // 2, 0), (CAM_W // 2, H), (255, 255, 255), 2)
        text_left(disp, f"ELSA {live[0] * 100:.0f}%", 20, 35, 0.9, ELSA_COLOR)
        text_left(disp, f"ANNA {live[1] * 100:.0f}%", CAM_W // 2 + 20, 35, 0.9, ANNA_COLOR)
        for s in (0, 1):
            if slots[s] is None:
                text_center(disp, f"{names[s]}: step onto your tape!", H - 40, 0.6,
                            (0, 0, 255), 2, x_center=CAM_W // 4 + s * CAM_W // 2)
        prog = int(CAM_W * min(player.elapsed() / CLIP_SECONDS, 1))
        cv2.rectangle(disp, (0, H - 8), (prog, H), (0, 215, 255), -1)

        panel = make_panel("DANCE!", f"{pair_live * 100:.0f}% match",
                           pair_live / 0.9, stars_for(pair_live),
                           CLIP_SECONDS - player.elapsed())
        show(compose(player.frame, disp, panel))

    stop_audio()
    player.release()
    acc = sums / max(scored, 1)
    if best_frame is None:
        best_frame = np.zeros((H, CAM_W, 3), np.uint8)
    return acc, best_frame


def make_photo(best_frame, pair_name, accs, pair_acc, stars, freed):
    img = best_frame.copy()
    cv2.line(img, (CAM_W // 2, 0), (CAM_W // 2, H), (255, 255, 255), 1)
    banner = img[H - 110:H]
    img[H - 110:H] = cv2.addWeighted(banner, 0.35, np.zeros_like(banner), 0.65, 0)
    title = "OLAF IS FREE!" if freed else "Olaf is still frozen..."
    text_center(img, title, 42, 1.2, (0, 255, 0) if freed else (255, 200, 120), 3)
    text_center(img, pair_name, 78, 0.7, (255, 255, 255), 2)
    text_center(img, f"ELSA {accs[0] * 100:.0f}%", H - 60, 0.9, ELSA_COLOR, 2, x_center=CAM_W // 6)
    text_center(img, f"ANNA {accs[1] * 100:.0f}%", H - 60, 0.9, ANNA_COLOR, 2, x_center=CAM_W - CAM_W // 6)
    text_center(img, f"{pair_acc * 100:.0f}%", H - 60, 1.3, (255, 255, 255), 3)
    draw_star_row(img, CAM_W // 2, H - 22, stars, r=12, gap=30)
    #save the photo with the scores on it
    PHOTO_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", pair_name)[:20] or "pair"
    path = PHOTO_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}.jpg"
    cv2.imwrite(str(path), img)
    print("Saved photo:", path)
    return img


def results_screen(photo, entries, pair_name, new_best, pair_acc, freed):
    right = leaderboard_panel(entries, pair_name, new_best, freed)
    #ice shatters when olaf is free
    particles = spawn_shards(CAM_W - 85, 120) if freed else []
    t0 = time.monotonic()
    while True:
        left = photo.copy()
        if freed:
            draw_olaf(left, CAM_W - 85, 130, 1.0, frozen=False, cheer=True)
            #confetti keeps falling for a few secs
            if time.monotonic() - t0 < 6:
                particles += spawn_confetti(5, CAM_W)
            particles = step_particles(left, particles)
        else:
            draw_olaf(left, CAM_W - 85, 130, 1.0, ice_level=pair_acc / 0.9, frozen=True)
        k = show(np.hstack([left, right]), delay=20)
        ch = chr(k).lower() if k < 128 else ""
        if ch == " ":
            return "space"
        if ch == "n" and freed:
            return "n"


def play_pair(camera, detector, dances, ref_cache, pair_name):
    dance = random.choice(dances)
    while True:
        learn_phase(camera, dance)
        if dance.name not in ref_cache:
            ref_cache[dance.name] = load_reference(dance, ref_cache["_factory"])
        accs, best_frame = run_challenge(camera, detector, dance, ref_cache[dance.name])

        pair_acc = float(accs.mean())
        stars = stars_for(pair_acc)
        freed = stars >= FREE_OLAF_STARS
        print(f"{pair_name}: Elsa {accs[0]:.0%}  Anna {accs[1]:.0%}  "
              f"pair {pair_acc:.0%}  stars {stars}  freed={freed}")

        photo = make_photo(best_frame, pair_name, accs, pair_acc, stars, freed)
        entries, new_best = add_leaderboard_entry(pair_name, dance.name, pair_acc, stars)
        choice = results_screen(photo, entries, pair_name, new_best, pair_acc, freed)
        if choice == "n" and freed:
            others = [d for d in dances if d.name != dance.name] or dances
            dance = random.choice(others)
            continue
        return


def main():
    model_path = find_model()
    print("Model:", model_path)
    dances = load_dances()
    print("Dances:", [d.name for d in dances])

    init_audio()
    camera = cv2.VideoCapture(CAMERA_INDEX)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    if not camera.isOpened():
        raise RuntimeError("Could not open webcam")

    detector = Detector(model_path, num_poses=2)
    ref_cache = {"_factory": lambda: Detector(model_path, num_poses=1)}
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)

    try:
        while True:
            #waits here till the next pair steps up
            wait_for_key(camera, [
                ("DeFrost", 2.0, (255, 220, 150)),
                ("Free Olaf! Dance together to crack the ice", 0.8, (255, 255, 255)),
                ("Elsa: LEFT tape   |   Anna: RIGHT tape", 0.8, (0, 215, 255)),
                ("Press SPACE to start", 0.9, (0, 255, 0)),
            ], {" "})
            pair_name = text_input(camera, "Enter your pair name:", "Elsa & Anna")
            play_pair(camera, detector, dances, ref_cache, pair_name)
    except QuitApp:
        pass
    finally:
        camera.release()
        detector.close()
        stop_audio()
        if AUDIO_OK:
            pygame.mixer.quit()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
