"use strict";

const screenImage = document.getElementById("screen");
const screenWrap = document.getElementById("screen-wrap");
const cursorEl = document.getElementById("cursor");
const status = document.getElementById("status");
const latencyEl = document.getElementById("latency");
const pad = document.getElementById("touchpad");
const textInput = document.getElementById("text-input");
const zoomLabel = document.getElementById("zoom-label");
const monitorSelect = document.getElementById("monitor-select");
const fileNameInput = document.getElementById("file-name");
const fileInput = document.getElementById("file-input");
const systemStatus = document.getElementById("system-status");
const toastEl = document.getElementById("toast");

const SCROLL_KEYS = new Set([
  " ",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "PageUp",
  "PageDown",
  "Home",
  "End",
]);

const BUTTONS = ["left", "middle", "right"];
const SENSITIVITY = 1.5;
const TAP_MS = 300;
const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
const ZOOM_FACTOR = 1.5;

/* Chrome reports wheel deltas in pixels (~100 per notch); Firefox may use
   lines. The server expects signed notches, positive meaning "scroll up". */
const WHEEL_PIXELS_PER_NOTCH = 100;
const WHEEL_LINE_PIXELS = 16;

const RETRY_BASE_MS = 500;
const RETRY_MAX_MS = 10000;
const PING_INTERVAL_MS = 5000;

const PRESETS = {
  low: { fps: 5, quality: 45, scale: 0.5 },
  medium: { fps: 10, quality: 70, scale: 0.75 },
  high: { fps: 15, quality: 85, scale: 1 },
};

let cursor = { x: 0.5, y: 0.5 };
let zoom = 1;
let pan = { x: 0, y: 0 };
let typingDelay = 0;

/* Every control that handles Enter/Space itself registers here, so the
   document-level keyboard handler knows which keypresses were already
   consumed locally and must not also be forwarded to the remote. Membership
   is what matters, not the element's ARIA attributes. */
const activationControls = new Set();

/* Fetched once at startup and sent with every mutating request. */
let csrfToken = "";
let csrfReady = null;

/* A 401 from anywhere means the session is gone: send the user back to the
   login page rather than letting the UI quietly stop responding. */
function guard(response) {
  if (response.status === 401) {
    location.href = "/login";
  }
  return response;
}

async function request(path, options) {
  const opts = Object.assign({}, options);
  const method = (opts.method || "GET").toUpperCase();
  const mutating = method !== "GET" && method !== "HEAD";
  if (mutating) {
    // The token arrives asynchronously, so a request made before it lands
    // waits for it rather than being rejected.
    if (!csrfToken && csrfReady) {
      await csrfReady;
    }
    opts.headers = Object.assign({}, opts.headers, { "X-CSRF-Token": csrfToken });
  }
  const response = await fetch(path, opts);
  if (mutating && response.status === 403 && (await refreshCsrf())) {
    // The session was replaced underneath us (a login in another tab), so the
    // token is stale. Re-send once with the current one.
    opts.headers = Object.assign({}, opts.headers, { "X-CSRF-Token": csrfToken });
    return guard(await fetch(path, opts));
  }
  return guard(response);
}

async function refreshCsrf() {
  try {
    const response = await fetch("/csrf", { cache: "no-store" });
    if (!response.ok) {
      return false;
    }
    const data = await response.json();
    if (!data.token) {
      return false;
    }
    csrfToken = data.token;
    return true;
  } catch (error) {
    return false;
  }
}

function post(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function clamp(value) {
  return Math.min(1, Math.max(0, value));
}

/* ------------------------------------------------------------------ toast */

let toastTimer = 0;

function showToast(message) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.hidden = true;
  }, 2500);
}

function setSystemStatus(message) {
  systemStatus.textContent = message;
}

/* --------------------------------------------------------------- geometry
   #screen is absolutely positioned and its rectangle is driven entirely from
   here, so this file is the single source of truth for the mapping between
   viewport pixels and remote-screen fractions. fitBox() is the unzoomed,
   centred picture; visibleBox() applies zoom and the camera-follow pan. */

function fitBox() {
  const width = screenWrap.clientWidth;
  const height = screenWrap.clientHeight;
  const naturalWidth = screenImage.naturalWidth || 1;
  const naturalHeight = screenImage.naturalHeight || 1;
  const scale = Math.min(width / naturalWidth, height / naturalHeight);
  const w = naturalWidth * scale;
  const h = naturalHeight * scale;
  return { x: (width - w) / 2, y: (height - h) / 2, w: w, h: h };
}

function visibleBox() {
  const box = fitBox();
  const cx = screenWrap.clientWidth / 2;
  const cy = screenWrap.clientHeight / 2;
  return {
    x: cx + (box.x - cx) * zoom + pan.x,
    y: cy + (box.y - cy) * zoom + pan.y,
    w: box.w * zoom,
    h: box.h * zoom,
  };
}

/* Clamp so the zoomed picture never pulls away from the viewport edges. When
   it is smaller than the viewport on an axis there is nothing to cover, so
   centre it on that axis and accept the letterboxing.

   The picture occupies [base + pan, base + pan + size], so covering the
   viewport means base + pan <= 0 and base + pan + size >= wrap, which bounds
   pan to [wrap - size - base, -base]. The upper bound is -base, not 0: base is
   negative once the picture is zoomed past the viewport, so -base is positive
   and it is exactly the pan that parks the near edge against the near viewport
   edge. Clamping at 0 instead threw away every positive pan, which is the half
   that follows the cursor into the left/upper half of the remote screen. */
function clampPan(next, factor) {
  const box = fitBox();
  const wrapWidth = screenWrap.clientWidth;
  const wrapHeight = screenWrap.clientHeight;
  const cx = wrapWidth / 2;
  const cy = wrapHeight / 2;
  const w = box.w * factor;
  const h = box.h * factor;
  const baseX = cx + (box.x - cx) * factor;
  const baseY = cy + (box.y - cy) * factor;
  return {
    x:
      w <= wrapWidth
        ? (wrapWidth - w) / 2 - baseX
        : Math.min(-baseX, Math.max(wrapWidth - w - baseX, next.x)),
    y:
      h <= wrapHeight
        ? (wrapHeight - h) / 2 - baseY
        : Math.min(-baseY, Math.max(wrapHeight - h - baseY, next.y)),
  };
}

/* The pan that puts the given remote point at the centre of the viewport. */
function panFor(point, factor) {
  const box = fitBox();
  const cx = screenWrap.clientWidth / 2;
  const cy = screenWrap.clientHeight / 2;
  const targetX = box.x + point.x * box.w;
  const targetY = box.y + point.y * box.h;
  const scaledX = cx + (targetX - cx) * factor;
  const scaledY = cy + (targetY - cy) * factor;
  return clampPan({ x: cx - scaledX, y: cy - scaledY }, factor);
}

function layout() {
  if (!screenImage.naturalWidth) {
    // Nothing has decoded yet, so fitBox() would fall back to a 1x1 picture
    // and place everything wrongly for the first frame. Hide the pointer and
    // wait for the first load instead.
    cursorEl.style.visibility = "hidden";
    return;
  }
  cursorEl.style.visibility = "";
  const box = visibleBox();
  screenImage.style.left = box.x + "px";
  screenImage.style.top = box.y + "px";
  screenImage.style.width = box.w + "px";
  screenImage.style.height = box.h + "px";
  cursorEl.style.left = box.x + cursor.x * box.w + "px";
  cursorEl.style.top = box.y + cursor.y * box.h + "px";
}

function normalized(event) {
  const rect = screenWrap.getBoundingClientRect();
  const box = visibleBox();
  return {
    x: clamp((event.clientX - rect.left - box.x) / box.w),
    y: clamp((event.clientY - rect.top - box.y) / box.h),
  };
}

function setZoom(next) {
  zoom = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
  // Anchor the zoom on the remote cursor, which is what makes zooming in feel
  // like it zooms into the pointer and keeps the camera following it.
  pan = panFor(cursor, zoom);
  zoomLabel.textContent = Math.round(zoom * 100) + "%";
  layout();
}

/* ------------------------------------------------------- cursor + coalescing
   Pointer moves arrive far faster than the stream can use, so collapse them
   into at most one request per animation frame. */

let pendingMove = null;
let moveFrame = 0;

function flushMove() {
  moveFrame = 0;
  if (pendingMove) {
    const point = pendingMove;
    pendingMove = null;
    post("/mouse", { event: "move", x: point.x, y: point.y });
  }
}

/* followCursor is true only for the touchpad, where the motion is relative to
   the pointer itself. On the image the pointer position *is* derived from the
   pan, so following would slide the picture out from under the mouse. */
function moveCursor(point, followCursor) {
  cursor = point;
  if (followCursor && zoom > 1) {
    pan = panFor(cursor, zoom);
  }
  layout();
  pendingMove = point;
  if (!moveFrame) {
    moveFrame = requestAnimationFrame(flushMove);
  }
}

/* A sub-notch remainder is carried until it adds up to a whole notch, but the
   mouse wheel and the touchpad gestures are different input sources: mixing
   them lets a leftover from one produce a stray notch in the other. */
const remainder = {
  wheel: { x: 0, y: 0 },
  touch: { x: 0, y: 0 },
};

function accumulateNotches(source, pixels, channel) {
  const state = remainder[source];
  state[channel] += pixels;
  const notches = Math.trunc(state[channel] / WHEEL_PIXELS_PER_NOTCH);
  state[channel] -= notches * WHEEL_PIXELS_PER_NOTCH;
  return notches;
}

function scrollRemote(dx, dy) {
  if (!dx && !dy) {
    return;
  }
  post("/mouse", { event: "scroll", x: cursor.x, y: cursor.y, dx: dx, dy: dy });
}

function clickAtCursor(button) {
  post("/mouse", { event: "click", x: cursor.x, y: cursor.y, button: button });
}

/* ------------------------------------------------------------- screen stream */

let streamAttempts = 0;
let streamTimer = 0;

function openStream(delay) {
  clearTimeout(streamTimer);
  streamTimer = setTimeout(() => {
    screenImage.src = "/screen?t=" + Date.now();
  }, delay);
}

screenImage.addEventListener("load", () => {
  clearTimeout(streamTimer);
  streamAttempts = 0;
  status.textContent = "connected";
  layout();
});

screenImage.addEventListener("error", () => {
  status.textContent = "reconnecting";
  const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * Math.pow(2, streamAttempts));
  streamAttempts += 1;
  openStream(delay);
  // An <img> cannot report why it failed, so a dead tunnel and an expired
  // session look identical. Ask a JSON endpoint: a dead tunnel fails the
  // request and is ignored, while an expired session answers 401 and guard()
  // takes the user to the login page.
  request("/ping", { cache: "no-store" }).catch(() => {});
});

/* Screen area: desktop mouse control over the image. */
screenWrap.addEventListener("mousemove", (event) => {
  moveCursor(normalized(event), false);
});

screenWrap.addEventListener("mousedown", (event) => {
  const button = BUTTONS[event.button];
  if (!button) {
    return;
  }
  event.preventDefault();
  const point = normalized(event);
  cursor = point;
  layout();
  clickAtCursor(button);
});

screenWrap.addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    const pixels = event.deltaMode === 1 ? -event.deltaY * WHEEL_LINE_PIXELS : -event.deltaY;
    const notches = accumulateNotches("wheel", pixels, "y");
    if (!notches) {
      return;
    }
    const point = normalized(event);
    cursor = point;
    layout();
    scrollRemote(0, notches);
  },
  { passive: false }
);

screenWrap.addEventListener("contextmenu", (event) => {
  event.preventDefault();
});

/* ------------------------------------------------------------------ keyboard
   Every held key is tracked locally and by a unique source id, so the two
   Shift keys stay independent and everything can be released at once if the
   connection or the page goes away mid-press. */

const pressed = new Map();

function pressKey(ident, key) {
  if (pressed.has(ident)) {
    return;
  }
  pressed.set(ident, key);
  post("/keyboard", { event: "down", key: key, id: ident });
}

function releaseKey(ident, key) {
  if (!pressed.delete(ident)) {
    return;
  }
  post("/keyboard", { event: "up", key: key, id: ident });
}

function releaseAllKeys() {
  const active = document.querySelectorAll('[aria-pressed="true"]');
  active.forEach((element) => {
    element.setAttribute("aria-pressed", "false");
    element.classList.remove("active");
  });
  // Touch state has to go too. A pointer that never reports pointerup (blur or
  // an app switch with fingers down) would otherwise stay in the map forever
  // and leave the touchpad stuck in two-finger gesture mode.
  pointers.clear();
  padDrag = null;
  pinch = null;
  if (!pressed.size) {
    return;
  }
  pressed.clear();
  // sendBeacon cannot set headers, so the token travels in the body.
  const body = JSON.stringify({ csrf: csrfToken });
  const blob = new Blob([body], { type: "application/json" });
  if (navigator.sendBeacon && navigator.sendBeacon("/keys/release", blob)) {
    return;
  }
  request("/keys/release", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body,
    keepalive: true,
  }).catch(() => {});
}

window.addEventListener("blur", releaseAllKeys);
window.addEventListener("pagehide", releaseAllKeys);
let hiddenSince = 0;

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    hiddenSince = performance.now();
    releaseAllKeys();
    // requestAnimationFrame does not fire while hidden, so a coalesced move
    // would otherwise sit in the queue and be delivered late, or never.
    if (moveFrame) {
      cancelAnimationFrame(moveFrame);
      moveFrame = 0;
    }
    flushMove();
    return;
  }
  const away = performance.now() - hiddenSince;
  if (away > 3000 || status.textContent !== "connected") {
    // A backgrounded tab can lose its stream without the element ever firing
    // error, leaving a frozen picture and a status line that still claims to
    // be connected. Reopening costs one request, so do it rather than trust
    // the status.
    streamAttempts = 0;
    openStream(0);
  }
});

/* Physical keyboard. Never intercept keys typed into the text box, and let
   Enter/Space activate a focused role=button control instead of forwarding it
   to the remote as a stray keypress. */
function typingInField(target) {
  return (
    target instanceof HTMLElement &&
    (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)
  );
}

/* Membership of activationControls is the exact set of controls that handle
   Enter/Space themselves, whether they are bound with bindPress or bindClick. */
function activatingControl(event) {
  if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") {
    return false;
  }
  return event.target instanceof HTMLElement && activationControls.has(event.target);
}

/* Tab belongs to the browser so the controls stay keyboard reachable. It is
   neither intercepted nor forwarded; to send Tab to the remote, use the
   on-screen Tab key. */
function localNavigationKey(event) {
  return event.key === "Tab";
}

document.addEventListener("keydown", (event) => {
  if (typingInField(event.target) || activatingControl(event) || localNavigationKey(event) || event.repeat) {
    return;
  }
  if (SCROLL_KEYS.has(event.key)) {
    event.preventDefault();
  }
  pressKey("kb:" + (event.code || event.key), event.key);
});

document.addEventListener("keyup", (event) => {
  if (typingInField(event.target) || activatingControl(event) || localNavigationKey(event)) {
    return;
  }
  if (SCROLL_KEYS.has(event.key)) {
    event.preventDefault();
  }
  releaseKey("kb:" + (event.code || event.key), event.key);
});

/* ------------------------------------------------------------ button binding
   One helper drives pointer, keyboard and ARIA state for every role=button
   control so nothing is pointer-only. */

function bindPress(element, press, release) {
  const down = () => {
    if (element.getAttribute("aria-pressed") === "true") {
      return;
    }
    element.setAttribute("aria-pressed", "true");
    element.classList.add("active");
    press();
  };
  const up = () => {
    if (element.getAttribute("aria-pressed") !== "true") {
      return;
    }
    element.setAttribute("aria-pressed", "false");
    element.classList.remove("active");
    release();
  };
  element.setAttribute("aria-pressed", "false");
  activationControls.add(element);
  element.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    if (event.pointerId >= 0) {
      try {
        element.setPointerCapture(event.pointerId);
      } catch (error) {
        // Synthetic or already-released pointer; capture is only an optimisation.
      }
    }
    down();
  });
  element.addEventListener("pointerup", up);
  element.addEventListener("pointercancel", up);
  element.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    if (!event.repeat) {
      down();
    }
  });
  element.addEventListener("keyup", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    up();
  });
}

function bindClick(element, action) {
  activationControls.add(element);
  element.addEventListener("click", action);
  element.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    if (!event.repeat) {
      action(event);
    }
  });
}

/* Virtual keyboard. */
document.querySelectorAll(".key[data-key]").forEach((key) => {
  const name = key.dataset.key;
  const ident = "vk:" + (key.dataset.id || name);
  bindPress(
    key,
    () => pressKey(ident, name),
    () => releaseKey(ident, name)
  );
});

/* Panel switch. */
document.querySelectorAll(".tab").forEach((tab) => {
  bindClick(tab, () => {
    document.querySelectorAll(".tab").forEach((other) => {
      other.classList.toggle("active", other === tab);
      other.setAttribute("aria-selected", String(other === tab));
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.id !== "panel-" + tab.dataset.panel);
    });
  });
});

/* Zoom. */
function flash(element) {
  element.classList.add("active");
  setTimeout(() => element.classList.remove("active"), 120);
}

bindClick(document.getElementById("zoom-in"), (event) => {
  flash(event.currentTarget);
  setZoom(zoom * ZOOM_FACTOR);
});

bindClick(document.getElementById("zoom-out"), (event) => {
  flash(event.currentTarget);
  setZoom(zoom / ZOOM_FACTOR);
});

bindClick(zoomLabel, (event) => {
  flash(event.currentTarget);
  setZoom(1);
});

/* Sign out: clear the server session and return to the login page. */
bindClick(document.getElementById("logout"), () => {
  releaseAllKeys();
  const form = document.createElement("form");
  form.method = "post";
  form.action = "/logout";
  // The form is a plain navigation, so the token travels as a field rather
  // than a header.
  const hidden = document.createElement("input");
  hidden.type = "hidden";
  hidden.name = "csrf";
  hidden.value = csrfToken;
  form.appendChild(hidden);
  document.body.appendChild(form);
  form.submit();
});

/* --------------------------------------------------------------------- text */

let sending = false;

async function sendText(text) {
  if (!text) {
    return;
  }
  if (sending) {
    // A slow typing job is still running. The caller has already cleared the
    // box, so returning here silently threw away what was just typed; put it
    // back and say why, the same way a failed send does.
    restoreText(text);
    showToast("still typing the previous text");
    return;
  }
  sending = true;
  try {
    const response = await post("/text", { text: text, delay: typingDelay });
    if (!response.ok) {
      // Do not lose what the user typed just because the request failed.
      restoreText(text);
      showToast("send failed: " + response.status);
      return;
    }
    const result = await response.json();
    setSystemStatus("typed " + result.typed + " of " + result.total + " characters");
  } catch (error) {
    restoreText(text);
    showToast("send failed: " + error.message);
  } finally {
    sending = false;
  }
}

/* Put the text back, but never overwrite something typed since. */
function restoreText(text) {
  if (!textInput.value) {
    textInput.value = text;
  }
}

bindClick(document.getElementById("text-send"), () => {
  const text = textInput.value;
  textInput.value = "";
  sendText(text);
});

textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    const text = textInput.value;
    textInput.value = "";
    sendText(text);
  }
});

/* ---------------------------------------------------------------- clipboard */

bindClick(document.getElementById("clipboard-send"), async () => {
  let text;
  try {
    text = await navigator.clipboard.readText();
  } catch (error) {
    showToast("cannot read local clipboard: " + error.message);
    return;
  }
  if (!text) {
    showToast("local clipboard is empty");
    return;
  }
  const response = await post("/clipboard", { text: text });
  if (response.ok) {
    showToast("remote clipboard set (" + text.length + " chars)");
  } else {
    showToast("failed: " + (await response.text()));
  }
});

bindClick(document.getElementById("clipboard-get"), async () => {
  const response = await request("/clipboard", { cache: "no-store" });
  if (!response.ok) {
    showToast("failed: " + (await response.text()));
    return;
  }
  const data = await response.json();
  const text = data.text || "";
  // Copying must not destroy something the user was part-way through typing.
  if (!textInput.value) {
    textInput.value = text;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast("remote clipboard copied (" + text.length + " chars)");
  } catch (error) {
    showToast("remote clipboard loaded into the text box");
  }
});

/* -------------------------------------------------------- touchpad gestures
   One pointer drags the remote cursor, two pointers scroll and pinch to zoom. */

const pointers = new Map();
let padDrag = null;
let pinch = null;

function gestureState() {
  const points = Array.from(pointers.values());
  if (points.length < 2) {
    return null;
  }
  const [a, b] = points;
  return {
    cx: (a.x + b.x) / 2,
    cy: (a.y + b.y) / 2,
    dist: Math.hypot(a.x - b.x, a.y - b.y),
  };
}

pad.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  try {
    pad.setPointerCapture(event.pointerId);
  } catch (error) {
    // Ignore: capture is only an optimisation.
  }
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  if (pointers.size === 1) {
    padDrag = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      moved: false,
      started: performance.now(),
    };
    pinch = null;
  } else {
    padDrag = null;
    pinch = gestureState();
  }
});

pad.addEventListener("pointermove", (event) => {
  if (!pointers.has(event.pointerId)) {
    return;
  }
  pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

  if (pointers.size >= 2) {
    const next = gestureState();
    if (!pinch || !next) {
      pinch = next;
      return;
    }
    const notchesY = accumulateNotches("touch", next.cy - pinch.cy, "y");
    const notchesX = accumulateNotches("touch", next.cx - pinch.cx, "x");
    scrollRemote(notchesX, notchesY);
    if (pinch.dist > 8 && next.dist > 8) {
      const factor = next.dist / pinch.dist;
      if (Math.abs(factor - 1) > 0.005) {
        setZoom(zoom * factor);
      }
    }
    pinch = next;
    return;
  }

  if (!padDrag || event.pointerId !== padDrag.id) {
    return;
  }
  const rect = pad.getBoundingClientRect();
  const stepX = event.clientX - padDrag.x;
  const stepY = event.clientY - padDrag.y;
  if (Math.abs(stepX) > 2 || Math.abs(stepY) > 2) {
    padDrag.moved = true;
  }
  padDrag.x = event.clientX;
  padDrag.y = event.clientY;
  moveCursor(
    {
      x: clamp(cursor.x + (stepX / rect.width) * SENSITIVITY),
      y: clamp(cursor.y + (stepY / rect.height) * SENSITIVITY),
    },
    true
  );
});

function endPointer(event) {
  if (!pointers.has(event.pointerId)) {
    return;
  }
  pointers.delete(event.pointerId);
  // Removing a pointer moves the centroid discontinuously, so re-baseline the
  // gesture. Without this, lifting one of three fingers registers the jump as
  // a large scroll on the next move.
  pinch = pointers.size >= 2 ? gestureState() : null;
  if (padDrag && event.pointerId === padDrag.id) {
    const tapped = !padDrag.moved && performance.now() - padDrag.started < TAP_MS;
    padDrag = null;
    if (tapped) {
      clickAtCursor("left");
    }
  }
  if (pointers.size === 1 && !padDrag) {
    const [id, point] = Array.from(pointers.entries())[0];
    padDrag = {
      id: id,
      x: point.x,
      y: point.y,
      moved: true,
      started: performance.now(),
    };
  }
}

pad.addEventListener("pointerup", endPointer);
pad.addEventListener("pointercancel", endPointer);

/* Virtual mouse buttons and scroll. Bound as press/release so the button
   shows its held state and reports aria-pressed, like the keyboard keys. */
document.querySelectorAll("[data-button], [data-scroll]").forEach((button) => {
  const action = () => {
    if (button.dataset.button) {
      clickAtCursor(button.dataset.button);
    } else {
      scrollRemote(0, Number(button.dataset.scroll));
    }
  };
  bindPress(button, action, () => {});
});

/* -------------------------------------------------------------- system panel */

async function loadMonitors(refresh) {
  try {
    const response = await request("/monitors" + (refresh ? "?refresh=1" : ""), {
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(String(response.status));
    }
    const data = await response.json();
    monitorSelect.innerHTML = "";
    data.monitors.forEach((monitor) => {
      const option = document.createElement("option");
      option.value = String(monitor.index);
      const offset = monitor.left || monitor.top ? " @ " + monitor.left + "," + monitor.top : "";
      option.textContent =
        "#" + (monitor.index + 1) + "  " + monitor.width + "×" + monitor.height + offset;
      monitorSelect.appendChild(option);
    });
    monitorSelect.value = String(data.current);
  } catch (error) {
    setSystemStatus("monitor list failed: " + error.message);
  }
}

monitorSelect.addEventListener("change", async () => {
  const response = await post("/monitor", { index: Number(monitorSelect.value) });
  setSystemStatus(response.ok ? "switched monitor" : "monitor switch failed");
});

monitorSelect.addEventListener("focus", () => {
  loadMonitors(true);
});

document.querySelectorAll("#presets .pbtn").forEach((button) => {
  bindClick(button, async () => {
    const preset = PRESETS[button.dataset.preset];
    document.querySelectorAll("#presets .pbtn").forEach((other) => {
      other.classList.toggle("active", other === button);
    });
    const response = await post("/stream-settings", preset);
    setSystemStatus(
      response.ok
        ? "stream: " + preset.fps + " fps, quality " + preset.quality + ", scale " + preset.scale
        : "stream settings failed"
    );
  });
});

document.querySelectorAll("#typing .pbtn").forEach((button) => {
  bindClick(button, () => {
    typingDelay = Number(button.dataset.delay);
    document.querySelectorAll("#typing .pbtn").forEach((other) => {
      other.classList.toggle("active", other === button);
    });
    setSystemStatus(typingDelay ? "slow typing on" : "fast typing on");
  });
});

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) {
    return;
  }
  for (const file of files) {
    const data = new FormData();
    data.append("file", file);
    try {
      const response = await request("/upload", { method: "POST", body: data });
      if (!response.ok) {
        throw new Error((await response.text()) || String(response.status));
      }
      const info = await response.json();
      showToast("sent " + info.name);
      setSystemStatus("saved to " + info.path);
    } catch (error) {
      showToast("upload failed: " + error.message);
    }
  }
}

bindClick(document.getElementById("file-send"), () => fileInput.click());

fileInput.addEventListener("change", () => {
  uploadFiles(fileInput.files);
  fileInput.value = "";
});

bindClick(document.getElementById("file-get"), async () => {
  const name = fileNameInput.value.trim();
  if (!name) {
    showToast("enter a file name from the remote Downloads folder");
    return;
  }
  // The endpoint is POST, so it cannot be a plain navigation; fetch the bytes
  // and save them. This buffers the file in memory, which is fine for the
  // documents this is meant for but not for very large files.
  const response = await post("/download", { name: name });
  if (!response.ok) {
    showToast("download failed: " + (await response.text()));
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
});

async function hostAction(action, question) {
  // Both actions take over the host immediately, so confirm first.
  if (!window.confirm(question)) {
    return;
  }
  const response = await post("/special", { action: action });
  if (response.ok) {
    showToast(action === "lock" ? "host locked" : "sent");
    return;
  }
  showToast((await response.text()) || "failed");
}

bindClick(document.getElementById("host-lock"), () =>
  hostAction("lock", "Lock the remote host now?")
);
bindClick(document.getElementById("host-cad"), () =>
  hostAction(
    "ctrl-alt-del",
    "Send Ctrl+Alt+Del to the remote host?\n\n" +
      "Windows only accepts this from a privileged service, so it will usually fail."
  )
);

/* Drag and drop onto the screen area sends files to the remote. */
["dragenter", "dragover"].forEach((type) => {
  screenWrap.addEventListener(type, (event) => {
    event.preventDefault();
    screenWrap.classList.add("drop");
  });
});

screenWrap.addEventListener("dragleave", (event) => {
  if (!screenWrap.contains(event.relatedTarget)) {
    screenWrap.classList.remove("drop");
  }
});

screenWrap.addEventListener("drop", (event) => {
  event.preventDefault();
  screenWrap.classList.remove("drop");
  uploadFiles(event.dataTransfer.files);
});

/* Keep the browser from navigating away when a file lands outside the wrap. */
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());

/* ------------------------------------------------------------------ latency */

async function checkLatency() {
  const started = performance.now();
  try {
    const response = await request("/ping", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(String(response.status));
    }
    const data = await response.json();
    latencyEl.textContent = Math.round(performance.now() - started) + " ms";
    // A dead capture path otherwise looks identical to a healthy one: a blank
    // picture and a status line that still says "connected".
    if (data.stream_error) {
      status.textContent = data.stream_error;
    }
  } catch (error) {
    latencyEl.textContent = "—";
  }
}

function pollLatency() {
  // A backgrounded tab does not need a request every 5s, which on a phone
  // keeps waking the radio.
  if (!document.hidden) {
    checkLatency();
  }
}

setInterval(pollLatency, PING_INTERVAL_MS);
checkLatency();

function loadCsrf() {
  csrfReady = request("/csrf", { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : null))
    .then((data) => {
      if (data && data.token) {
        csrfToken = data.token;
      }
    })
    .catch(() => {});
  return csrfReady;
}

/* The remote monitor can change shape underneath us (monitor switch, docking,
   resolution change); re-derive the picture rectangle when it does. */
let lastNatural = "";
setInterval(() => {
  const key = screenImage.naturalWidth + "x" + screenImage.naturalHeight;
  if (key !== lastNatural) {
    lastNatural = key;
    layout();
  }
}, 1000);

window.addEventListener("resize", () => {
  pan = panFor(cursor, zoom);
  layout();
});

loadMonitors(false);
layout();
// Must complete before any mutating request is attempted.
loadCsrf();
