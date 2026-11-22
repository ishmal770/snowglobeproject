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
            fps = video.get(cv2.CAP_PROP_FPS) or 30
            frame_count = video.get(cv2.CAP_PROP_FRAME_COUNT)
            self.fps[name] = fps
            video_seconds = frame_count / fps if frame_count > 0 else td.CLIP_SECONDS
            audio_seconds = None
            if dance.audio and dance.audio.suffix.lower() == ".wav" and dance.audio.is_file():
                try:
                    with wave.open(str(dance.audio), "rb") as audio_file:
                        audio_seconds = audio_file.getnframes() / audio_file.getframerate()
                except (OSError, wave.Error, ZeroDivisionError):
                    audio_seconds = None
            video.release()

            source_seconds = min(td.CLIP_SECONDS, video_seconds,
                                 audio_seconds if audio_seconds and audio_seconds > 0 else video_seconds)
            self.dance_seconds[name] = source_seconds
            self.round_seconds[name] = max(1.0, source_seconds - 1.0)

            self.ref[name] = td.load_reference(
                dance, factory,
                lambda f, name=name: print(f"\rAnalyzing {name}: {f * 100:3.0f}%", end="", flush=True))
        print()

        self.async_det = td.AsyncDetector(td.Detector(model_path, num_poses=2))
        td.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        self.initialized = True
        self.start_thread()

    def start_thread(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def close(self):
        self.running = False
        time.sleep(0.2)
        if self.async_det is not None:
            self.async_det.close()

    def submit_frame(self, frame):
        if not self.initialized or self.async_det is None:
            return False
        cam = cv2.flip(frame, 1)
        with self.frame_lock:
            self.latest_cam = cam.copy()
        self.async_det.submit(cam)
        return True

    def loop(self):
        while self.running:
            try:
                self.tick()
            except Exception as exc:  # never let the game loop thread die
                import traceback
                traceback.print_exc()
                print("loop error (recovering):", exc)
                with self.lock:
                    if self.phase in ("countdown", "challenge"):
                        self.phase = "idle"
            time.sleep(1 / STATE_HZ)

    def tick(self):
        with self.frame_lock:
            cam = None if self.latest_cam is None else self.latest_cam.copy()
        if cam is not None:
            self.update(cam, time.monotonic())
        else:
            now = time.monotonic()
            with self.lock:
                if self.phase == "countdown" and now >= self.countdown_end:
                    self.phase = "challenge"
                    self.t0 = now
                    self.clear_round()
                    if self.async_det is not None:
                        self.async_det.reset()
                        _, self.last_id = self.async_det.latest()
                elif (self.phase == "challenge" and
                      now - self.t0 >= self.round_seconds.get(self.dance, max(td.CLIP_SECONDS - 1.0, 1.0))):
                    self.finish()

    def update(self, cam, now):
        poses, rid = self.async_det.latest()
        new_result = poses is not None and rid != self.last_id
        with self.lock:
            if new_result:
                self.last_id = rid
                self.slots = td.assign_players(poses)
                for s in (0, 1):
                    lms = self.slots[s]
                    self.present[s] = lms is not None
                    self.landmarks[s] = None if lms is None else [
                        [round(l.x, 3), round(l.y, 3), round(l.visibility if l.visibility is not None else 1.0, 2)]
                        for l in lms]

            if self.phase == "countdown" and now >= self.countdown_end:
                self.phase = "challenge"
                self.t0 = now
                self.clear_round()
                self.async_det.reset()
                _, self.last_id = self.async_det.latest()
                new_result = False

            if self.phase == "challenge":
                elapsed = now - self.t0
                if elapsed >= self.round_seconds.get(self.dance, max(td.CLIP_SECONDS - 1.0, 1.0)):
                    self.finish()
                elif new_result:
                    self.score(cam, elapsed)

    def score(self, cam, elapsed):
        idx = int(elapsed * self.fps[self.dance])
        #solo dance: both copy the same dancer, duo dance: each copies the dancer on their side
        cands = [td.ref_candidates(self.ref[self.dance], idx, s) for s in (0, 1)]
        sims = [0.0, 0.0]
        #score both players separately
        for s in (0, 1):
            if self.slots[s] is not None and cands[s]:
                vec = td.pose_vector(self.slots[s], cam.shape[1], cam.shape[0])
                sims[s] = td.pose_similarity(vec, cands[s])
            if cands[s]:
                self.sums[s] += sims[s]
                self.scored[s] += 1
        for s in (0, 1):
            self.inst[s] = 0.6 * self.inst[s] + 0.4 * sims[s]
            #doing well on this comparison = a star hit
            if cands[s] and sims[s] >= td.GOOD_SCORE:
                self.hits[s] += 1

        #keep track of the best moment for the photo
        self.ema = 0.8 * self.ema + 0.2 * (sims[0] + sims[1]) / 2
        if (self.scored.max() > 10 and self.slots[0] is not None and self.slots[1] is not None
                and self.ema > self.best_ema):
            self.best_ema = self.ema
            disp = td.to_display(cam)
            colors = (td.P1_COLOR, td.P2_COLOR)
            for s in (0, 1):
                td.draw_skeleton(disp, self.slots[s], colors[s])
            self.best_frame = disp

    def finish(self):
        accs = self.sums / np.maximum(self.scored, 1)
        pair_acc = float(accs.mean())
        stars = int(td.stars_for(pair_acc))
        passed = stars >= td.PASS_STARS

        best = self.best_frame
        if best is None:
            best = np.zeros((td.H, td.CAM_W, 3), np.uint8)
        try:
            _, photo_path = td.make_photo(best, self.pair, accs, pair_acc, stars, passed)
        except Exception as exc:
            print("make_photo failed:", exc)
            photo_path = "none.jpg"
        try:
            entries, new_best = td.add_leaderboard_entry(self.pair, self.dance, pair_acc, stars)
        except Exception as exc:
            print("leaderboard failed:", exc)
            entries, new_best = [], False
        ranked = td.best_per_pair(entries)
        rank = next((i + 1 for i, e in enumerate(ranked) if e["pair"].lower() == self.pair.lower()), None)

        self.result = {
            "pair": self.pair,
            "dance": self.dance,
            "accuracies": [round(float(a), 3) for a in accs],
            "pair_accuracy": round(pair_acc, 3),
            "stars": stars,
            "passed": bool(passed),
            "new_personal_best": bool(new_best),
            "star_hits": list(self.hits),
            "photo_url": "/photos/" + Path(photo_path).name,
            "rank": rank,
            "leaderboard": ranked[:5],
        }
        self.phase = "results"
        print(f"{self.pair}: {accs[0]:.0%} / {accs[1]:.0%}  pair {pair_acc:.0%}  stars {stars}  passed={passed}")

    def start(self, dance_name, pair, countdown=None):
        with self.lock:
            # a new start always replaces any old/stuck round (was causing 409)
            if dance_name is None:
                dance_name = random.choice(list(self.dances))
            if dance_name not in self.dances:
                return False, f"unknown dance: {dance_name}"
            self.dance = dance_name
            self.pair = pair or "Pair"
            self.clear_round()
            self.phase = "countdown"
            seconds = COUNTDOWN_SECONDS if countdown is None else max(0.0, min(float(countdown), 10.0))
            self.countdown_end = time.monotonic() + seconds
            return True, "ok"

    def reset(self):
        with self.lock:
            self.phase = "idle"
            self.dance = None
            self.clear_round()

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            if self.phase == "challenge":
                elapsed = max(now - self.t0, 0.0)
            elif self.phase == "results":
                elapsed = self.round_seconds.get(self.dance, max(td.CLIP_SECONDS - 1.0, 1.0))
            else:
                elapsed = 0.0
            round_length = self.round_seconds.get(self.dance, max(td.CLIP_SECONDS - 1.0, 1.0))
            live = self.sums / np.maximum(self.scored, 1)
            players = []
            for s in (0, 1):
                players.append({
                    "id": s,
                    "label": td.PLAYER_LABELS[s],
                    "present": bool(self.present[s]),
                    "live": round(float(live[s]), 3),
                    "now": round(float(self.inst[s]), 3),
                    "stars": int(td.stars_for(float(live[s]))),
                    "hits": int(self.hits[s]),
                    "landmarks": self.landmarks[s],
                })
            pair_live = float(live.mean())
            return {
                "phase": self.phase,
                "dance": self.dance,
                "pair": self.pair,
                "countdown": (max(math.ceil(self.countdown_end - now), 0)
                              if self.phase == "countdown" else None),
                "elapsed": round(elapsed, 2),
                "time_left": round(max(round_length - elapsed, 0.0), 2),
                "clip_seconds": round_length,
                "good_score": td.GOOD_SCORE,
                "players": players,
                "pair_live": round(pair_live, 3),
                "pair_stars": int(td.stars_for(pair_live)),
                "result": self.result,
            }


server = GameServer()


@app.after_request
def add_cors(resp):
    origins = [value.strip() for value in os.environ.get("FRONTEND_ORIGINS", "").split(",") if value.strip()]
    origin = request.headers.get("Origin")
    if not origins or (origin and origin in origins):
        resp.headers["Access-Control-Allow-Origin"] = origin if origins and origin else "*"
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.get("/")
def index():
    folder = frontend_dir()
    if folder is not None:
        return send_from_directory(folder, "index.html")
    return send_file(SERVER_DIR / "demo.html")


@app.post("/api/frame")
def camera_frame():
    raw = request.get_data(cache=False)
    if not raw or len(raw) > 1_500_000:
        return jsonify({"error": "frame must be a JPEG under 1.5 MB"}), 413
    frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"error": "invalid JPEG frame"}), 400
    if not server.submit_frame(frame):
        return jsonify({"error": "game server is still starting"}), 503
    return "", 204


@app.get("/api/events")
def events():
    def gen():
        while True:
            yield f"data: {json.dumps(server.snapshot())}\n\n"
            time.sleep(1 / STATE_HZ)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/state")
def state():
    return jsonify(server.snapshot())


@app.get("/api/dances")
def dances():
    return jsonify({
        "clip_seconds": td.CLIP_SECONDS,
        "slow_speed": td.SLOW_SPEED,
        "countdown_seconds": COUNTDOWN_SECONDS,
        "dances": [{
            "id": name,
            "video_url": "/dances/" + d.video.name,
            "duration_seconds": server.dance_seconds.get(name, td.CLIP_SECONDS),
            "round_seconds": server.round_seconds.get(name, max(td.CLIP_SECONDS - 1.0, 1.0)),
            "audio_url": "/dances/" + d.audio.name if d.audio and d.audio.is_file() else None,
            "slow_audio_url": "/dances/" + d.audio_slow.name if d.audio_slow and d.audio_slow.is_file() else None,
            "fps": server.fps.get(name),
            "duo": d.duo,
        } for name, d in server.dances.items()],
    })


@app.get("/api/leaderboard")
def leaderboard():
    limit = request.args.get("limit", 5, type=int)
    return jsonify(td.best_per_pair(td.load_leaderboard())[:limit])


@app.post("/api/challenge/start")
def challenge_start():
    data = request.get_json(silent=True) or {}
    pair = str(data.get("pair") or "Pair").strip()[:20]
    try:
        countdown = None if data.get("countdown") is None else float(data["countdown"])
    except (TypeError, ValueError):
        return jsonify({"error": "countdown must be a number"}), 400
    ok, msg = server.start(data.get("dance"), pair, countdown)
    if not ok:
        return jsonify({"error": msg}), 409 if "running" in msg else 400
    return jsonify(server.snapshot())


@app.post("/api/challenge/cancel")
def challenge_cancel():
    server.reset()
    return jsonify(server.snapshot())


@app.post("/api/reset")
def reset():
    server.reset()
    return jsonify(server.snapshot())


@app.get("/dances/<path:filename>")
def dance_file(filename):
    if Path(filename).suffix.lower() not in (".mp4", ".mp3", ".wav"):
        abort(404)
    return send_from_directory(td.DANCE_DIR, filename)


@app.get("/photos/<path:filename>")
def photo_file(filename):
    return send_from_directory(td.PHOTO_DIR, filename)


@app.get("/<path:filename>")
def frontend_file(filename):
    folder = frontend_dir()
    if folder is not None:
        if Path(filename).suffix.lower() in FRONTEND_TYPES and (folder / filename).is_file():
            return send_from_directory(folder, filename)
        #react-router style pages: unknown paths without a file extension get index.html
        if "." not in Path(filename).name:
            return send_from_directory(folder, "index.html")
    abort(404)


def main():
    server.setup()
    atexit.register(server.close)
    where = frontend_dir() or "built-in demo page"
    print(f"Server running: http://{HOST}:{PORT}  (frontend: {where})")
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()