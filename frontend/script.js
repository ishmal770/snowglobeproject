
let countdownTimer;
let currentScore = 0;

// SWITCH BETWEEN SCREENS
function goToPage(pageId) {

    clearInterval(countdownTimer);

    document.querySelectorAll(".screen").forEach(page => {
        page.classList.remove("active");
    });

    document.getElementById(pageId).classList.add("active");
    // Start camera when entering practice room
    if (pageId === "tutorial") {
        startSharedCamera();
    }

    // Start splitting the camera when dance begins
    if (pageId === "challenge") {
        drawCameraHalves();
    }

    window.scrollTo(0, 0);

    // Automatically start countdown
    if (pageId === "countdown") {
        startCountdown();
    }

    if (pageId === "challenge") {
    startCameras();
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


// CALCULATE STARS BASED ON ACCURACY
function calculateStars(score) {

    if (score >= 90) return 3;
    if (score >= 70) return 2;
    if (score >= 50) return 1;

    return 0;
}


// FINISH DANCE CHALLENGE
function finishChallenge(score) {

    currentScore = score;

    const stars = calculateStars(score);

    // Update progress bar
    document.getElementById("progress-fill").style.width =
        score + "%";

    document.getElementById("accuracy-text").textContent =
        score + "%";

    // Update stars
    document.getElementById("stars-display").textContent =
        "★ ".repeat(stars) + "☆ ".repeat(3 - stars);

    // Success requires at least 2 stars
    if (stars >= 2) {

        document.getElementById("victory-stars").textContent =
            "⭐".repeat(stars);

        document.getElementById("final-score").textContent =
            "Your dance accuracy: " + score + "%";

        document.getElementById("submit-message").textContent = "";

        goToPage("victory");

        displayLeaderboard();

    } else {

        document.getElementById("failure-score").textContent =
            "Your dance accuracy: " + score + "%";

        goToPage("failure");
    }
}


// GET LEADERBOARD FROM BROWSER STORAGE
function getLeaderboard() {

    return JSON.parse(
        localStorage.getItem("defrostLeaderboard") || "[]"
    );
}


// SUBMIT SCORE TO LEADERBOARD
function saveScore() {

    const nameInput = document.getElementById("player-name");

    const name = nameInput.value.trim();

    if (!name) {
        document.getElementById("submit-message").textContent =
            "Please enter your name first!";
        return;
    }

    const leaderboard = getLeaderboard();

    leaderboard.push({
        name: name,
        score: currentScore
    });

    // Highest scores first
    leaderboard.sort((a, b) => b.score - a.score);

    // Keep the top 10
    const topScores = leaderboard.slice(0, 10);

    localStorage.setItem(
        "defrostLeaderboard",
        JSON.stringify(topScores)
    );

    document.getElementById("submit-message").textContent =
        "Your score has been saved! 🎉";

    nameInput.value = "";

    displayLeaderboard();
}


// DISPLAY LEADERBOARD
function displayLeaderboard() {

    const leaderboard = getLeaderboard();

    const list = document.getElementById("leaderboard-list");

    list.innerHTML = "";

    if (leaderboard.length === 0) {

        list.innerHTML = "<li>No scores yet. Be the first!</li>";

        return;
    }

    leaderboard.forEach((entry, index) => {

        const item = document.createElement("li");

        item.textContent =
            entry.name + " — " + entry.score + "%";

        list.appendChild(item);

    });
}


// RESTART GAME
function restartGame() {

    currentScore = 0;

    document.getElementById("progress-fill").style.width = "0%";

    document.getElementById("accuracy-text").textContent = "0%";

    document.getElementById("stars-display").textContent =
        "☆ ☆ ☆";

    goToPage("welcome");
}


// SHARED CAMERA STREAM
let sharedCameraStream = null;


// START ONE CAMERA FOR BOTH PLAYERS
async function startSharedCamera() {

    const source = document.getElementById("camera-source");
    const preview = document.getElementById("practice-camera");
    const status = document.getElementById("camera-status");

    try {

        // Only request the camera once
        if (!sharedCameraStream) {

            sharedCameraStream =
                await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: "user"
                    },
                    audio: false
                });

        }

        // Use the SAME stream in both video elements
        source.srcObject = sharedCameraStream;
        preview.srcObject = sharedCameraStream;

        await source.play();
        await preview.play();

        status.textContent =
            "Camera connected! Both players can practice.";

    } catch (error) {

        console.error("Camera error:", error);

        status.textContent =
            "Camera unavailable. Check your browser permissions.";

    }
}


// SPLIT THE CAMERA INTO LEFT AND RIGHT HALVES
function drawCameraHalves() {

    // Stop drawing when we leave the challenge
    if (!document.getElementById("challenge").classList.contains("active")) {
        return;
    }

    const source = document.getElementById("camera-source");

    const leftCanvas = document.getElementById("player1-canvas");
    const rightCanvas = document.getElementById("player2-canvas");

    if (!source || source.videoWidth === 0) {
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

    // Keep updating the live camera feed
    requestAnimationFrame(drawCameraHalves);
}



