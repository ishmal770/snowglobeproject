// API URL is set in config.js for deployment; local development defaults to port 5000.
const localHost = ["localhost", "127.0.0.1"].includes(location.hostname);
const API = String(window.SNOWGLOBE_API || (localHost ? "http://" + location.hostname + ":5000" : ""))
    .replace(/\/+$/, "");

let countdownTimer;
let introTimer = null;
let config = null;
let currentDance = null;
let teamName = "";
let latestState = null;
let lastPhase = "idle";
let waitingForResult = false;
let roundStarted = false;
let cameraStarted = false;
let cameraStream = null;
let frameTimer = null;
let frameCanvas = null;
let sendingFrame = false;
let lastHits = [0, 0];
let lastStarTime = [0, 0];
let playerCombos = [0, 0];
let lastComboHit = [0, 0];
let lastResult = null;

// practice room playback: one required slow play, one normal play, plus optional replays
let practiceStage = "idle"; // idle | countdown | slow | normal | replay | done
let practiceReplaysLeft = 3;
let practicePlayIndex = 0;
let practiceCountdownTimer = null;
let countdownNextStage = "slow";
let practiceSeeking = false;
let challengePreviewTimer = null;
let challengePreviewLimit = 0;
let challengePreviewOpaqueUntil = 0;
let challengePlaybackId = 0;
let displayedStars = -1;
let practicePlaybackId = 0;
let practiceAudioScale = 1;
let practiceHasSidecar = false;

const PLAYER_COLORS = ["#00b4ff", "#ffb400"];


// SWITCH BETWEEN SCREENS
function goToPage(pageId) {

    clearInterval(countdownTimer);
    clearInterval(introTimer);
    introTimer = null;

    document.querySelectorAll(".screen").forEach(page => {
        page.classList.remove("active");
    });

    document.getElementById(pageId).classList.add("active");

    if (!["tutorial", "countdown", "challenge"].includes(pageId)) stopBrowserCamera();

    if (pageId !== "tutorial") {
        stopPracticePlayback();
    }
    if (pageId !== "challenge") {
        stopChallengePreview();
    }

    // Show the big "practice room: time to learn" screen first
    if (pageId === "practice-intro") {
        startPracticeIntro();
    }

    // Connect the camera and pick a dance when entering practice room
    if (pageId === "tutorial") {
        startBackendCamera();
        enterPracticeRoom();
    }

    // Start splitting the camera and tell the server to start scoring
    if (pageId === "challenge") {
        drawCameraHalves();
        startBackendChallenge();
    }

    window.scrollTo(0, 0);

    // Automatically start countdown
    if (pageId === "countdown") {
        startCountdown();
    }

}


// COUNTDOWN: 5, 4, 3, 2, 1
function startCountdown() {

    let count = 5;

    const number = document.getElementById("countdown-number");

    number.textContent = count;

    countdownTimer = setInterval(() => {

        count--;

        if (count > 0) {
            number.textContent = count;

            // Replay animation
            number.style.animation = "none";
            void number.offsetWidth;
            number.style.animation = "pop 0.8s ease-in-out";

        } else {
            clearInterval(countdownTimer);

            number.textContent = "DANCE!";

            setTimeout(() => {
                goToPage("challenge");
            }, 800);
        }

    }, 1000);
}


// LOAD THE LIST OF DANCES FROM THE SERVER
async function loadConfig() {

    if (config) return config;

    const response = await fetch(API + "/api/dances");

    config = await response.json();

    return config;
}


// PICK A DANCE AND LOAD IT INTO THE PRACTICE ROOM
async function pickDance(forceNew = false) {

    const status = document.getElementById("camera-status");

    try {
        await loadConfig();
    } catch (error) {
        console.error("Server error:", error);
        status.textContent =
            "Can't reach the game server. Is python server.py running?";
        return false;
    }

    if (!currentDance || forceNew) {

        const options = config.dances.filter(
            dance => !currentDance || dance.id !== currentDance.id
        );

        currentDance =
            options[Math.floor(Math.random() * options.length)] || config.dances[0];
    }

    const video = document.getElementById("tutorial-video");
    const music = document.getElementById("music-player");
    const url = API + currentDance.video_url;
    const audioUrl = currentDance.audio_url ? API + currentDance.audio_url : url;

    if (video.getAttribute("src") !== url) {
        video.src = url;
        video.load();
    }
    if (music.getAttribute("src") !== audioUrl) {
        music.src = audioUrl;
        music.load();
    }

    const duoHint = document.getElementById("duo-hint");
    if (duoHint) duoHint.hidden = !currentDance.duo;

    return true;
}


async function newDance() {

    const loaded = await pickDance(true);

    if (!loaded) return;
    if (!document.getElementById("tutorial").classList.contains("active")) return;

    // a different dance starts the mandatory slow + fast watch again
    beginPracticeSequence();
}


// ============================================================
// PRACTICE ROOM: BIG "TIME TO LEARN" INTRO
// ============================================================

function startPracticeIntro() {

    clearInterval(introTimer);

    let count = 5;

    const label = document.getElementById("intro-count");

    if (label) label.textContent = count;

    introTimer = setInterval(() => {

        count--;

        if (label) label.textContent = Math.max(count, 0);

        if (count <= 0) {

            clearInterval(introTimer);
            introTimer = null;

            if (document.getElementById("practice-intro").classList.contains("active")) {
                goToPage("tutorial");
            }
        }

    }, 1000);
}


function enterPractice() {

    clearInterval(introTimer);
    introTimer = null;

    goToPage("tutorial");
}


// ============================================================
// PRACTICE ROOM: ONE REQUIRED SLOW PLAY, ONE NORMAL PLAY, THEN OPTIONAL REPLAYS
// ============================================================

async function enterPracticeRoom() {

    const loaded = await pickDance();

    if (!loaded) return;
    if (!document.getElementById("tutorial").classList.contains("active")) return;

    beginPracticeSequence();
}


function beginPracticeSequence() {

    practicePlayIndex = 0;
    practiceReplaysLeft = 3;
    queuePracticePlayback("slow", 0.75);
}


function queuePracticePlayback(stage, rate) {

    clearInterval(practiceCountdownTimer);
    countdownNextStage = stage;
    practiceStage = "countdown";
    updatePracticeUI();

    const overlay = document.getElementById("practice-countdown");
    const label = document.getElementById("practice-countdown-label");
    const number = document.getElementById("practice-countdown-number");
    let count = 3;

    label.textContent = stage === "replay" ? "REPLAY STARTS IN" : "NEXT VIEWING IN";
    number.textContent = count;
    overlay.hidden = false;

    practiceCountdownTimer = setInterval(() => {
        count--;
        if (count > 0) {
            number.textContent = count;
            number.classList.remove("countdown-pop");
            void number.offsetWidth;
            number.classList.add("countdown-pop");
            return;
        }

        clearInterval(practiceCountdownTimer);
        practiceCountdownTimer = null;
        overlay.hidden = true;
        practiceStage = stage;
        if (stage !== "replay") practicePlayIndex++;
        updatePracticeUI();
        playPracticeVideo(rate);
    }, 1000);
}


function waitForMediaReady(media, timeoutMs = 12000) {
    if (media.readyState >= 2) return Promise.resolve();

    return new Promise(resolve => {
        let finished = false;
        const finish = () => {
            if (finished) return;
            finished = true;
            clearTimeout(timer);
            media.removeEventListener("canplay", finish);
            media.removeEventListener("error", finish);
            resolve();
        };
        const timer = setTimeout(finish, timeoutMs);
        media.addEventListener("canplay", finish, { once: true });
        media.addEventListener("error", finish, { once: true });
    });
}


function seekMedia(media, target = 0) {
    if (!media || media.readyState < 1) return Promise.resolve();
    if (Math.abs(media.currentTime - target) < 0.03) return Promise.resolve();

    return new Promise(resolve => {
        let finished = false;
        const finish = () => {
            if (finished) return;
            finished = true;
            clearTimeout(timer);
            media.removeEventListener("seeked", finish);
            resolve();
        };
        const timer = setTimeout(finish, 1200);
        media.addEventListener("seeked", finish, { once: true });
        try {
            media.currentTime = target;
        } catch (error) {
            finish();
        }
    });
}


function syncPracticeAudio() {
    if (!practiceHasSidecar || practiceStage === "idle" || practiceStage === "done") return;
    const video = document.getElementById("tutorial-video");
    const music = document.getElementById("music-player");
    if (!video || !music || video.paused || music.paused) return;

    const target = video.currentTime / practiceAudioScale;
    if (Math.abs(music.currentTime - target) > 0.12) {
        music.currentTime = Math.min(target, Number.isFinite(music.duration) ? music.duration : target);
    }
}


function pausePracticeAudioWhileVideoBuffers() {
    const music = document.getElementById("music-player");
    if (practiceHasSidecar && music) music.pause();
}


function resumePracticeAudioWithVideo() {
    if (!practiceHasSidecar || practiceStage === "idle" || practiceStage === "done") return;
    const music = document.getElementById("music-player");
    const video = document.getElementById("tutorial-video");
    if (!music || !video || video.paused) return;
    music.currentTime = Math.min(
        video.currentTime / practiceAudioScale,
        Number.isFinite(music.duration) ? music.duration : Infinity
    );
    music.play().catch(() => {});
}


document.getElementById("tutorial-video").addEventListener("waiting", pausePracticeAudioWhileVideoBuffers);
document.getElementById("tutorial-video").addEventListener("playing", resumePracticeAudioWithVideo);


async function playPracticeVideo(rate) {
    const video = document.getElementById("tutorial-video");
    const music = document.getElementById("music-player");
    if (!video || !video.getAttribute("src")) return;

    const playbackId = ++practicePlaybackId;
    document.getElementById("tutorial-tap").hidden = true;
    video.pause();
    music.pause();
    video.playbackRate = rate;

    const slowTrack = rate < 1 && currentDance && currentDance.slow_audio_url
        ? API + currentDance.slow_audio_url : null;
    const normalTrack = currentDance && currentDance.audio_url
        ? API + currentDance.audio_url : null;
    const audioTrack = slowTrack || normalTrack;

    practiceHasSidecar = Boolean(audioTrack);
    practiceAudioScale = slowTrack ? rate : 1;

    if (audioTrack && music.getAttribute("src") !== audioTrack) {
        music.src = audioTrack;
        music.load();
    }
    music.playbackRate = slowTrack ? 1 : rate;
    video.muted = Boolean(audioTrack);

    practiceSeeking = true;
    try {
        await Promise.all([
            waitForMediaReady(video),
            audioTrack ? waitForMediaReady(music) : Promise.resolve()
        ]);
        if (playbackId !== practicePlaybackId) return;

        await Promise.all([
            seekMedia(video, 0),
            audioTrack ? seekMedia(music, 0) : Promise.resolve()
        ]);
        if (playbackId !== practicePlaybackId) return;

        practiceSeeking = false;
        const attempts = await Promise.all([
            video.play(),
            audioTrack ? music.play() : Promise.resolve()
        ]);
        void attempts;
        syncPracticeAudio();
    } catch (error) {
        if (playbackId !== practicePlaybackId || error.name === "AbortError") return;
        video.pause();
        music.pause();
        video.muted = false;
        document.getElementById("tutorial-tap").hidden = false;
    } finally {
        if (playbackId === practicePlaybackId) practiceSeeking = false;
    }
}

async function resumePracticeVideo() {
    document.getElementById("tutorial-tap").hidden = true;
    const video = document.getElementById("tutorial-video");
    const music = document.getElementById("music-player");
    try {
        await Promise.all([
            video.play(),
            practiceHasSidecar ? music.play() : Promise.resolve()
        ]);
        syncPracticeAudio();
    } catch (error) {
        music.pause();
        video.muted = false;
        document.getElementById("tutorial-tap").hidden = false;
    }
}

function stopPracticePlayback() {

    const video = document.getElementById("tutorial-video");

    practicePlaybackId++;
    practiceHasSidecar = false;
    if (video) {
        video.pause();
        video.muted = false;
    }
    document.getElementById("music-player").pause();

    clearInterval(practiceCountdownTimer);
    practiceCountdownTimer = null;
    practiceStage = "idle";
    practiceSeeking = false;
    const overlay = document.getElementById("practice-countdown");
    if (overlay) overlay.hidden = true;

    const tap = document.getElementById("tutorial-tap");
    if (tap) tap.hidden = true;

    updatePracticeUI();
}


function finishPracticePlayback() {

    const video = document.getElementById("tutorial-video");

    video.pause();
    document.getElementById("music-player").pause();

    if (practiceStage === "slow") {
        queuePracticePlayback("normal", 1);
        return;
    }

    // The required slow and normal-speed viewings are complete.
    practiceStage = "done";

    try {
        video.currentTime = 0;
    } catch (error) {
        // ignore
    }

    updatePracticeUI();
}


function replayFast() {

    if (practiceStage === "slow" || practiceStage === "normal" || practiceStage === "countdown") return;
    if (practiceReplaysLeft <= 0) return;

    practiceReplaysLeft--;
    queuePracticePlayback("replay", 1);
}


function updatePracticeUI() {

    const step = document.getElementById("practice-step");
    const badge = document.getElementById("tutorial-badge");
    const replay = document.getElementById("replay-fast");
    const start = document.getElementById("start-challenge-button");

    let stepText = "GETTING READY";
    let badgeText = "";

    if (practiceStage === "countdown") {
        stepText = `GET READY • VIEWING ${Math.min(practicePlayIndex + 1, 2)}/2`;
        badgeText = countdownNextStage === "slow" ? "NEXT: SLOW 0.75×" :
            countdownNextStage === "normal" ? "NEXT: NORMAL 1×" : "OPTIONAL REPLAY 1×";
    } else if (practiceStage === "slow") {
        stepText = "WATCHING SLOW 1/1";
        badgeText = "SLOW 0.75× • AUDIO SLOWED";
    } else if (practiceStage === "normal") {
        stepText = "WATCHING NORMAL 1/1";
        badgeText = "NORMAL SPEED 1×";
    } else if (practiceStage === "replay") {
        stepText = "OPTIONAL REPLAY";
        badgeText = "NORMAL SPEED 1×";
    } else if (practiceStage === "done") {
        stepText = "READY TO PRACTICE";
    }

    if (step) step.textContent = stepText;

    if (badge) {
        badge.textContent = badgeText;
        badge.hidden = !badgeText;
    }

    if (replay) {
        replay.disabled = practiceStage !== "done" || practiceReplaysLeft <= 0;
        replay.textContent = practiceReplaysLeft > 0
            ? "↻ REPLAY NORMAL SPEED (" + practiceReplaysLeft + " left)"
            : "NO REPLAYS LEFT";
    }

    if (start) {
        // Both required viewings must finish before the challenge can start.
        start.disabled = practiceStage !== "done";
    }
}


// stop the tutorial at the end of the part used in the challenge
document.getElementById("tutorial-video").addEventListener("timeupdate", event => {

    if (!config || practiceSeeking) return;

    syncPracticeAudio();
    const video = event.target;
    const limit = Math.min(config.clip_seconds, video.duration || config.clip_seconds);

    if (video.currentTime < limit - 0.05) return;

    if (practiceStage === "slow" || practiceStage === "normal" || practiceStage === "replay") {
        finishPracticePlayback();
    }
});


// CHECK THE NAME, THEN START
function startFromPractice() {

    const name = document.getElementById("team-name").value.trim();
    const message = document.getElementById("name-message");

    if (!name) {
        message.textContent = "Please enter your team name first!";
        return;
    }

    if (!currentDance) {
        message.textContent =
            "The dance isn't loaded yet. Is python server.py running?";
        return;
    }

    // The tutorial's four required viewings must finish before starting.
    if (practiceStage !== "done") {
        message.textContent =
            "Finish the slow viewing and the normal-speed viewing first!";
        return;
    }

    message.textContent = "";
    teamName = name;

    // the browser only lets music play after a click, so unlock it now
    const music = document.getElementById("music-player");
    music.muted = true;
    music.play()
        .then(() => { music.pause(); music.muted = false; })
        .catch(() => { music.muted = false; });

    goToPage("countdown");
}


// Request the local team's camera in the browser and send small frames to the scoring API.
async function startBackendCamera() {
    if (cameraStarted) return;
    const status = document.getElementById("camera-status");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        status.textContent = "Camera access needs a supported browser and HTTPS.";
        return;
    }
    try {
        cameraStream = await navigator.mediaDevices.getUserMedia({
            video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false
        });
        const preview = document.getElementById("practice-camera");
        const source = document.getElementById("camera-source");
        preview.srcObject = cameraStream;
        source.srcObject = cameraStream;
        await Promise.all([preview.play(), source.play()]);
        cameraStarted = true;
        frameCanvas = document.createElement("canvas");
        status.textContent = "Camera connected. Stand side by side so both players are visible.";
        if (frameTimer) clearInterval(frameTimer);
        frameTimer = setInterval(sendCameraFrame, 200);
    } catch (error) {
        cameraStarted = false;
        status.textContent = "Camera unavailable: allow camera permission, then try again.";
        console.error("Camera permission failed:", error);
    }
}

async function sendCameraFrame() {
    const source = document.getElementById("camera-source");
    if (!cameraStarted || sendingFrame || !source.videoWidth || !source.videoHeight) return;
    sendingFrame = true;
    try {
        const scale = Math.min(1, 480 / source.videoWidth);
        frameCanvas.width = Math.round(source.videoWidth * scale);
        frameCanvas.height = Math.round(source.videoHeight * scale);
        frameCanvas.getContext("2d").drawImage(source, 0, 0, frameCanvas.width, frameCanvas.height);
        const blob = await new Promise(resolve => frameCanvas.toBlob(resolve, "image/jpeg", 0.65));
        if (blob) {
            const response = await fetch(API + "/api/frame", {
                method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob
            });
            if (!response.ok && response.status !== 503) throw new Error("camera upload failed: " + response.status);
        }
    } catch (error) {
        console.error("Camera frame upload failed:", error);
        document.getElementById("camera-status").textContent = "Lost connection to the scoring server.";
    } finally {
        sendingFrame = false;
    }
}

function stopBrowserCamera() {
    if (frameTimer) clearInterval(frameTimer);
    frameTimer = null;
    sendingFrame = false;
    cameraStarted = false;
    if (cameraStream) cameraStream.getTracks().forEach(track => track.stop());
    cameraStream = null;
    ["practice-camera", "camera-source"].forEach(id => {
        const video = document.getElementById(id);
        if (video) video.srcObject = null;
    });
}

// TELL THE PRACTICE ROOM IF BOTH PLAYERS CAN BE SEEN
function updatePracticeStatus(state) {

    if (!document.getElementById("tutorial").classList.contains("active")) return;

    const status = document.getElementById("camera-status");
    const [p1, p2] = state.players;

    if (p1.present && p2.present) {
        status.textContent = "Both players detected! ✅ Ready to dance.";
    } else if (!p1.present && !p2.present) {
        status.textContent =
            "Step in front of the camera! Player 1 on the left, Player 2 on the right.";
    } else if (!p1.present) {
        status.textContent = "Waiting for Player 1 (left side)...";
    } else {
        status.textContent = "Waiting for Player 2 (right side)...";
    }
}


// TELL THE SERVER TO START THE CHALLENGE
async function startBackendChallenge() {

    const message = document.getElementById("challenge-message");

    // Avoid duplicate start requests if the screen transition fires twice.
    if (waitingForResult) return;

    resetChallengeUI();

    if (!currentDance) {
        message.textContent = "No dance loaded. Is python server.py running?";
        return;
    }

    waitingForResult = true;
    roundStarted = false;
    lastHits = [0, 0];

    try {
        const startRequest = () => fetch(API + "/api/challenge/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                dance: currentDance.id,
                pair: teamName,
                countdown: 0
            })
        });

        let response = await startRequest();
        if (response.status === 409) {
            const conflict = await response.json().catch(() => ({}));
            if (String(conflict.error || "").includes("already running")) {
                // The server keeps one shared game state across page reloads.
                // Clear a stale round, then retry this start once.
                const resetResponse = await fetch(API + "/api/reset", { method: "POST" });
                if (!resetResponse.ok) throw new Error("Could not reset the previous round.");
                lastPhase = "idle";
                response = await startRequest();
            } else {
                throw new Error(conflict.error || response.status);
            }
        }

        if (!response.ok) {
            const info = await response.json().catch(() => ({}));
            throw new Error(info.error || response.status);
        }

    } catch (error) {

        console.error("Could not start:", error);

        waitingForResult = false;

        message.textContent = "Couldn't start the dance: " + error.message;
    }
}


// LIVE UPDATES FROM THE SERVER (about 15 times a second)
function connectEvents() {

    const events = new EventSource(API + "/api/events");

    events.onmessage = event => onState(JSON.parse(event.data));

    events.onerror = () => {
        if (document.getElementById("tutorial").classList.contains("active")) {
            document.getElementById("camera-status").textContent =
                "Lost connection to the game server. Is python server.py running?";
        }
    };
}


function onState(state) {

    latestState = state;

    updatePracticeStatus(state);

    if (waitingForResult) {

        if (state.phase === "countdown" || state.phase === "challenge") {
            roundStarted = true;
        }

        // Start the reference video and soundtrack together when the round begins.
        if (state.phase === "challenge" && lastPhase !== "challenge") {
            startChallengePlayback();
        }

        if (state.phase === "challenge") {
            updateChallengeUI(state);
        }

        // the dance is over
        if (state.phase === "results" && roundStarted && state.result) {
            waitingForResult = false;
            roundStarted = false;
            document.getElementById("music-player").pause();
            stopChallengePreview();
            showResults(state.result);
        }
    }

    lastPhase = state.phase;
}


async function startChallengePlayback() {
    stopChallengePreview();
    if (!currentDance) return;

    const playbackId = challengePlaybackId;
    const preview = document.getElementById("challenge-preview");
    const reference = document.getElementById("challenge-reference");
    const music = document.getElementById("music-player");
    const previewUrl = API + currentDance.video_url;
    const musicUrl = currentDance.audio_url ? API + currentDance.audio_url : "";

    if (preview.getAttribute("src") !== previewUrl) {
        preview.src = previewUrl;
        preview.load();
    }
    if (musicUrl && music.getAttribute("src") !== musicUrl) {
        music.src = musicUrl;
        music.load();
    }

    challengePreviewLimit = Math.max(
        1,
        Number(currentDance.round_seconds || currentDance.duration_seconds || config.clip_seconds || 15)
    );
    challengePreviewOpaqueUntil = challengePreviewLimit / 3;
    preview.playbackRate = 1;
    preview.muted = Boolean(musicUrl);
    reference.style.opacity = "1";
    reference.hidden = false;
    preview.hidden = false;

    try {
        await Promise.all([
            waitForMediaReady(preview),
            musicUrl ? waitForMediaReady(music) : Promise.resolve()
        ]);
        if (playbackId !== challengePlaybackId ||
            !document.getElementById("challenge").classList.contains("active")) return;

        await Promise.all([
            seekMedia(preview, 0),
            musicUrl ? seekMedia(music, 0) : Promise.resolve()
        ]);
        if (playbackId !== challengePlaybackId) return;

        const attempts = await Promise.all([preview.play(), musicUrl ? music.play() : Promise.resolve()]);
        void attempts;
    } catch (error) {
        if (playbackId === challengePlaybackId) {
            console.warn("Challenge media could not start:", error);
            stopChallengePreview();
        }
        return;
    }

    preview.ontimeupdate = () => {
        if (musicUrl && !music.paused && Math.abs(music.currentTime - preview.currentTime) > 0.12) {
            music.currentTime = Math.min(preview.currentTime, Number.isFinite(music.duration) ? music.duration : preview.currentTime);
        }
        const fadeDuration = Math.max(0.01, challengePreviewLimit - challengePreviewOpaqueUntil);
        const fade = Math.max(0, Math.min(1,
            (preview.currentTime - challengePreviewOpaqueUntil) / fadeDuration));
        reference.style.opacity = String(1 - fade);
    };
    preview.onwaiting = () => { if (musicUrl) music.pause(); };
    preview.onplaying = () => {
        if (musicUrl && music.paused && playbackId === challengePlaybackId) {
            music.currentTime = Math.min(preview.currentTime, Number.isFinite(music.duration) ? music.duration : preview.currentTime);
            music.play().catch(error => console.warn("Challenge audio could not resume:", error));
        }
    };
    preview.onended = stopChallengePreview;
    challengePreviewTimer = setTimeout(
        stopChallengePreview,
        challengePreviewLimit * 1000 + 1000
    );
}


function stopChallengePreview() {
    clearTimeout(challengePreviewTimer);
    challengePreviewTimer = null;
    challengePlaybackId++;

    const preview = document.getElementById("challenge-preview");
    const reference = document.getElementById("challenge-reference");
    const music = document.getElementById("music-player");
    if (preview) {
        preview.pause();
        preview.hidden = true;
        preview.ontimeupdate = null;
        preview.onwaiting = null;
        preview.onplaying = null;
        preview.onended = null;
    }
    if (reference) {
        reference.style.opacity = "1";
        reference.hidden = true;
    }
    if (music) music.pause();
}



// UPDATE THE CHALLENGE SCREEN
function updateChallengeUI(state) {

    const percent = Math.round(state.pair_live * 100);
    const stars = state.pair_stars;

    document.getElementById("progress-fill").style.width = percent + "%";

    document.getElementById("accuracy-text").textContent = percent + "%";

    document.getElementById("time-left").textContent =
        Math.ceil(state.time_left) + "s";

    const starDisplay = document.getElementById("stars-display");
    if (stars !== displayedStars) {
        starDisplay.replaceChildren();
        for (let index = 0; index < 5; index++) {
            const star = document.createElement("span");
            const earned = index < stars;
            star.className = "score-star " + (earned ? "earned" : "empty");
            star.setAttribute("aria-hidden", "true");
            star.textContent = earned ? "★" : "☆";
            starDisplay.appendChild(star);
        }
        displayedStars = stars;
        starDisplay.setAttribute("aria-label", stars + " of 5 stars");
    }

    const [p1, p2] = state.players;

    document.getElementById("challenge-message").textContent =
        "Team accuracy: " + percent + "%  •  Player 1: " +
        Math.round(p1.live * 100) + "%  •  Player 2: " +
        Math.round(p2.live * 100) + "%";

    // the ice melts as the team does better
    document.getElementById("ice-overlay").style.opacity =
        Math.max(0, 1 - state.pair_live / 0.9) * 0.85;

    state.players.forEach((player, index) => {

        const panel = document.getElementById("p" + (index + 1) + "-panel");
        const title = document.getElementById("p" + (index + 1) + "-title");
        const star = document.getElementById("p" + (index + 1) + "-star");

        title.textContent =
            "PLAYER " + (index + 1) + " · " + Math.round(player.live * 100) + "%";

        // gold = above the 80% "good frame" threshold
        const hitting = player.present && player.now >= state.good_score;

        let quality = "q-idle";

        if (player.present) {
            if (hitting) quality = "q-good";
            else if (player.now >= 0.5) quality = "q-ok";
            else quality = "q-bad";
        }

        panel.classList.remove("q-good", "q-ok", "q-bad", "q-idle");
        panel.classList.add(quality);

        // big star stays around the player while they keep hitting good frames
        if (star) star.classList.toggle("active", hitting);

        // Show each dancer their live streak and celebrate matched moves.
        const now = performance.now();
        const gainedHits = Math.max(0, Number(player.hits || 0) - Number(lastHits[index] || 0));
        const feedback = document.getElementById("p" + (index + 1) + "-feedback");

        if (gainedHits > 0) {
            if (now - lastComboHit[index] > 900) playerCombos[index] = 0;
            playerCombos[index] += gainedHits;
            lastComboHit[index] = now;
            if (now - lastStarTime[index] > 200) {
                spawnStar(panel, index);
                lastStarTime[index] = now;
            }
        } else if (now - lastComboHit[index] > 1000) {
            playerCombos[index] = 0;
        }

        if (!player.present) {
            feedback.textContent = "STEP INTO VIEW";
        } else if (hitting && playerCombos[index] >= 4) {
            feedback.textContent = "🔥 ON FIRE ×" + playerCombos[index];
        } else if (hitting) {
            feedback.textContent = "NICE MOVE ×" + Math.max(playerCombos[index], 1);
        } else if (player.now >= 0.5) {
            feedback.textContent = "ALMOST — KEEP GOING";
        } else {
            feedback.textContent = "MOVE WITH THE BEAT";
        }
        feedback.classList.toggle("combo-hot", hitting && playerCombos[index] >= 4);
        feedback.classList.toggle("combo-hit", hitting);

        lastHits[index] = player.hits;
    });
}


function spawnStar(panel, side) {

    const star = document.createElement("span");

    star.className = "star-pop " + (side === 0 ? "left" : "right");
    star.textContent = "★";

    panel.appendChild(star);

    setTimeout(() => star.remove(), 1400);
}


function resetChallengeUI() {

    document.getElementById("progress-fill").style.width = "0%";
    document.getElementById("accuracy-text").textContent = "0%";
    document.getElementById("time-left").textContent = "";
    document.getElementById("stars-display").textContent = "☆ ☆ ☆ ☆ ☆";
    displayedStars = -1;
    document.getElementById("challenge-message").textContent = "Team accuracy: 0%";
    document.getElementById("ice-overlay").style.opacity = 0.85;
    document.getElementById("p1-title").textContent = "PLAYER 1";
    document.getElementById("p2-title").textContent = "PLAYER 2";

    ["p1-panel", "p2-panel"].forEach(id => {
        document.getElementById(id).classList.remove("q-good", "q-ok", "q-bad", "q-idle");
    });

    lastHits = [0, 0];
    lastStarTime = [0, 0];
    playerCombos = [0, 0];
    lastComboHit = [0, 0];
    ["p1-star", "p2-star"].forEach(id => {
        const star = document.getElementById(id);
        if (star) star.classList.remove("active");
    });
    ["p1-feedback", "p2-feedback"].forEach(id => {
        const feedback = document.getElementById(id);
        if (feedback) {
            feedback.textContent = "STEP INTO VIEW";
            feedback.classList.remove("combo-hot", "combo-hit");
        }
    });
}


// SHOW THE RESULT FROM THE SERVER
function showResults(result) {

    lastResult = result;

    const percent = Math.round(result.pair_accuracy * 100);

    const detail =
        "Player 1: " + Math.round(result.accuracies[0] * 100) + "%  •  Player 2: " +
        Math.round(result.accuracies[1] * 100) + "%";

    const rankText = result.rank ? "Team rank: #" + result.rank : "";

    const photo = API + result.photo_url;

    // 3+ stars passes and unlocks another dance / freeing Olaf
    if (result.passed) {

        document.getElementById("ice-overlay").style.opacity = 0;
        document.getElementById("olaf-image").src = "assets/characters/free%20olaf.png";

        document.getElementById("final-score").textContent = percent + "%";

        document.getElementById("final-detail").textContent = detail;
        document.getElementById("final-rank").textContent = rankText;

        document.getElementById("submit-message").textContent =
            result.new_personal_best
                ? "NEW PERSONAL BEST — your score has been saved."
                : "Your score has been saved.";

        const victoryPhoto = document.getElementById("victory-photo");
        victoryPhoto.src = photo;
        victoryPhoto.hidden = false;

        goToPage("victory");

        displayLeaderboard();

    } else {

        document.getElementById("failure-score").textContent = percent + "%";

        document.getElementById("failure-detail").textContent = detail;
        document.getElementById("failure-rank").textContent = rankText;

        const failurePhoto = document.getElementById("failure-photo");
        failurePhoto.src = photo;
        failurePhoto.hidden = false;

        goToPage("failure");

        displayLeaderboard();
    }
}


// DISPLAY LEADERBOARD (kept by the game server)
async function displayLeaderboard() {

    let entries = [];

    try {
        const response = await fetch(API + "/api/leaderboard?limit=10");
        entries = await response.json();
    } catch (error) {
        ["leaderboard-list", "failure-leaderboard-list", "finale-leaderboard-list"].forEach(id => {
            const list = document.getElementById(id);
            if (list) list.innerHTML = "<li>Leaderboard unavailable.</li>";
        });
        return;
    }

    renderLeaderboard("leaderboard-list", entries);
    renderLeaderboard("failure-leaderboard-list", entries);
    renderLeaderboard("finale-leaderboard-list", entries);
}


function renderLeaderboard(listId, entries) {

    const list = document.getElementById(listId);

    if (!list) return;

    list.innerHTML = "";

    if (entries.length === 0) {

        list.innerHTML = "<li>No scores yet. Be the first!</li>";

        return;
    }

    entries.forEach((entry, index) => {
        const item = document.createElement("li");
        item.className = "leaderboard-row";
        if (entry.pair.toLowerCase() === teamName.toLowerCase()) item.classList.add("me");

        const rank = document.createElement("span");
        rank.className = "leaderboard-rank";
        rank.textContent = String(index + 1).padStart(2, "0");

        const name = document.createElement("span");
        name.className = "leaderboard-name";
        name.textContent = entry.pair;

        const score = document.createElement("span");
        score.className = "leaderboard-score";
        score.textContent = Math.round(entry.accuracy * 100) + "%";

        const stars = document.createElement("span");
        stars.className = "leaderboard-stars";
        stars.textContent = (entry.stars || 0) + " / 5 stars";

        item.append(rank, name, score, stars);
        list.appendChild(item);
    });
}


// PASSED A DANCE: PRACTICE A NEW ONE
function nextDance() {

    resetEverything();

    currentDance = null;

    goToPage("practice-intro");
}


// PASSED A DANCE: FINISH THE STORY AND FREE OLAF
function freeOlaf() {

    if (lastResult) {

        document.getElementById("finale-score").textContent =
            "Final dance accuracy: " + Math.round(lastResult.pair_accuracy * 100) + "%";

        document.getElementById("finale-detail").textContent =
            "Player 1: " + Math.round(lastResult.accuracies[0] * 100) + "%  •  Player 2: " +
            Math.round(lastResult.accuracies[1] * 100) + "%";
    }

    goToPage("finale");

    displayLeaderboard();
}


// STOP EVERYTHING AND TELL THE SERVER TO GO BACK TO WAITING
function resetEverything() {

    waitingForResult = false;
    roundStarted = false;

    document.getElementById("music-player").pause();

    stopPracticePlayback();

    resetChallengeUI();

    document.getElementById("victory-photo").hidden = true;
    document.getElementById("failure-photo").hidden = true;

    fetch(API + "/api/reset", { method: "POST" }).catch(() => {});
}


// RESTART GAME
function restartGame() {

    resetEverything();

    currentDance = null;
    lastResult = null;
    teamName = "";

    const nameInput = document.getElementById("team-name");
    if (nameInput) nameInput.value = "";

    goToPage("welcome");
}


// TRY AGAIN (back to the practice room, same team name)
function retryGame() {

    resetEverything();

    goToPage("practice-intro");
}


function nextPlayer() {

    resetEverything();
    currentDance = null;
    teamName = "";
    lastResult = null;

    const nameInput = document.getElementById("team-name");
    if (nameInput) nameInput.value = "";

    goToPage("practice-intro");
}


// SPLIT THE CAMERA INTO LEFT AND RIGHT HALVES (and draw each player's skeleton)
function drawCameraHalves() {

    // Stop drawing when we leave the challenge
    if (!document.getElementById("challenge").classList.contains("active")) {
        return;
    }

    const source = document.getElementById("camera-source");

    const leftCanvas = document.getElementById("player1-canvas");
    const rightCanvas = document.getElementById("player2-canvas");

    if (!source || !source.videoWidth) {
        requestAnimationFrame(drawCameraHalves);
        return;
    }

    const width = source.videoWidth;
    const height = source.videoHeight;

    const halfWidth = Math.floor(width / 2);

    // Set canvas dimensions to match the actual camera image
    if (leftCanvas.width !== halfWidth ||
        leftCanvas.height !== height) {

        leftCanvas.width = halfWidth;
        leftCanvas.height = height;

        rightCanvas.width = width - halfWidth;
        rightCanvas.height = height;
    }

    const leftContext = leftCanvas.getContext("2d");
    const rightContext = rightCanvas.getContext("2d");

    // Mirror both visible halves to match the frames scored by the backend.
    leftContext.save();
    leftContext.translate(halfWidth, 0);
    leftContext.scale(-1, 1);
    leftContext.drawImage(source, width - halfWidth, 0, halfWidth, height, 0, 0, halfWidth, height);
    leftContext.restore();

    rightContext.save();
    rightContext.translate(width - halfWidth, 0);
    rightContext.scale(-1, 1);
    rightContext.drawImage(source, 0, 0, width - halfWidth, height, 0, 0, width - halfWidth, height);
    rightContext.restore();

    drawSkeleton(leftContext, 0, 0, width, height);
    drawSkeleton(rightContext, 1, halfWidth, width, height);

    // Keep updating the live camera feed
    requestAnimationFrame(drawCameraHalves);
}


// DRAW ONE PLAYER'S BODY POINTS ON THEIR HALF
function drawSkeleton(context, playerIndex, offsetX, width, height) {

    if (!latestState) return;

    const landmarks = latestState.players[playerIndex].landmarks;

    if (!landmarks) return;

    const color = PLAYER_COLORS[playerIndex];
    const radius = Math.max(5, height / 85);

    // Show only shoulders, elbows, wrists, hips, knees, and ankles.
    const importantJoints = [11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28];
    for (const index of importantJoints) {

        if (landmarks[index][2] < 0.3) continue;

        const x = landmarks[index][0] * width - offsetX;
        const y = landmarks[index][1] * height;

        // black outline keeps the dots visible on light and dark backgrounds
        context.beginPath();
        context.arc(x, y, radius + 3, 0, Math.PI * 2);
        context.fillStyle = "rgba(0, 0, 0, 0.8)";
        context.fill();

        context.beginPath();
        context.arc(x, y, radius, 0, Math.PI * 2);
        context.fillStyle = color;
        context.fill();
    }
}


connectEvents();
