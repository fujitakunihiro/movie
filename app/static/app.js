const $ = (id) => document.getElementById(id);
let currentPath = "";
const makeIcon = (pathData) => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", pathData);
  path.setAttribute("fill", "currentColor");
  svg.append(path);
  return svg;
};
const escPath = (p) => p.split("/").map(encodeURIComponent).join("/");
const prettySize = (n) => {
  if (!Number.isFinite(n)) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
};

function renderBreadcrumbs(path) {
  const nav = $("breadcrumbs");
  nav.replaceChildren();
  const parts = path ? path.split("/") : [];
  const addCrumb = (label, target, isLast) => {
    if (nav.childElementCount) {
      const separator = document.createElement("span"); separator.className = "separator"; separator.textContent = "/"; nav.append(separator);
    }
    const button = document.createElement("button"); button.className = "crumb"; button.textContent = label;
    button.setAttribute("aria-current", isLast ? "page" : "false");
    button.addEventListener("click", () => loadDirectory(target)); nav.append(button);
  };
  addCrumb("ホーム", "", parts.length === 0);
  let target = "";
  parts.forEach((part, i) => { target = target ? `${target}/${part}` : part; addCrumb(part, target, i === parts.length - 1); });
}

async function loadDirectory(path = "") {
  currentPath = path;
  $("status").textContent = "読み込み中…";
  $("empty").hidden = true;
  try {
    const response = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
    if (!response.ok) throw new Error(response.status === 401 ? "認証が必要です。ページを再読み込みしてください。" : `読み込みに失敗しました (${response.status})`);
    const data = await response.json();
    currentPath = data.path;
    renderBreadcrumbs(data.path);
    const folderBox = $("folders"), videoBox = $("videos");
    folderBox.replaceChildren(); videoBox.replaceChildren();
    data.folders.forEach((folder) => {
      const card = document.createElement("button"); card.className = "folder-card";
      const icon = document.createElement("span"); icon.className = "folder-icon";
      icon.append(makeIcon("M3 7.5A2.5 2.5 0 0 1 5.5 5h4.1l2 2H18.5A2.5 2.5 0 0 1 21 9.5v7a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 16.5v-9Z"));
      const name = document.createElement("span"); name.className = "folder-name"; name.textContent = folder.name;
      const chevron = document.createElement("span"); chevron.className = "folder-chevron"; chevron.textContent = "›";
      card.append(icon, name, chevron); card.addEventListener("click", () => loadDirectory(folder.path)); folderBox.append(card);
    });
    data.videos.forEach((video) => {
      const row = document.createElement("button"); row.className = "video-row";
      const thumb = document.createElement("span"); thumb.className = "video-thumb";
      const placeholder = document.createElement("span"); placeholder.className = "thumb-placeholder";
      placeholder.append(makeIcon("M8 5.8c0-.77.84-1.25 1.5-.87l9.32 5.36a1 1 0 0 1 0 1.74L9.5 17.39A1 1 0 0 1 8 16.52V5.8Z"));
      const image = document.createElement("img"); image.loading = "lazy"; image.alt = "";
      image.src = "/api/thumbnail?path=" + encodeURIComponent(video.path);
      image.addEventListener("error", () => { image.hidden = true; }, { once: true });
      thumb.append(placeholder, image);
      const name = document.createElement("span"); name.className = "video-name"; name.textContent = video.name;
      const meta = document.createElement("span"); meta.className = "video-meta"; meta.textContent = prettySize(video.size);
      const info = document.createElement("span"); info.className = "video-info"; info.append(name, meta);
      row.append(thumb, info); row.addEventListener("click", () => playVideo(video)); videoBox.append(row);
    });
    $("folders-section").hidden = data.folders.length === 0;
    $("videos-section").hidden = data.videos.length === 0;
    $("folder-count").textContent = data.folders.length ? `${data.folders.length}項目` : "";
    $("video-count").textContent = data.videos.length ? `(${data.videos.length})` : "";
    $("empty").hidden = data.folders.length + data.videos.length !== 0;
    $("status").textContent = "";
  } catch (error) { $("status").textContent = error.message; }
}

let currentHls = null;
let currentHlsInfo = null;
let playGeneration = 0;
let sourceDuration = NaN;
let pendingSeekTarget = null;
let serverAvailableDuration = 0;
let seekPollGeneration = null;
let currentVideoPath = "";
let nativeHlsPlayback = false;

function formatTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainder = whole % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function mediaAvailableDuration() {
  const player = $("player");
  let end = Number.isFinite(player.duration) ? player.duration : 0;
  try {
    if (player.seekable.length) end = Math.max(end, player.seekable.end(player.seekable.length - 1));
  } catch (_) {}
  return end;
}

function updateTransport(previewTime = null) {
  const player = $("player");
  const duration = Number.isFinite(sourceDuration) && sourceDuration > 0
    ? sourceDuration
    : (Number.isFinite(player.duration) ? player.duration : NaN);
  const current = previewTime === null ? player.currentTime : previewTime;
  $("current-time").textContent = formatTime(Number.isFinite(current) ? current : 0);
  $("total-time").textContent = formatTime(duration);
  const seek = $("seek");
  seek.max = Number.isFinite(duration) ? String(duration) : "0";
  seek.disabled = !Number.isFinite(duration) || duration <= 0;
  if (previewTime === null) {
    const visiblePosition = pendingSeekTarget === null ? current : pendingSeekTarget;
    seek.value = String(Math.min(Number.isFinite(visiblePosition) ? visiblePosition : 0, Number.isFinite(duration) ? duration : 0));
  }
}

function syncPendingSeek() {
  if (pendingSeekTarget === null) return;
  const target = pendingSeekTarget;
  const mediaEnd = mediaAvailableDuration();
  const playerReady = mediaEnd >= target - .1;
  const serverReady = serverAvailableDuration >= target - .1;
  const transcodeComplete = currentHlsInfo && currentHlsInfo.complete;
  if (playerReady || (nativeHlsPlayback && serverReady) || transcodeComplete) {
    pendingSeekTarget = null;
    const seekTo = transcodeComplete && Number.isFinite(sourceDuration)
      ? Math.min(target, sourceDuration)
      : (playerReady ? Math.min(target, mediaEnd) : target);
    try { $("player").currentTime = seekTo; } catch (_) {}
    $("status").textContent = "";
    updateTransport();
  }
}

async function pollPendingSeek(generation, videoPath) {
  if (seekPollGeneration === generation) return;
  seekPollGeneration = generation;
  const endpoint = "/api/hls/start?path=" + encodeURIComponent(videoPath);
  try {
    while (pendingSeekTarget !== null && generation === playGeneration) {
      await sleep(1500);
      if (pendingSeekTarget === null || generation !== playGeneration) break;
      try {
        const response = await fetch(endpoint, { cache: "no-store" });
        if (response.ok) {
          const info = await response.json();
          serverAvailableDuration = Math.max(serverAvailableDuration, Number(info.availableDuration) || 0);
          if (currentHlsInfo) currentHlsInfo.complete = Boolean(info.complete);
          if (pendingSeekTarget !== null && serverAvailableDuration < pendingSeekTarget - .1) {
            $("status").textContent = `変換中です。再生可能 ${formatTime(serverAvailableDuration)} / 選択位置 ${formatTime(pendingSeekTarget)}`;
          }
          syncPendingSeek();
        }
      } catch (_) { /* A later poll can still observe the advancing playlist. */ }
    }
  } finally {
    if (seekPollGeneration === generation) seekPollGeneration = null;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitForHls(video, generation) {
  const endpoint = "/api/hls/start?path=" + encodeURIComponent(video.path);
  while (generation === playGeneration) {
    const response = await fetch(endpoint, { cache: "no-store" });
    if (response.status === 200) return response.json();
    if (response.status !== 202) {
      let detail = "動画の準備に失敗しました (" + response.status + ")";
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    $("status").textContent = "スマホ向け画質を準備しています。初回は動画の変換に時間がかかります…";
    await sleep(2000);
  }
  return null;
}

function resetQualityOptions(nativeMode, levels = []) {
  const select = $("quality");
  select.replaceChildren(new Option("自動（通信状況）", "auto"));
  if (nativeMode) {
    [360, 480, 720].forEach((height, index) => select.add(new Option(String(height) + "p", String(index))));
  } else {
    levels.forEach((level, index) => select.add(new Option(String(level.height || "?") + "p", String(index))));
  }
  select.value = "auto";
}

function playVideo(video) {
  const generation = ++playGeneration;
  currentVideoPath = video.path;
  if (currentHls) { currentHls.destroy(); currentHls = null; }
  currentHlsInfo = null;
  sourceDuration = NaN;
  pendingSeekTarget = null;
  serverAvailableDuration = 0;
  nativeHlsPlayback = false;
  updateTransport();
  $("play-pause").disabled = true;
  $("play-pause").textContent = "準備中…";
  $("play-pause").setAttribute("aria-label", "準備中");
  const player = $("player");
  player.pause(); player.removeAttribute("src"); player.load();
  $("player-title").textContent = video.name;
  $("player-panel").hidden = false;
  $("status").textContent = "";
  resetQualityOptions(true);
  const panel = $("player-panel");
  if (isMobilePlayback()) {
    enterMobileFullscreen();
  } else {
    panel.classList.remove("mobile-immersive", "rotate-landscape");
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  waitForHls(video, generation).then((info) => {
    if (!info || generation !== playGeneration) return;
    currentHlsInfo = info;
    sourceDuration = Number.isFinite(info.duration) && info.duration > 0 ? info.duration : NaN;
    serverAvailableDuration = Number(info.availableDuration) || 0;
    updateTransport();
    $("play-pause").disabled = false;
    $("play-pause").textContent = "再生";
    $("play-pause").setAttribute("aria-label", "再生");
    const masterUrl = info.master;
    const nativeHls = player.canPlayType("application/vnd.apple.mpegurl");
    nativeHlsPlayback = Boolean(nativeHls && !(window.Hls && Hls.isSupported()));
    if (window.Hls && Hls.isSupported()) {
      currentHls = new Hls({ capLevelToPlayerSize: true, maxBufferLength: 30 });
      currentHls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (generation !== playGeneration) return;
        resetQualityOptions(false, currentHls.levels);
        player.play().catch(() => {});
        $("status").textContent = "";
      });
      currentHls.on(Hls.Events.ERROR, (_event, data) => {
        if (data.fatal && generation === playGeneration) $("status").textContent = "動画を再生できません。対応コーデックを確認してください。";
      });
      currentHls.loadSource(masterUrl);
      currentHls.attachMedia(player);
    } else if (nativeHls) {
      player.src = masterUrl;
      player.load();
      player.play().catch(() => {});
      $("status").textContent = "";
    } else {
      player.src = "/api/stream?path=" + encodeURIComponent(video.path);
      player.load();
      player.play().catch(() => {});
      $("status").textContent = "このブラウザはHLSに対応していないため、元動画で再生します。";
    }
  }).catch((error) => {
    if (generation === playGeneration) {
      $("play-pause").disabled = true;
      $("play-pause").textContent = "再生できません";
      $("play-pause").setAttribute("aria-label", "再生できません");
      $("status").textContent = error.message;
    }
  });
}

function isMobilePlayback() {
  return window.matchMedia("(max-width: 700px), (pointer: coarse)").matches;
}

function updateLandscapeLayout() {
  const panel = $("player-panel");
  if (!panel.classList.contains("mobile-immersive")) return;
  panel.classList.toggle("rotate-landscape", window.matchMedia("(orientation: portrait)").matches);
}

function enterMobileFullscreen() {
  const panel = $("player-panel");
  panel.classList.add("mobile-immersive");
  document.body.classList.add("mobile-player-open");
  updateLandscapeLayout();
  // Invoke fullscreen synchronously from the tap; orientation locking is allowed after fullscreen.
  let fullscreenRequest = Promise.resolve();
  if (panel.requestFullscreen && document.fullscreenElement !== panel) {
    try { fullscreenRequest = panel.requestFullscreen(); } catch (_) { updateLandscapeLayout(); }
  }
  Promise.resolve(fullscreenRequest).then(() => {
    if (screen.orientation && screen.orientation.lock) {
      screen.orientation.lock("landscape").catch(() => updateLandscapeLayout());
    }
  }).catch(() => updateLandscapeLayout());
}

async function closePlayer() {
  playGeneration++;
  if (currentHls) { currentHls.destroy(); currentHls = null; }
  currentHlsInfo = null;
  sourceDuration = NaN;
  pendingSeekTarget = null;
  serverAvailableDuration = 0;
  nativeHlsPlayback = false;
  const panel = $("player-panel"), player = $("player");
  panel.classList.remove("mobile-immersive", "rotate-landscape");
  document.body.classList.remove("mobile-player-open");
  if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
  if (document.fullscreenElement === panel && document.exitFullscreen) {
    try { await document.exitFullscreen(); } catch (_) {}
  }
  player.pause(); player.removeAttribute("src"); player.load();
  updateTransport();
  panel.hidden = true;
  $("status").textContent = "";
}

window.addEventListener("resize", updateLandscapeLayout);
window.addEventListener("orientationchange", updateLandscapeLayout);
document.addEventListener("fullscreenchange", () => {
  const panel = $("player-panel");
  if (panel.classList.contains("mobile-immersive") && document.fullscreenElement !== panel) {
    panel.classList.remove("mobile-immersive", "rotate-landscape");
    document.body.classList.remove("mobile-player-open");
    if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
  }
});

$("quality").addEventListener("change", (event) => {
  const value = event.target.value;
  if (currentHls) {
    currentHls.currentLevel = value === "auto" ? -1 : Number(value);
    return;
  }
  if (!currentHlsInfo) return;
  const player = $("player");
  const time = player.currentTime;
  const wasPlaying = !player.paused;
  const url = value === "auto" ? currentHlsInfo.master : "/api/hls/" + currentHlsInfo.id + "/v" + value + "/index.m3u8";
  player.src = url; player.load();
  player.addEventListener("loadedmetadata", () => {
    if (Number.isFinite(time)) player.currentTime = time;
    if (wasPlaying) player.play().catch(() => {});
  }, { once: true });
});
$("play-pause").addEventListener("click", () => {
  const player = $("player");
  if (player.paused) player.play().catch(() => {});
  else player.pause();
});
$("seek").addEventListener("input", (event) => updateTransport(Number(event.target.value)));
$("seek").addEventListener("change", (event) => {
  const target = Number(event.target.value);
  const player = $("player");
  const ready = mediaAvailableDuration() >= target - .1;
  if (currentHlsInfo && !currentHlsInfo.complete && !ready) {
    pendingSeekTarget = target;
    $("status").textContent = `変換中です。再生可能 ${formatTime(serverAvailableDuration)} / 選択位置 ${formatTime(target)}`;
    updateTransport();
    pollPendingSeek(playGeneration, currentVideoPath);
    return;
  }
  pendingSeekTarget = null;
  try { player.currentTime = target; } catch (_) {}
  updateTransport();
});
$("volume").addEventListener("input", (event) => { $("player").volume = Number(event.target.value); });
$("player").addEventListener("timeupdate", () => { updateTransport(); syncPendingSeek(); });
$("player").addEventListener("durationchange", () => { updateTransport(); syncPendingSeek(); });
$("player").addEventListener("loadedmetadata", () => updateTransport());
$("player").addEventListener("play", () => { $("play-pause").disabled = false; $("play-pause").textContent = "一時停止"; $("play-pause").setAttribute("aria-label", "一時停止"); });
$("player").addEventListener("pause", () => { $("play-pause").disabled = false; $("play-pause").textContent = "再生"; $("play-pause").setAttribute("aria-label", "再生"); });
$("refresh").addEventListener("click", () => loadDirectory(currentPath));
$("speed").addEventListener("change", (event) => { $("player").playbackRate = Number(event.target.value); });
$("fullscreen").addEventListener("click", async () => {
  if (isMobilePlayback()) {
    enterMobileFullscreen();
    return;
  }
  const player = $("player");
  try {
    if (player.requestFullscreen) await player.requestFullscreen();
    else if (player.webkitEnterFullscreen) player.webkitEnterFullscreen();
  } catch (_) { /* Browser denied fullscreen; native video controls remain available. */ }
});
$("close-player").addEventListener("click", closePlayer);
loadDirectory();


