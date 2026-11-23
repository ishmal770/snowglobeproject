"""DeFrost! game server.

The browser sends webcam frames to /api/frame, this server finds the two
dancers with MediaPipe, scores them against the dance video, and streams the
live game state back over Server-Sent Events (/api/events).

Heavy start-up work (audio extraction, loading the pose model, analysing the
dance videos) runs in a background thread so the web server answers requests
(and passes Render's health check) right away.
"""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from flask import (Flask, Response, abort, jsonify, request, send_file,
                   send_from_directory, stream_with_context)

import tiktokdance as td

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "5000"))
STATE_HZ = 12
COUNTDOWN_SECONDS = 3
STALE_SECONDS = 1.5          # no new detection for this long = nobody is "present"
MAX_SSE_CLIENTS = 24
# The player sees the dance a little after the server's clock starts (network +
# media start-up) and their camera frame arrives a little after they moved.
# Compare against the dance video this many seconds in the past.
SCORE_DELAY_SECONDS = float(os.environ.get("SCORE_DELAY_SECONDS", "0.4"))

SERVER_DIR = Path(__file__).resolve().parent

# only these kinds of files are served to the browser (so .py files, the model and json never get exposed)
FRONTEND_TYPES = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
                  ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".ttf"}

# If set (e.g. https://your-site.vercel.app/dances) the dance videos / music are loaded from
# that static host instead of this server, which is much faster on a small Render instance.
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "").strip().rstrip("/")

ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]


def frontend_dir():
    # looks for index.html in a folder called "frontend", then next to this file. FRONTEND_DIR env var overrides
    env = os.environ.get("FRONTEND_DIR")
    for folder in ([Path(env)] if env else [SERVER_DIR / "frontend", SERVER_DIR]):
        if (folder / "index.html").is_file():
            return folder
    return None


def pick_data_dir():
    # where the leaderboard and result photos are saved. Falls back to the code folder if
    # SNOWGLOBE_DATA_DIR doesn't exist / isn't writable (e.g. no persistent disk on Render's free plan)
    wanted = os.environ.get("SNOWGLOBE_DATA_DIR")
    for folder in ([Path(wanted)] if wanted else []) + [SERVER_DIR]:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return folder
        except Exception as exc:
            print(f"Data dir {folder} is not usable: {exc}")
    return SERVER_DIR


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1_500_000


# ---------------------------------------------------------------- CORS
@app.after_request
def add_cors(response):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    allow = None
    if not ALLOWED_ORIGINS or "*" in ALLOWED_ORIGINS:
        allow = "*"
    elif origin in ALLOWED_ORIGINS or re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin):
        allow = origin
        response.headers.add("Vary", "Origin")
    if allow:
        response.headers["Access-Control-Allow-Origin"] = allow
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Expose-Headers"] = "Content-Range, Accept-Ranges, Content-Length"
        response.headers["Access-Control-Max-Age"] = "600"
    return response


class GameServer:

    def __init__(self):
        self.lock = threading.RLock()
        self.thread_lock = threading.Lock()
        self.running = True
        self.initialized = False

        self.async_det = None
        self.model_path = None
        self.dances = {}
        self.fps = {}
        self.dance_seconds = {}
        self.round_seconds = {}
        self.ref = {}

        self.audio_locks = {}
        self.wanted = []                # dances the player picked: analysed first
        self.ready = False
        self.stage = "starting"
        self.progress = 0.0
        self.error = None
        self.sse_clients = 0

        self.phase = "idle"
        self.dance = None
        self.pair = ""
        self.t0 = 0.0
        self.last_frame = None          # latest mirrored webcam frame
        self.frame_size = (640, 480)
        self.last_result_time = 0.0
        self.slots = [None, None]
        self.landmarks = [None, None]
        self.present = [False, False]
        self.clear_round()

    # ------------------------------------------------------------ round state
    def clear_round(self):
        self.sums = np.zeros(2)
        self.scored = np.zeros(2)
        self.hits = [0, 0]
        self.inst = [0.0, 0.0]
        self.ema = 0.0
        self.best_ema = -1.0
        self.best_frame = None
        self.result = None

    # ------------------------------------------------------------ start-up
    def setup(self):
        """Fast: lists the dances and starts the background workers. Never blocks for long."""
        with self.thread_lock:
            if self.initialized:
                return
            self.initialized = True

        data_dir = pick_data_dir()
        td.LEADERBOARD_FILE = data_dir / "leaderboard.json"
        td.PHOTO_DIR = data_dir / "photos"
        td.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        seed = SERVER_DIR / "leaderboard.json"
        if not td.LEADERBOARD_FILE.exists() and seed.exists() and seed != td.LEADERBOARD_FILE:
            shutil.copyfile(seed, td.LEADERBOARD_FILE)
        print("Data dir:", data_dir)

        self.dances = {d.name: d for d in td.load_dances()}
        for name, dance in self.dances.items():
            cap = cv2.VideoCapture(str(dance.video))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            cap.release()
            seconds = frames / fps if frames > 0 else float(td.CLIP_SECONDS)
            self.fps[name] = fps
            self.dance_seconds[name] = seconds
            self.round_seconds[name] = min(float(td.CLIP_SECONDS), seconds)

        threading.Thread(target=self._prepare, daemon=True, name="prepare").start()
        threading.Thread(target=self._loop, daemon=True, name="game-loop").start()

    def _prepare(self):
        try:
            self.stage = "finding pose model"
            self.model_path = td.find_model()
            print("Model:", self.model_path)

            # detector first: players can be seen in the practice room as soon as possible
            self.stage = "loading pose detector"
            self.async_det = td.AsyncDetector(td.Detector(self.model_path, num_poses=2))
            print("Pose detector ready")

            factory = lambda n: td.Detector(self.model_path, num_poses=n)
            pending = list(self.dances)
            total = len(pending)
            while pending and self.running:
                # the dance a player just picked goes first
                name = next((n for n in self.wanted if n in pending), pending[0])
                pending.remove(name)
                self.stage = f"analysing {name} ({total - len(pending)}/{total})"
                self.progress = (total - len(pending) - 1) / total
                try:
                    self.ref[name] = td.load_reference(self.dances[name], factory)
                except Exception as exc:
                    print("Could not analyse", name, exc)
            self.progress = 1.0
            self.stage = "ready"
            self.ready = True
            print("Server ready. Dances:", sorted(self.ref))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.error = str(exc)
            self.stage = "failed"

    def want(self, name):
        if name in self.dances and name not in self.wanted:
            self.wanted.append(name)

    def ensure_audio(self, name):
        """Make this one dance's audio files the first time they are asked for."""
        dance = self.dances.get(name)
        if dance is None:
            return
        lock = self.audio_locks.setdefault(name, threading.Lock())
        with lock:
            if getattr(dance, "audio_checked", False):
                return
            try:
                td.prepare_audio(dance)
            except Exception as exc:
                print("Audio failed for", name, exc)
                dance.audio = None
                dance.audio_slow = None
            dance.audio_checked = True

    def close(self):
        self.running = False
        if self.async_det is not None:
            try:
                self.async_det.close()
            except Exception:
                pass

    # ------------------------------------------------------------ frames in
    def submit_frame(self, frame):
        det = self.async_det
        if det is None:
            return False
        mirrored = cv2.flip(frame, 1)       # same mirroring the browser shows the players
        with self.lock:
            self.last_frame = mirrored
            self.frame_size = (mirrored.shape[1], mirrored.shape[0])
        det.submit(mirrored)
        return True

    # ------------------------------------------------------------ game loop
    def _loop(self):
        last_id = 0
        while self.running:
            time.sleep(0.02)
            det = self.async_det
            try:
                with self.lock:
                    if det is not None:
                        poses, rid = det.latest()
                        if poses is not None and rid != last_id:
                            last_id = rid
                            self._process(poses)
                    self._tick_phase()
            except Exception:
                import traceback
                traceback.print_exc()

    def _process(self, poses):
        now = time.monotonic()
        w, h = self.frame_size
        self.last_result_time = now
        self.slots = td.assign_players(poses)
        for s in (0, 1):
            lms = self.slots[s]
            self.present[s] = lms is not None
            self.landmarks[s] = None if lms is None else [
                [round(l.x, 4), round(l.y, 4), round(l.visibility if l.visibility is not None else 1.0, 3)]
                for l in lms]

        if self.phase != "challenge" or self.dance is None:
            return

        name = self.dance.name
        ref = self.ref[name]
        elapsed = max(0.0, now - self.t0 - SCORE_DELAY_SECONDS)
        idx = int(elapsed * self.fps[name])
        sims = [0.0, 0.0]
        for s in (0, 1):
            cands = td.ref_candidates(ref, idx, s)
            if self.slots[s] is not None and cands:
                vec = td.pose_vector(self.slots[s], w, h)
                sims[s] = td.pose_similarity(vec, cands)
            if cands:
                self.sums[s] += sims[s]
                self.scored[s] += 1
                if sims[s] >= td.GOOD_SCORE:
                    self.hits[s] += 1
            self.inst[s] = 0.6 * self.inst[s] + 0.4 * sims[s]

        self.ema = 0.8 * self.ema + 0.2 * (sims[0] + sims[1]) / 2
        if (self.scored.max() > 10 and self.slots[0] is not None and self.slots[1] is not None
                and self.ema > self.best_ema and self.last_frame is not None):
            self.best_ema = self.ema
            self.best_frame = self._render_photo_frame(self.last_frame, self.slots)

    def _render_photo_frame(self, frame, slots):
        td.set_layout(frame)
        disp = td.to_display(frame.copy())
        for s, color in zip((0, 1), (td.P1_COLOR, td.P2_COLOR)):
            if slots[s] is not None:
                td.draw_skeleton(disp, slots[s], color)
        return disp

    def _tick_phase(self):
        now = time.monotonic()
        if self.phase == "countdown" and now >= self.t0:
            self.phase = "challenge"
        if self.phase == "challenge" and self.dance is not None:
            if now - self.t0 >= self.round_seconds[self.dance.name]:
                self._finish()

    def _finish(self):
        accs = self.sums / np.maximum(self.scored, 1)
        pair_acc = float(accs.mean())
        stars = int(td.stars_for(pair_acc))
        passed = stars >= td.PASS_STARS
        pair = self.pair or "Pair"
        print(f"{pair}: P1 {accs[0]:.0%}  P2 {accs[1]:.0%}  pair {pair_acc:.0%}  stars {stars}  passed={passed}")

        best = self.best_frame
        if best is None:
            if self.last_frame is not None:
                best = self._render_photo_frame(self.last_frame, self.slots)
            else:
                best = np.zeros((td.H, td.CAM_W, 3), np.uint8)
        _, photo_path = td.make_photo(best, pair, accs, pair_acc, stars, passed)

        entries, new_best = td.add_leaderboard_entry(pair, self.dance.name, pair_acc, stars)
        ranked = td.best_per_pair(entries)
        rank = next((i + 1 for i, e in enumerate(ranked) if e["pair"].lower() == pair.lower()), None)

        self.result = {
            "pair": pair,
            "dance": self.dance.name,
            "accuracies": [round(float(a), 4) for a in accs],
            "pair_accuracy": round(pair_acc, 4),
            "stars": stars,
            "passed": bool(passed),
            "rank": rank,
            "new_personal_best": bool(new_best),
            "photo_url": "/api/photos/" + photo_path.name,
        }
        self.phase = "results"

    # ------------------------------------------------------------ API actions
    def start_challenge(self, dance_id, pair, countdown):
        with self.lock:
            if self.phase in ("countdown", "challenge"):
                return 409, {"error": "A round is already running"}
            dance = self.dances.get(dance_id)
            if dance is None:
                return 400, {"error": f"Unknown dance: {dance_id}"}
            if self.error:
                return 503, {"error": "Server failed to start: " + self.error}
            if self.async_det is None or dance.name not in self.ref:
                return 503, {"error": f"Still getting the dance ready ({self.stage}). Try again in a moment."}
            self.clear_round()
            self.async_det.reset()
            self.slots = [None, None]
            self.dance = dance
            self.pair = pair
            self.t0 = time.monotonic() + countdown
            self.phase = "countdown" if countdown > 0 else "challenge"
            return 200, {"ok": True, "phase": self.phase}

    def reset(self):
        with self.lock:
            self.phase = "idle"
            self.dance = None
            self.clear_round()
            if self.async_det is not None:
                self.async_det.reset()

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            live = self.sums / np.maximum(self.scored, 1)
            pair_live = float(live.mean())
            fresh = (now - self.last_result_time) < STALE_SECONDS
            players = []
            for s in (0, 1):
                present = bool(self.present[s]) and fresh
                players.append({
                    "present": present,
                    "live": round(float(live[s]), 4),
                    "now": round(float(self.inst[s]), 4),
                    "hits": int(self.hits[s]),
                    "stars": int(td.stars_for(live[s])),
                    "landmarks": self.landmarks[s] if present else None,
                })
            round_seconds = self.round_seconds.get(self.dance.name, td.CLIP_SECONDS) if self.dance else td.CLIP_SECONDS
            if self.phase == "challenge":
                time_left = max(0.0, round_seconds - (now - self.t0))
            elif self.phase == "countdown":
                time_left = float(round_seconds)
            else:
                time_left = 0.0
            return {
                "phase": self.phase,
                "ready": self.ready,
                "dance": self.dance.name if self.dance else None,
                "pair": self.pair,
                "time_left": round(time_left, 2),
                "countdown_left": round(max(0.0, self.t0 - now), 2) if self.phase == "countdown" else 0,
                "good_score": td.GOOD_SCORE,
                "pair_live": round(pair_live, 4),
                "pair_stars": int(td.stars_for(pair_live)),
                "players": players,
                "result": self.result,
            }

    def dance_list(self):
        def static_file(path):
            # use the static host only for files that really exist next to the video in the repo
            return MEDIA_BASE_URL and Path(path).exists()

        items = []
        for name, d in self.dances.items():
            mp3 = d.video.with_name(name + ".mp3")
            slow = d.video.with_name(name + "_slow.mp3")
            items.append({
                "id": name,
                "name": name,
                "duo": bool(d.duo),
                "video_url": f"{MEDIA_BASE_URL}/{d.video.name}" if MEDIA_BASE_URL else f"/api/dances/{name}/video",
                "audio_url": f"{MEDIA_BASE_URL}/{mp3.name}" if static_file(mp3) else f"/api/dances/{name}/audio",
                "slow_audio_url": f"{MEDIA_BASE_URL}/{slow.name}" if static_file(slow) else f"/api/dances/{name}/audio_slow",
                "duration_seconds": round(self.dance_seconds[name], 2),
                "round_seconds": round(self.round_seconds[name], 2),
            })
        return items


server = GameServer()


# ---------------------------------------------------------------- routes
@app.route("/api/health")
def health():
    return jsonify({"ok": server.error is None, "ready": server.ready, "stage": server.stage,
                    "progress": round(server.progress, 2), "error": server.error,
                    "dances_ready": sorted(server.ref)})


@app.route("/api/dances")
def api_dances():
    return jsonify({
        "dances": server.dance_list(),
        "clip_seconds": td.CLIP_SECONDS,
        "slow_speed": td.SLOW_SPEED,
        "pass_stars": td.PASS_STARS,
        "good_score": td.GOOD_SCORE,
        "ready": server.ready,
    })


def _media(dance_id, attr, default_mime):
    dance = server.dances.get(dance_id)
    if dance is None:
        abort(404)
    server.want(dance_id)
    if attr != "video":
        server.ensure_audio(dance_id)
    path = dance.video if attr == "video" else getattr(dance, attr, None)
    if not path or not Path(path).exists():
        abort(404)
    mime = default_mime
    if attr != "video":
        mime = "audio/wav" if Path(path).suffix.lower() == ".wav" else "audio/mpeg"
    return send_file(path, mimetype=mime, conditional=True, max_age=3600)


@app.route("/api/dances/<dance_id>/video")
def dance_video(dance_id):
    return _media(dance_id, "video", "video/mp4")


@app.route("/api/dances/<dance_id>/audio")
def dance_audio(dance_id):
    return _media(dance_id, "audio", "audio/mpeg")


@app.route("/api/dances/<dance_id>/audio_slow")
def dance_audio_slow(dance_id):
    return _media(dance_id, "audio_slow", "audio/mpeg")


@app.route("/api/frame", methods=["POST"])
def api_frame():
    data = request.get_data()
    if not data:
        return jsonify({"error": "empty frame"}), 400
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"error": "bad image"}), 400
    if not server.submit_frame(frame):
        return jsonify({"error": "detector still loading"}), 503
    return ("", 204)


@app.route("/api/challenge/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    pair = str(data.get("pair") or "").strip()[:20] or "Pair"
    try:
        countdown = float(data.get("countdown", COUNTDOWN_SECONDS))
    except (TypeError, ValueError):
        countdown = COUNTDOWN_SECONDS
    countdown = min(max(countdown, 0.0), 10.0)
    status, body = server.start_challenge(str(data.get("dance") or ""), pair, countdown)
    return jsonify(body), status


@app.route("/api/reset", methods=["POST"])
def api_reset():
    server.reset()
    return jsonify({"ok": True})


@app.route("/api/state")
def api_state():
    return jsonify(server.snapshot())


@app.route("/api/events")
def api_events():
    if server.sse_clients >= MAX_SSE_CLIENTS:
        return jsonify({"error": "too many viewers"}), 503

    game = server

    def stream():
        game.sse_clients += 1
        try:
            yield "retry: 1000\n\n"
            while game.running:
                yield "data: " + json.dumps(game.snapshot()) + "\n\n"
                time.sleep(1.0 / STATE_HZ)
        finally:
            game.sse_clients -= 1

    response = Response(stream_with_context(stream()), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/leaderboard")
def api_leaderboard():
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 100))
    except ValueError:
        limit = 10
    with server.lock:
        entries = td.best_per_pair(td.load_leaderboard())
    return jsonify(entries[:limit])


@app.route("/api/photos/<name>")
def api_photo(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.jpg", name):
        abort(404)
    return send_from_directory(td.PHOTO_DIR, name, max_age=3600)


# ---------------------------------------------------------------- optional: serve the frontend too
@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def frontend(path):
    folder = frontend_dir()
    if path.startswith("api/") or folder is None or Path(path).suffix.lower() not in FRONTEND_TYPES:
        if path == "index.html" and folder is None:
            return jsonify({"service": "snowglobe-api", "health": "/api/health"})
        abort(404)
    return send_from_directory(folder, path)


if __name__ == "__main__":
    server.setup()
    try:
        app.run(host=HOST, port=PORT, threaded=True)
    finally:
        server.close()
