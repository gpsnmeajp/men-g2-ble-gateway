const statusGrid = document.getElementById("statusGrid");
const eventLog = document.getElementById("eventLog");
const MAX_LOG_LINES = 200;
const API_KEY_STORAGE_KEY = "g2GatewayApiKey";

let websocketConnection = null;
let reconnectTimer = null;

function getApiKey() {
  return window.localStorage.getItem(API_KEY_STORAGE_KEY) || "";
}

function authHeaders() {
  const apiKey = getApiKey();
  return apiKey ? { "X-API-Key": apiKey } : {};
}

function buildWebSocketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(`${protocol}//${window.location.host}/ws`);
  const apiKey = getApiKey();
  if (apiKey) {
    url.searchParams.set("api_key", apiKey);
  }
  return url.toString();
}

function resetWebSocket() {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (websocketConnection !== null) {
    const websocket = websocketConnection;
    websocketConnection = null;
    websocket.close();
  }
  connectWebSocket();
}

function setResult(id, message, isError = false) {
  const node = document.getElementById(id);
  node.textContent = message;
  node.dataset.error = isError ? "true" : "false";
}

function appendEvent(event) {
  const line = JSON.stringify(event);
  const lines = eventLog.textContent ? eventLog.textContent.trimEnd().split("\n") : [];
  lines.push(line);
  if (lines.length > MAX_LOG_LINES) {
    lines.splice(0, lines.length - MAX_LOG_LINES);
  }
  eventLog.textContent = lines.join("\n") + "\n";
  eventLog.scrollTop = eventLog.scrollHeight;
}

document.getElementById("apiKeyInput").value = getApiKey();

document.getElementById("apiKeyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const apiKey = document.getElementById("apiKeyInput").value.trim();
  if (apiKey) {
    window.localStorage.setItem(API_KEY_STORAGE_KEY, apiKey);
    setResult("apiKeyResult", "Saved.");
  } else {
    window.localStorage.removeItem(API_KEY_STORAGE_KEY);
    setResult("apiKeyResult", "Cleared.");
  }
  fetchStatus().catch((error) => appendEvent({ kind: "system.error", data: { message: String(error) } }));
  resetWebSocket();
});

document.getElementById("clearApiKeyButton").addEventListener("click", () => {
  document.getElementById("apiKeyInput").value = "";
  window.localStorage.removeItem(API_KEY_STORAGE_KEY);
  setResult("apiKeyResult", "Cleared.");
  fetchStatus().catch((error) => appendEvent({ kind: "system.error", data: { message: String(error) } }));
  resetWebSocket();
});

async function withLoading(buttons, fn) {
  buttons.forEach((b) => { b.disabled = true; });
  try {
    await fn();
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

function updateMicButtons(enabled) {
  document.getElementById("micOnButton").classList.toggle("active-state", enabled);
  document.getElementById("micOffButton").classList.toggle("active-state", !enabled);
}

function renderStatus(payload) {
  const server = payload.server || {};
  const glasses = payload.glasses || {};
  const left = glasses.left || {};
  const right = glasses.right || {};

  const rows = [
    ["Server", `${server.host || "-"}:${server.port || "-"}`],
    ["Phase", glasses.phase || "-"],
    ["Ready", glasses.ready ? "yes" : "no"],
    ["Serial", glasses.last_serial_number || "-"],
    ["Left Address", left.address || "-"],
    ["Right Address", right.address || "-"],
    ["Microphone", `Current: ${glasses.mic_enabled ? "on" : "off"} / Target: ${glasses.target_mic_enabled ? "on" : "off"}`],
    ["Battery", glasses.battery_level >= 0 ? `${glasses.battery_level}%` : "-"],
    ["Charging", glasses.charging ? "yes" : "no"],
    ["Firmware", glasses.firmware_version || "-"],
    ["Error", glasses.last_error || "-"],
    ["Pairing", glasses.pairing_warning || "-"],
    ["Gesture", glasses.last_gesture || "-"],
  ];

  statusGrid.innerHTML = rows.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("");
  updateMicButtons(!!glasses.mic_enabled);
}

async function fetchStatus() {
  const response = await fetch("/api/status", { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`status request failed: ${response.status}`);
  }
  const payload = await response.json();
  renderStatus(payload);
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(text);
  }
  return text;
}

function getNumberInputValue(id, fallback, minimum = null) {
  const parsed = Number(document.getElementById(id).value);
  let value = Number.isFinite(parsed) ? parsed : fallback;
  if (minimum !== null) {
    value = Math.max(minimum, value);
  }
  return value;
}

function buildLayoutTextElement() {
  const text = document.getElementById("composeText").value.replace(/\r\n/g, "\n");
  if (!text.trim()) {
    return null;
  }

  return {
    type: "text",
    text,
    x: getNumberInputValue("composeTextX", 0, 0),
    y: getNumberInputValue("composeTextY", 0, 0),
    width: getNumberInputValue("composeTextWidth", 576, 1),
    height: getNumberInputValue("composeTextHeight", 72, 1),
    padding: getNumberInputValue("composeTextPadding", 8, 0),
  };
}

document.getElementById("textForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = document.getElementById("textInput").value.trim();
  if (!text) {
    setResult("textResult", "Please enter text.", true);
    return;
  }
  const btn = event.target.querySelector('button[type="submit"]');
  await withLoading([btn], async () => {
    try {
      const result = await postJson("/api/display", { text });
      setResult("textResult", result);
    } catch (error) {
      setResult("textResult", String(error), true);
    }
  });
});

document.getElementById("clearButton").addEventListener("click", async (event) => {
  await withLoading([event.currentTarget], async () => {
    try {
      const result = await postJson("/api/display", { clear: true });
      setResult("textResult", result);
    } catch (error) {
      setResult("textResult", String(error), true);
    }
  });
});

let previewDebounceTimer = null;

function buildProcessedCanvas(file, targetWidth, targetHeight, keepAspect, gamma) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      const canvas = document.createElement("canvas");
      canvas.width = targetWidth;
      canvas.height = targetHeight;
      const ctx = canvas.getContext("2d");
      if (keepAspect) {
        const scale = Math.min(targetWidth / img.naturalWidth, targetHeight / img.naturalHeight);
        const drawW = img.naturalWidth * scale;
        const drawH = img.naturalHeight * scale;
        ctx.drawImage(img, (targetWidth - drawW) / 2, (targetHeight - drawH) / 2, drawW, drawH);
      } else {
        ctx.drawImage(img, 0, 0, targetWidth, targetHeight);
      }

      // グレースケール変換 + ガンマ補正
      const imageData = ctx.getImageData(0, 0, targetWidth, targetHeight);
      const data = imageData.data;
      const gray = new Float32Array(targetWidth * targetHeight);
      for (let i = 0; i < gray.length; i++) {
        const lum = 0.299 * (data[i * 4] / 255)
                  + 0.587 * (data[i * 4 + 1] / 255)
                  + 0.114 * (data[i * 4 + 2] / 255);
        gray[i] = Math.pow(Math.max(0, lum), gamma) * 255;
      }

      // Floyd-Steinberg ディザリング（4 bit = 16 階調）
      const step = 255 / 15;
      for (let y = 0; y < targetHeight; y++) {
        for (let x = 0; x < targetWidth; x++) {
          const idx = y * targetWidth + x;
          const oldVal = Math.max(0, Math.min(255, gray[idx]));
          const quantized = Math.round(oldVal / step) * step;
          const err = oldVal - quantized;
          gray[idx] = quantized;
          if (x + 1 < targetWidth) gray[idx + 1] += err * 7 / 16;
          if (y + 1 < targetHeight) {
            if (x > 0) gray[idx + targetWidth - 1] += err * 3 / 16;
            gray[idx + targetWidth] += err * 5 / 16;
            if (x + 1 < targetWidth) gray[idx + targetWidth + 1] += err * 1 / 16;
          }
        }
      }

      for (let i = 0; i < gray.length; i++) {
        const v = Math.max(0, Math.min(255, Math.round(gray[i])));
        data[i * 4] = v;
        data[i * 4 + 1] = v;
        data[i * 4 + 2] = v;
        data[i * 4 + 3] = 255;
      }
      ctx.putImageData(imageData, 0, 0);
      resolve(canvas);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Failed to load the image."));
    };
    img.src = url;
  });
}

async function updateImagePreview() {
  const fileInput = document.getElementById("imageFile");
  const previewEl = document.getElementById("imagePreview");
  if (!fileInput.files || fileInput.files.length === 0) {
    previewEl.style.display = "none";
    return;
  }
  const targetWidth = Math.max(1, Number(document.getElementById("imageWidth").value) || 288);
  const targetHeight = Math.max(1, Number(document.getElementById("imageHeight").value) || 144);
  const keepAspect = document.getElementById("keepAspect").checked;
  const gamma = Math.max(0.01, Number(document.getElementById("gammaValue").value) || 1.0);
  try {
    const canvas = await buildProcessedCanvas(fileInput.files[0], targetWidth, targetHeight, keepAspect, gamma);
    const previewCtx = previewEl.getContext("2d");
    previewEl.width = canvas.width;
    previewEl.height = canvas.height;
    previewCtx.drawImage(canvas, 0, 0);
    previewEl.style.display = "block";
  } catch {
    previewEl.style.display = "none";
  }
}

function schedulePreview() {
  clearTimeout(previewDebounceTimer);
  previewDebounceTimer = setTimeout(updateImagePreview, 200);
}

async function buildLayoutPayloadFromForm() {
  const elements = [];
  const textElement = buildLayoutTextElement();
  if (textElement) {
    elements.push(textElement);
  }

  const fileInput = document.getElementById("imageFile");
  if (fileInput.files && fileInput.files.length > 0) {
    const targetWidth = getNumberInputValue("imageWidth", 288, 1);
    const targetHeight = getNumberInputValue("imageHeight", 144, 1);
    const keepAspect = document.getElementById("keepAspect").checked;
    const gamma = Math.max(0.01, getNumberInputValue("gammaValue", 1.0));
    const canvas = await buildProcessedCanvas(fileInput.files[0], targetWidth, targetHeight, keepAspect, gamma);
    const imageBase64 = canvas.toDataURL("image/png").split(",")[1];
    elements.push({
      type: "image",
      image_base64: imageBase64,
      x: getNumberInputValue("imageX", 0, 0),
      y: getNumberInputValue("imageY", 0, 0),
      width: targetWidth,
      height: targetHeight,
    });
  }

  if (!elements.length) {
    throw new Error("Please enter text or choose an image.");
  }

  return { elements };
}

document.getElementById("imageForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const btn = event.target.querySelector('button[type="submit"]');
  await withLoading([btn], async () => {
    try {
      const payload = await buildLayoutPayloadFromForm();
      const result = await postJson("/api/display", payload);
      setResult("imageResult", result);
    } catch (error) {
      setResult("imageResult", String(error), true);
    }
  });
});

document.getElementById("gammaSlider").addEventListener("input", () => {
  document.getElementById("gammaValue").value = document.getElementById("gammaSlider").value;
  schedulePreview();
});

document.getElementById("gammaValue").addEventListener("input", () => {
  document.getElementById("gammaSlider").value = document.getElementById("gammaValue").value;
  schedulePreview();
});

["imageFile", "imageWidth", "imageHeight", "keepAspect"].forEach((id) => {
  document.getElementById(id).addEventListener("change", schedulePreview);
});

document.getElementById("jsonPayload").addEventListener("input", (event) => {
  try {
    JSON.parse(event.target.value);
    event.target.setCustomValidity("");
  } catch {
    event.target.setCustomValidity("Invalid JSON.");
  }
});

document.getElementById("jsonForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const btn = event.target.querySelector('button[type="submit"]');
  await withLoading([btn], async () => {
    try {
      const payload = JSON.parse(document.getElementById("jsonPayload").value);
      const result = await postJson("/api/display", payload);
      setResult("jsonResult", result);
    } catch (error) {
      setResult("jsonResult", String(error), true);
    }
  });
});

document.getElementById("micOnButton").addEventListener("click", async (event) => {
  const otherBtn = document.getElementById("micOffButton");
  await withLoading([event.currentTarget, otherBtn], async () => {
    try {
      const result = await postJson("/api/mic", { enabled: true });
      setResult("micResult", result);
    } catch (error) {
      setResult("micResult", String(error), true);
    }
  });
});

document.getElementById("micOffButton").addEventListener("click", async (event) => {
  const otherBtn = document.getElementById("micOnButton");
  await withLoading([event.currentTarget, otherBtn], async () => {
    try {
      const result = await postJson("/api/mic", { enabled: false });
      setResult("micResult", result);
    } catch (error) {
      setResult("micResult", String(error), true);
    }
  });
});

["touchSwipeUp", "touchSwipeDown", "touchTap", "touchDoubleTap"].forEach((id) => {
  document.getElementById(id).addEventListener("click", async (event) => {
    const gesture = event.currentTarget.dataset.gesture;
    await withLoading([event.currentTarget], async () => {
      try {
        const result = await postJson("/api/touch", { gesture });
        setResult("touchResult", result);
      } catch (error) {
        setResult("touchResult", String(error), true);
      }
    });
  });
});

document.getElementById("clearLogButton").addEventListener("click", () => {
  eventLog.textContent = "";
});

function connectWebSocket() {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  const websocket = new WebSocket(buildWebSocketUrl());
  websocketConnection = websocket;
  websocket.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      appendEvent(payload);
      if (payload.kind === "status.snapshot") {
        renderStatus(payload.data);
      }
    } catch (error) {
      appendEvent({ kind: "system.error", data: { message: String(error) } });
    }
  };
  websocket.onclose = () => {
    appendEvent({ kind: "connection.state", data: { phase: "ws_closed" } });
    if (websocketConnection === websocket) {
      reconnectTimer = window.setTimeout(connectWebSocket, 2000);
    }
  };
}

window.setInterval(() => {
  fetchStatus().catch((error) => appendEvent({ kind: "system.error", data: { message: String(error) } }));
}, 5000);

fetchStatus().catch((error) => appendEvent({ kind: "system.error", data: { message: String(error) } }));
connectWebSocket();
