# Browser-only SnowGlobe deployment

Gameplay now runs in the browser: MediaPipe pose detection, matching, timing, and result photos need no Flask/Render service. Dance MP4/WAV files and the MediaPipe model are loaded from this public GitHub repository; the converted pose references are served by Vercel.

The leaderboard is saved in localStorage on each browser. Scores do not sync between devices or browsers.

## Deploy

1. Commit and push the changed frontend files to GitHub.
2. In Vercel, use the GitHub project with Root Directory set to `frontend`. Leave Framework Preset as Other, Build Command blank, and Output Directory as `.`.
3. Deploy. No environment variables or Render service are required. Camera access works on the HTTPS Vercel URL; allow camera and audio when prompted.

If changing the GitHub repository or branch, update `SNOWGLOBE_MEDIA_BASE` and `SNOWGLOBE_MODEL_URL` in `frontend/config.js`. Keep the repository public so the media URLs can be read by the browser.
