// GAME SERVER ADDRESS (python server.py)
// If this page is served by the game server it uses the same address,
// otherwise (file, live server, npm run dev) it talks to the server on port 5000.
const API = (location.protocol.startsWith("http") && location.port === "5000")
    ? ""
    : "http://127.0.0.1:5000";

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
let lastHits = [0, 0];
let lastStarTime = [0, 0];

// practice room playback: mandatory slow once, fast once, then 2 fast replays
let practiceStage = "idle"; // idle | slow | fast | replay | done
let practiceReplaysLeft = 2;

const BODY = [[11, 12], [11, 13], [13, 15], [12, 14], [14, 16], [11, 23], [12, 24], [23, 24],
              [23, 25], [25, 27], [27, 29], [29, 31], [27, 31], [24, 26], [26, 28], [28, 30],
              [30, 32], [28, 32]];
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

    if (pageId !== "tutorial") {
        stopPracticePlayback();
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
        return;
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

    if (video.getAttribute("src") !== url) {
        video.src = url;
        video.load();
        music.src = url;
        music.load();
    }

    document.getElementById("duo-hint").hidden = !currentDance.duo;
}


function newDance() {
    pickDance(true);
}


// PLAY THE TUTORIAL SLOW OR AT NORMAL SPEED
function playTutorial(speed) {

    const video = document.getElementById("tutorial-video");

    video.currentTime = 0;
    video.playbackRate = speed;
    video.muted = false;
    video.play();
}


// keep the tutorial looping over just the part that is used in the challenge
document.getElementById("tutorial-video").addEventListener("timeupdate", event => {

    if (config && event.target.currentTime >= config.clip_seconds) {
        event.target.currentTime = 0;
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


// SHOW THE LIVE CAMERA FROM THE GAME SERVER
function startBackendCamera() {

    if (cameraStarted) return;

    cameraStarted = true;

    const preview = document.getElementById("practice-camera");
    const source = document.getElementById("camera-source");

    preview.onerror = () => {
        cameraStarted = false;
        document.getElementById("camera-status").textContent =
            "Can't reach the game server. Is python server.py running?";
    };

    preview.src = API + "/video_feed";
    source.src = API + "/video_feed";
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

    resetChallengeUI();

    if (!currentDance) {
        message.textContent = "No dance loaded. Is python server.py running?";
        return;
    }

    waitingForResult = true;
    roundStarted = false;
    lastHits = [0, 0];

    try {

        const response = await fetch(API + "/api/challenge/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                dance: currentDance.id,
                pair: teamName,
                countdown: 0
            })
        });

        if (!response.ok) {
            const info = await response.json();
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

        // the dance just started: play the music
        if (state.phase === "challenge" && lastPhase !== "challenge") {
            playMusic();
        }

        if (state.phase === "challenge") {
            updateChallengeUI(state);
        }

        // the dance is over
        if (state.phase === "results" && roundStarted && state.result) {
            waitingForResult = false;
            roundStarted = false;
            document.getElementById("music-player").pause();
            showResults(state.result);
        }
    }

    lastPhase = state.phase;
}


function playMusic() {

    const music = document.getElementById("music-player");

    music.currentTime = 0;
    music.playbackRate = 1;
    music.muted = false;

    music.play().catch(error => console.warn("Music was blocked:", error));
}


// UPDATE THE CHALLENGE SCREEN
function updateChallengeUI(state) {

    const percent = Math.round(state.pair_live * 100);
    const stars = state.pair_stars;

    document.getElementById("progress-fill").style.width = percent + "%";

    document.getElementById("accuracy-text").textContent = percent + "%";

    document.getElementById("time-left").textContent =
        Math.ceil(state.time_left) + "s";

    document.getElementById("stars-display").textContent =
        "★ ".repeat(stars) + "☆ ".repeat(5 - stars);

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

        document.getElementById("p" + (index + 1) + "-title").textContent =
            "PLAYER " + (index + 1) + " · " + Math.round(player.live * 100) + "%";

        // green = nailing it, yellow = ok, red = off, grey = can't see them
        let quality = "q-idle";

        if (player.present) {
            if (player.now >= state.good_score) quality = "q-good";
            else if (player.now >= 0.5) quality = "q-ok";
            else quality = "q-bad";
        }

        panel.classList.remove("q-good", "q-ok", "q-bad", "q-idle");
        panel.classList.add(quality);

        // a star floats up every time they match a moment well
        const now = performance.now();

        if (player.hits > lastHits[index] && now - lastStarTime[index] > 200) {
            spawnStar(panel, index);
            lastStarTime[index] = now;
        }

        lastHits[index] = player.hits;
    });
}


function spawnStar(panel, side) {

    const star = document.createElement("span");

    star.className = "star-pop " + (side === 0 ? "left" : "right");
    star.textContent = "⭐";

    panel.appendChild(star);

    setTimeout(() => star.remove(), 1300);
}


function resetChallengeUI() {

    document.getElementById("progress-fill").style.width = "0%";
    document.getElementById("accuracy-text").textContent = "0%";
    document.getElementById("time-left").textContent = "";
    document.getElementById("stars-display").textContent = "☆ ☆ ☆ ☆ ☆";
    document.getElementById("challenge-message").textContent = "Team accuracy: 0%";
    document.getElementById("ice-overlay").style.opacity = 0.85;
    document.getElementById("p1-title").textContent = "PLAYER 1";
    document.getElementById("p2-title").textContent = "PLAYER 2";

    ["p1-panel", "p2-panel"].forEach(id => {
        document.getElementById(id).classList.remove("q-good", "q-ok", "q-bad", "q-idle");
    });
}


// SHOW THE RESULT FROM THE SERVER
function showResults(result) {

    const percent = Math.round(result.pair_accuracy * 100);

    const detail =
        "Player 1: " + Math.round(result.accuracies[0] * 100) + "%  •  Player 2: " +
        Math.round(result.accuracies[1] * 100) + "%";

    const photo = API + result.photo_url;

    // Success needs enough stars (the server decides)
    if (result.passed) {

        document.getElementById("ice-overlay").style.opacity = 0;

        document.getElementById("victory-stars").textContent =
            "⭐".repeat(result.stars);

        document.getElementById("final-score").textContent =
            "Your dance accuracy: " + percent + "%";

        document.getElementById("final-detail").textContent = detail;

        document.getElementById("submit-message").textContent =
            result.new_personal_best
                ? "NEW PERSONAL BEST! 🎉 Your score has been saved!"
                : "Your score has been saved! 🎉";

        const victoryPhoto = document.getElementById("victory-photo");
        victoryPhoto.src = photo;
        victoryPhoto.hidden = false;

        goToPage("victory");

        displayLeaderboard();

    } else {

        document.getElementById("failure-score").textContent =
            "Your dance accuracy: " + percent + "%";

        document.getElementById("failure-detail").textContent = detail;

        const failurePhoto = document.getElementById("failure-photo");
        failurePhoto.src = photo;
        failurePhoto.hidden = false;

        goToPage("failure");
    }
}


// DISPLAY LEADERBOARD (kept by the game server)
async function displayLeaderboard() {

    const list = document.getElementById("leaderboard-list");

    list.innerHTML = "";

    let entries = [];

    try {
        const response = await fetch(API + "/api/leaderboard?limit=10");
        entries = await response.json();
    } catch (error) {
        list.innerHTML = "<li>Leaderboard unavailable.</li>";
        return;
    }

    if (entries.length === 0) {

        list.innerHTML = "<li>No scores yet. Be the first!</li>";

        return;
    }

    entries.forEach(entry => {

        const item = document.createElement("li");

        item.textContent =
            entry.pair + " — " + Math.round(entry.accuracy * 100) + "%";

        if (entry.pair.toLowerCase() === teamName.toLowerCase()) {
            item.className = "me";
        }

        list.appendChild(item);

    });
}


// STOP EVERYTHING AND TELL THE SERVER TO GO BACK TO WAITING
function resetEverything() {

    waitingForResult = false;
    roundStarted = false;

    document.getElementById("music-player").pause();

    resetChallengeUI();

    document.getElementById("victory-photo").hidden = true;
    document.getElementById("failure-photo").hidden = true;

    fetch(API + "/api/reset", { method: "POST" }).catch(() => {});
}


// RESTART GAME
function restartGame() {

    resetEverything();

    currentDance = null;

    goToPage("welcome");
}


// TRY AGAIN (back to the practice room, same team name)
function retryGame() {

    resetEverything();

    goToPage("tutorial");
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

    if (!source || !source.naturalWidth) {
        requestAnimationFrame(drawCameraHalves);
        return;
    }

    const width = source.naturalWidth;
    const height = source.naturalHeight;

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

    // Draw LEFT half of webcam
    leftContext.drawImage(
        source,
        0, 0,
        halfWidth, height,
        0, 0,
        halfWidth, height
    );

    // Draw RIGHT half of webcam
    rightContext.drawImage(
        source,
        halfWidth, 0,
        width - halfWidth, height,
        0, 0,
        width - halfWidth, height
    );

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
    const point = index => [landmarks[index][0] * width - offsetX, landmarks[index][1] * height];

    context.strokeStyle = color;
    context.fillStyle = color;
    context.lineWidth = Math.max(3, height / 120);
    context.lineCap = "round";

    BODY.forEach(([a, b]) => {

        if (landmarks[a][2] < 0.3 || landmarks[b][2] < 0.3) return;

        context.beginPath();
        context.moveTo(...point(a));
        context.lineTo(...point(b));
        context.stroke();
    });

    for (let index = 11; index <= 32; index++) {

        if (landmarks[index][2] < 0.3) continue;

        context.beginPath();
        context.arc(...point(index), Math.max(4, height / 100), 0, Math.PI * 2);
        context.fill();
    }
}


connectEvents();
