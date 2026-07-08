// GitHub Pages build — no backend, so replay the captured sequence.
// Connect a deployed backend instead with ?api=wss://your-host
window.MF_CONFIG = {
  forceReplay: true,
  replayUrl: "data/replay.json",
  apiBase: null,
};
