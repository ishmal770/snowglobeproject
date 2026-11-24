let landmarker = null;

self.onmessage = async ({ data }) => {
  if (data?.type === "init") {
    try {
      const vision = await import(`${data.cdn}/vision_bundle.mjs`);
      const files = await vision.FilesetResolver.forVisionTasks(`${data.cdn}/wasm`);
      const create = delegate => vision.PoseLandmarker.createFromOptions(files, {
        baseOptions: { modelAssetPath: data.modelUrl, delegate },
        runningMode: "VIDEO",
        numPoses: 2
      });
      let delegate = "GPU";
      try {
        landmarker = await create("GPU");
      } catch (gpuError) {
        console.warn("Pose worker GPU delegate unavailable; using CPU:", gpuError);
        delegate = "CPU";
        landmarker = await create("CPU");
      }
      self.postMessage({ type: "ready", delegate });
    } catch (error) {
      self.postMessage({ type: "init-error", message: error?.message || String(error) });
    }
    return;
  }

  if (data?.type !== "frame" || !landmarker) return;
  try {
    const result = landmarker.detectForVideo(data.bitmap, data.timestamp);
    self.postMessage({
      type: "result",
      landmarks: result.landmarks || [],
      width: data.width,
      height: data.height,
      loopToken: data.loopToken
    });
  } catch (error) {
    self.postMessage({ type: "frame-error", message: error?.message || String(error) });
  } finally {
    data.bitmap?.close?.();
  }
};
