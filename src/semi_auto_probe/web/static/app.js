const els = {
  clock: document.querySelector("#clock"),
  desktopState: document.querySelector("#desktopState"),
  cameraState: document.querySelector("#cameraState"),
  serialState: document.querySelector("#serialState"),
  rateState: document.querySelector("#rateState"),
  feedPill: document.querySelector("#feedPill"),
  positionPill: document.querySelector("#positionPill"),
  cameraSource: document.querySelector("#cameraSource"),
  applySource: document.querySelector("#applySource"),
  token: document.querySelector("#token"),
  saveToken: document.querySelector("#saveToken"),
  posX: document.querySelector("#posX"),
  posY: document.querySelector("#posY"),
  posZ: document.querySelector("#posZ"),
  cameraFeed: document.querySelector("#cameraFeed"),
  cameraEmpty: document.querySelector("#cameraEmpty"),
  log: document.querySelector("#log"),
};

let streamActive = false;
let lastError = "";
let tokenSaveTimer = null;

els.token.value = localStorage.getItem("probeWebToken") || "";
populateFallbackCameraSources();

function token() {
  return localStorage.getItem("probeWebToken") || "";
}

function selectedSource() {
  return els.cameraSource.value || "auto";
}

function cameraUrl() {
  const savedToken = token();
  const query = new URLSearchParams({ ts: String(Date.now()), source: selectedSource() });
  if (savedToken) query.set("token", savedToken);
  return `/camera.mjpg?${query.toString()}`;
}

function log(message, data) {
  const detail = data ? ` ${JSON.stringify(data)}` : "";
  els.log.textContent = `[${new Date().toLocaleTimeString()}] ${message}${detail}\n${els.log.textContent}`;
}

function setTone(element, tone) {
  element.dataset.tone = tone;
}

function populateFallbackCameraSources() {
  if (els.cameraSource.options.length > 0) return;
  const fallbackSources = [
    { id: "auto", label: "Auto", fps: "1/10" },
    { id: "desktop", label: "Microscope feed", fps: 1 },
    { id: "direct:0", label: "ProbeOM", fps: 10 },
    { id: "direct:1", label: "EmbeddedCam", fps: 10 },
    { id: "direct:2", label: "MonitorCam", fps: 10 },
  ];
  for (const source of fallbackSources) {
    const option = document.createElement("option");
    option.value = source.id;
    option.textContent = `${source.label} (${source.fps} FPS)`;
    els.cameraSource.append(option);
  }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token()) headers["X-Access-Token"] = token();
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(data.detail || response.statusText);
  }
  return data;
}

function showStream() {
  if (!streamActive) {
    els.cameraFeed.src = cameraUrl();
    streamActive = true;
  }
  els.cameraFeed.style.display = "block";
  els.cameraEmpty.style.display = "none";
}

function hideStream(title, detail) {
  if (streamActive) {
    els.cameraFeed.removeAttribute("src");
    streamActive = false;
  }
  els.cameraFeed.style.display = "none";
  els.cameraEmpty.style.display = "flex";
  els.cameraEmpty.querySelector("strong").textContent = title;
  els.cameraEmpty.querySelector("span").textContent = detail;
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    lastError = "";

    els.desktopState.textContent = data.desktop_app_running ? "Online" : "Offline";
    setTone(els.desktopState, data.desktop_app_running ? "good" : "bad");

    els.cameraState.textContent = data.camera_running ? (data.camera_source_label || "Live") : "Unavailable";
    setTone(els.cameraState, data.camera_running ? "good" : "warn");

    els.serialState.textContent = data.serial_connected ? data.serial_port : "Disconnected";
    setTone(els.serialState, data.serial_connected ? "good" : "warn");

    els.rateState.textContent = `${Number(data.publisher_fps || (data.desktop_app_running ? 1 : 10)).toFixed(0)} FPS`;
    els.feedPill.textContent = data.camera_running
      ? (data.camera_source === "direct" ? "Direct" : "Live")
      : "Starting";
    setTone(els.feedPill, data.camera_running ? "good" : "warn");

    if (!data.desktop_app_running) {
      showStream();
      if (!data.camera_running) {
        els.cameraState.textContent = "Direct fallback";
        setTone(els.cameraState, "warn");
      }
      els.positionPill.textContent = "Waiting";
      setTone(els.positionPill, "warn");
      return;
    }

    if (!data.camera_running) {
      hideStream("Camera feed is not available", "The local software is online, but no recent frame is being published.");
      els.positionPill.textContent = "Online";
      setTone(els.positionPill, "good");
      return;
    }

    showStream();
  } catch (error) {
    if (error.message !== lastError) {
      log("Status error", { message: error.message });
      lastError = error.message;
    }
    els.desktopState.textContent = "Unauthorized";
    els.cameraState.textContent = "Locked";
    els.serialState.textContent = "-";
    setTone(els.desktopState, "bad");
    setTone(els.cameraState, "bad");
    hideStream("Access token required", "Enter the monitor token to view the remote dashboard.");
  }
}

async function refreshCameraSources() {
  try {
    const data = await api("/api/camera-sources");
    const currentValue = els.cameraSource.value || data.selected || "auto";
    els.cameraSource.innerHTML = "";
    for (const source of data.sources) {
      const option = document.createElement("option");
      option.value = source.id;
      option.textContent = `${source.label} (${source.fps} FPS)`;
      if (!source.available) option.textContent += " - standby";
      els.cameraSource.append(option);
    }
    els.cameraSource.value = [...els.cameraSource.options].some((option) => option.value === currentValue)
      ? currentValue
      : data.selected;
  } catch (error) {
    populateFallbackCameraSources();
    if (token()) {
      log("Camera source error", { message: error.message });
    }
  }
}

async function readPositions() {
  try {
    const data = await api("/api/positions");
    els.posX.textContent = data.positions.X?.position ?? "-";
    els.posY.textContent = data.positions.Y?.position ?? "-";
    els.posZ.textContent = data.positions.Z?.position ?? "-";
    els.positionPill.textContent = "Updated";
    setTone(els.positionPill, "good");
  } catch {
    els.posX.textContent = "-";
    els.posY.textContent = "-";
    els.posZ.textContent = "-";
    els.positionPill.textContent = "No Serial";
    setTone(els.positionPill, "warn");
  }
}

els.saveToken.addEventListener("click", async () => {
  localStorage.setItem("probeWebToken", els.token.value);
  streamActive = false;
  log("Token saved");
  await refreshCameraSources();
  await refreshStatus();
});

els.token.addEventListener("input", () => {
  window.clearTimeout(tokenSaveTimer);
  tokenSaveTimer = window.setTimeout(async () => {
    localStorage.setItem("probeWebToken", els.token.value);
    streamActive = false;
    await refreshCameraSources();
    await refreshStatus();
  }, 300);
});

els.token.addEventListener("keydown", async (event) => {
  if (event.key === "Enter") {
    localStorage.setItem("probeWebToken", els.token.value);
    streamActive = false;
    await refreshCameraSources();
    await refreshStatus();
  }
});

els.applySource.addEventListener("click", async () => {
  const source = selectedSource();
  await api(`/api/camera-source?source=${encodeURIComponent(source)}`, { method: "POST" });
  streamActive = false;
  showStream();
  log("Camera source changed", { source });
  await refreshStatus();
});

setInterval(() => {
  els.clock.textContent = new Date().toLocaleTimeString();
}, 1000);

refreshStatus();
refreshCameraSources();
readPositions();
setInterval(refreshStatus, 5000);
setInterval(refreshCameraSources, 15000);
setInterval(readPositions, 5000);
