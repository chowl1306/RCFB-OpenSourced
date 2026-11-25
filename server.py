import functools
import hashlib
import hmac
import io
import itertools
import os
import secrets
import sys
import threading
import time

import mss
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController
from werkzeug.utils import secure_filename

try:
    import pyperclip
except Exception:  # pragma: no cover - optional dependency
    pyperclip = None

HERE = os.path.dirname(os.path.abspath(__file__))

# Two directories, and they are not the same once the program is frozen.
#
# RESOURCE_DIR holds the read-only files shipped with the program (static/ and
# the fallback .env). A onefile build - Nuitka or PyInstaller - unpacks those
# into a temporary directory that is deleted when the process exits, so it is
# found through __file__ (Nuitka) or sys._MEIPASS (PyInstaller).
#
# BASE_DIR is where state that must outlive the process goes: the transfers
# directory and a user supplied .env. That is always the folder holding the
# executable. Nuitka injects __compiled__ into the module globals (not sys) and
# its containing_dir is that folder; PyInstaller sets sys.frozen and
# sys.executable to the exe, which is why it needs handling of its own -
# without it BASE_DIR resolved inside the temporary unpack directory, so
# uploads were deleted when the exe exited and the .env written next to the exe
# was never the one the server read.
_compiled = globals().get("__compiled__")
if getattr(_compiled, "onefile", False):
    BASE_DIR = getattr(_compiled, "containing_dir", None) or HERE
elif getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = HERE

RESOURCE_DIR = getattr(sys, "_MEIPASS", None) or HERE

USERNAME = "Root"
BOUNDARY = "frame"
DEFAULT_STREAM = {"fps": 10, "quality": 70, "scale": 1.0, "monitor": 0}
FPS_RANGE = (1, 30)
QUALITY_RANGE = (20, 95)
SCALE_RANGE = (0.25, 1.0)
LOGIN_MAX_FAILURES = 8
LOGIN_WINDOW = 300.0
LOGIN_FAILURE_KEYS_MAX = 1024
MONITOR_TTL = 5.0
STREAM_TTL = 600.0
TEXT_MAX_SECONDS = 30.0
STREAM_MAX_FAILURES = 20
UPLOAD_MAX_BYTES = 256 * 1024 * 1024
RCFB_DIR_MAX_BYTES = 2 * 1024 * 1024 * 1024
# Transfers live in their own directory rather than the whole Downloads folder,
# so a compromised session cannot read or overwrite everything in it.
RCFB_DIR = os.path.abspath(
    os.environ.get("RCFB_DIR") or os.path.join(BASE_DIR, "transfers")
)

RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR

def load_env():
    values = {}
    # An .env next to the executable wins over the one bundled into the build,
    # so the password can be changed without rebuilding.
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(path):
        path = os.path.join(RESOURCE_DIR, ".env")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip("\"'")
    return values

PASSWORD = load_env().get("PASSWORD")
if not PASSWORD:
    raise SystemExit("rcfb: PASSWORD is missing from .env")

app = Flask(__name__, static_folder=os.path.join(RESOURCE_DIR, "static"))
app.secret_key = hashlib.sha256(PASSWORD.encode()).digest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=512 * 1024 * 1024,
)

keyboard = KeyboardController()
mouse = MouseController()

# pynput injects through the OS input queue; two threads doing so at once can
# interleave events. Every device access is serialised on this lock. It is held
# only around individual calls, never across a sleep, so a slow typing job does
# not block input.
_device_lock = threading.RLock()

SPECIAL_KEYS = {
    "Enter": Key.enter,
    " ": Key.space,
    "Escape": Key.esc,
    "Backspace": Key.backspace,
    "Tab": Key.tab,
    "Shift": Key.shift,
    "Control": Key.ctrl,
    "Alt": Key.alt,
    "Meta": Key.cmd,
    "CapsLock": Key.caps_lock,
    "ArrowUp": Key.up,
    "ArrowDown": Key.down,
    "ArrowLeft": Key.left,
    "ArrowRight": Key.right,
    "Home": Key.home,
    "End": Key.end,
    "PageUp": Key.page_up,
    "PageDown": Key.page_down,
    "Insert": Key.insert,
    "Delete": Key.delete,
}
CONTROL_CHARS = {
    "\n": Key.enter,
    "\r": Key.enter,
    "\t": Key.tab,
    "\b": Key.backspace,
    "\x1b": Key.esc,
    "\x7f": Key.delete,
}
for _i in range(1, 13):
    SPECIAL_KEYS["F%d" % _i] = getattr(Key, "f%d" % _i)

BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}

def csrf_token():
    token = session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def csrf_ok():
    """State-changing requests must carry this session's token.

    SameSite=Lax already blocks the cookie on a cross-site POST, but that is a
    browser default rather than a guarantee, and it does nothing for a GET that
    returns data. Every mutating request is checked explicitly.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    expected = session.get("csrf")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf")
    if not isinstance(supplied, str):
        # sendBeacon cannot set headers, so a release-all carries it in the body.
        supplied = (request.get_json(silent=True) or {}).get("csrf")
    return (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and hmac.compare_digest(supplied, expected)
    )


def authenticated(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            if request.method == "GET" and request.path == "/":
                return redirect(url_for("login_page"))
            return "unauthorized", 401
        if not csrf_ok():
            return "bad csrf token", 403
        return view(*args, **kwargs)

    return wrapper

@app.before_request
def choose_cookie_security():
    """Mark the session cookie Secure when the request arrived over https.

    Behind the tunnel the app itself sees plain http on loopback, so the
    proxy's forwarded scheme is what counts. Locally over http://127.0.0.1 a
    Secure cookie would simply never be sent, which would break login.
    """
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    trusted = (request.remote_addr or "") in TRUSTED_PROXIES
    app.config["SESSION_COOKIE_SECURE"] = request.is_secure or (
        trusted and forwarded == "https"
    )


@app.after_request
def no_store(response):
    # Nothing here is worth caching: the shell is session specific and the
    # JSON/stream endpoints must never be replayed from a shared browser.
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response

# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------

_login_failures = {}
_login_lock = threading.Lock()

TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1", "localhost"})

def client_ip():
    """Identify the client, trusting a forwarded header only from a local proxy.

    cloudflared connects from loopback, so the header is the only way to tell
    clients apart behind the tunnel. A client that reaches the server directly
    must not be able to choose its own rate-limit bucket by sending the header
    itself, so it is honoured only when the peer really is loopback.
    """
    peer = request.remote_addr or "?"
    if peer in TRUSTED_PROXIES:
        forwarded = request.headers.get("CF-Connecting-IP")
        if forwarded and forwarded.strip():
            return forwarded.strip()
    return peer

def login_locked(ip):
    now = time.monotonic()
    with _login_lock:
        recent = [t for t in _login_failures.get(ip, ()) if now - t < LOGIN_WINDOW]
        if recent:
            _login_failures[ip] = recent
        else:
            _login_failures.pop(ip, None)
        return len(recent) >= LOGIN_MAX_FAILURES

def prune_login_failures(now):
    """Caller holds _login_lock. Keep the table bounded even when it is fed a
    flood of distinct keys, which a forged forwarded header would produce."""
    for name, stamps in list(_login_failures.items()):
        kept = [t for t in stamps if now - t < LOGIN_WINDOW]
        if kept:
            _login_failures[name] = kept
        else:
            _login_failures.pop(name, None)
    overflow = len(_login_failures) - LOGIN_FAILURE_KEYS_MAX
    if overflow > 0:
        oldest = sorted(_login_failures.items(), key=lambda item: max(item[1]))
        for name, _ in oldest[:overflow]:
            _login_failures.pop(name, None)


def record_login_failure(ip):
    now = time.monotonic()
    with _login_lock:
        recent = [t for t in _login_failures.get(ip, ()) if now - t < LOGIN_WINDOW]
        recent.append(now)
        _login_failures[ip] = recent
        if len(_login_failures) > LOGIN_FAILURE_KEYS_MAX:
            prune_login_failures(now)

def clear_login_failures(ip):
    with _login_lock:
        _login_failures.pop(ip, None)

# --------------------------------------------------------------------------
# Keyboard state. Presses are counted per source id so that, for example, the
# two Shift buttons stay independent, and every held key can be released even
# when the client disappears mid-press.
# --------------------------------------------------------------------------

_pressed = {}
_pressed_lock = threading.Lock()
_recent = {}
RECENT_TTL = 30.0
# Source ids for batch typing, so every character it presses is tracked under
# a name no client can collide with.
_text_counter = itertools.count(1)


def session_id():
    """A stable per-browser id, so one client cannot release another's keys.

    It is minted once at login and carried in the session cookie, so two
    requests that arrive together see the same value rather than each creating
    their own. Anything keyed by this (the stream settings table, the key
    ownership map) therefore agrees across concurrent requests.
    """
    sid = session.get("sid")
    if not isinstance(sid, str):
        sid = secrets.token_hex(8)
        session["sid"] = sid
    return sid


def _mark_recent(key):
    now = time.monotonic()
    with _pressed_lock:
        _recent[repr(key)] = now
        if len(_recent) > 512:
            for name, seen in list(_recent.items()):
                if now - seen > RECENT_TTL:
                    _recent.pop(name, None)


def _recently_pressed(key):
    with _pressed_lock:
        return time.monotonic() - _recent.get(repr(key), 0.0) <= RECENT_TTL


def key_down(sid, key, ident):
    with _pressed_lock:
        if (sid, ident) in _pressed:
            return
        # Only send a real press when no source anywhere already holds it.
        first = not any(held is key or held == key for held in _pressed.values())
        _pressed[(sid, ident)] = key
    _mark_recent(key)
    if first:
        keyboard.press(key)


def key_up(sid, key, ident):
    with _pressed_lock:
        stored = _pressed.pop((sid, ident), None)
        known = stored is not None
        if known:
            # Release the key that was actually pressed. The client reports
            # event.key, which changes case while Shift is held, so trusting
            # the name on the release would let go of a different physical key
            # than the press took and leave the pressed one stuck.
            key = stored
        # Release only once every source has let go.
        release = not any(held is key or held == key for held in _pressed.values())
    if release and not known and not _recently_pressed(key):
        # A release for a key we have no record of pressing: ignore it rather
        # than inject an event for something the host may be holding itself.
        return
    if release:
        keyboard.release(key)


def release_all_keys(sid=None):
    """Release held keys. With an sid, only that session's presses are dropped."""
    with _pressed_lock:
        keys = []
        for owner, ident in list(_pressed.keys()):
            if sid is not None and owner != sid:
                continue
            key = _pressed.pop((owner, ident))
            # Another session may still be holding the same physical key.
            if any(held is key or held == key for held in _pressed.values()):
                continue
            if not any(held is key or held == key for held in keys):
                keys.append(key)
    for key in keys:
        try:
            keyboard.release(key)
        except Exception:
            pass
    return len(keys)

# --------------------------------------------------------------------------
# Monitor geometry. Re-enumerated on a short timer so hot-plugging, docking or
# a resolution change is picked up without restarting the server.
# --------------------------------------------------------------------------

_monitors = []
_monitors_at = 0.0
_monitors_lock = threading.Lock()

def monitors(refresh=False):
    global _monitors, _monitors_at
    now = time.monotonic()
    with _monitors_lock:
        if refresh or not _monitors or now - _monitors_at > MONITOR_TTL:
            try:
                with mss.mss() as sct:
                    _monitors = [dict(m) for m in sct.monitors[1:]]
            except Exception:
                pass
            _monitors_at = now
        return list(_monitors)

# --------------------------------------------------------------------------
# Per-client stream settings. Kept in a process-local table keyed by a token in
# the session so the frame generator can pick up quality/fps/monitor changes on
# the fly, without the client having to restart the stream.
# --------------------------------------------------------------------------

_streams = {}
_streams_lock = threading.Lock()

def stream_config():
    """This session's stream settings, creating the entry on first use.

    The key is the session id, not a token minted here. The page load issues
    /screen (from the <img>) and /ping together; each used to find no token in
    its own copy of the session and mint its own, so the cookie kept one while
    the live generator streamed on the other, and every later settings change
    went to the entry nobody was reading. The session id is fixed at login, so
    concurrent requests agree on the same entry.
    """
    token = session_id()
    now = time.monotonic()
    with _streams_lock:
        stale = [k for k, v in _streams.items() if now - v["touched"] > STREAM_TTL]
        for key in stale:
            _streams.pop(key, None)
        config = _streams.get(token)
        if config is None:
            config = _streams[token] = dict(DEFAULT_STREAM, touched=now)
        config["touched"] = now
    # Kept in the session too, so a client can carry it; written only when it
    # actually changes, which is once per login.
    if session.get("stream") != token:
        session["stream"] = token
    return token, config

def set_stream_error(token, message):
    """Record (or clear) the failure state of one stream.

    This belongs to the stream, not to the process: one global flag let a
    healthy frame on any stream clear the report of a stream that had
    permanently failed, and let /ping describe someone else's failure.
    """
    with _streams_lock:
        entry = _streams.get(token)
        if entry is not None:
            entry["error"] = message

def stream_error(token):
    with _streams_lock:
        entry = _streams.get(token)
        return entry.get("error") if entry is not None else None

def peek_config():
    """Read this session's stream config without creating or touching it.

    Used on the mouse path, which runs many times a second: stream_config()
    can write to the session, and a session write means a Set-Cookie on every
    mouse move.
    """
    token = session.get("stream")
    if not isinstance(token, str):
        return None
    with _streams_lock:
        entry = _streams.get(token)
        return dict(entry) if entry is not None else None


def monitor_geometry(monitor):
    return {k: monitor.get(k) for k in ("left", "top", "width", "height")}


def resolve_monitor(config):
    """Resolve which display a config refers to.

    The selection is stored as a geometry key, not just a slot index, so that
    unplugging a display does not silently hand the session to whatever now
    occupies that index. Returns (index, monitor, monitors).
    """
    mons = monitors()
    if not mons:
        return 0, None, mons
    key = config.get("monitor_key")
    if isinstance(key, dict):
        for i, monitor in enumerate(mons):
            if monitor_geometry(monitor) == key:
                return i, monitor, mons
        # The display we were using is gone; fall back to the first one rather
        # than following a different monitor that happens to share the slot.
        return 0, mons[0], mons
    index = config.get("monitor", 0)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(mons):
        index = 0
    return index, mons[index], mons


def current_monitor():
    config = peek_config() or DEFAULT_STREAM
    _, monitor, _ = resolve_monitor(config)
    return monitor, monitors()

def frames(token):
    """Yield MJPEG frames for one stream until it fails or the client leaves.

    Its liveness stamp uses the same clock stream_config() sweeps with.
    """
    grabber = None
    consecutive = 0
    try:
        while True:
            started = time.monotonic()
            with _streams_lock:
                entry = _streams.get(token)
                if entry is not None:
                    # An open stream is a live subscriber, so keep its entry
                    # from being swept as stale: a viewer that only watches
                    # the screen sends no other request that would refresh it,
                    # and eviction would silently reset its settings. The clock
                    # here must be the same one stream_config() sweeps with, or
                    # the age it computes is meaningless and nothing is ever
                    # evicted.
                    entry["touched"] = started
                    config = dict(entry)
                else:
                    config = dict(DEFAULT_STREAM)
            _, monitor, _ = resolve_monitor(config)
            if monitor is None:
                # Nothing to capture. Retrying is right, but it has to end the
                # same way a failing grab does, and it has to say so: looping
                # here forever left the client showing "connected" over a blank
                # picture with no explanation.
                set_stream_error(token, "no display available to capture")
                consecutive += 1
                if consecutive >= STREAM_MAX_FAILURES:
                    return
                time.sleep(0.5)
                continue
            fps = config.get("fps", DEFAULT_STREAM["fps"])
            quality = config.get("quality", DEFAULT_STREAM["quality"])
            scale = config.get("scale", DEFAULT_STREAM["scale"])
            try:
                if grabber is None:
                    grabber = mss.mss()
                shot = grabber.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                if scale < 1.0:
                    size = (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    )
                    image = image.resize(size, RESAMPLE)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=quality)
                payload = buffer.getvalue()
                consecutive = 0
                set_stream_error(token, None)
            except Exception as exc:
                # A transient grab failure must not kill the stream; drop the
                # grabber (it may be in a bad state) and try again shortly.
                if grabber is not None:
                    try:
                        grabber.close()
                    except Exception:
                        pass
                    grabber = None
                consecutive += 1
                if consecutive >= STREAM_MAX_FAILURES:
                    # Retrying forever would leave the client showing a blank
                    # picture with no explanation. Record why and stop, so the
                    # status line can say something useful.
                    set_stream_error(token, "capture failing: %s" % exc)
                    return
                time.sleep(0.2)
                continue
            yield (
                b"--" + BOUNDARY.encode() + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                + payload + b"\r\n"
            )
            # Guarded: a corrupt config must not end the stream with a
            # ZeroDivisionError from inside the generator.
            time.sleep(max(0.0, 1.0 / max(1, fps) - (time.monotonic() - started)))
    finally:
        if grabber is not None:
            try:
                grabber.close()
            except Exception:
                pass

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def directory_size(path):
    """Total bytes of the files directly in path, or None if it cannot be read.

    None is not the same as 0: a directory that cannot be scanned must not look
    empty to the quota check, or uploads would be allowed into a place whose
    usage is unknown.
    """
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
    except OSError:
        return None
    return total

def remove_file(path):
    """Delete a file, ignoring the case where it is already gone."""
    try:
        os.remove(path)
    except OSError:
        pass


def unique_path(directory, name):
    """Create and return a path that did not exist a moment ago.

    O_CREAT|O_EXCL makes the check and the create one step, so two concurrent
    uploads of the same name cannot both choose the same target and have one
    silently overwrite the other.
    """
    stem, extension = os.path.splitext(name)
    for counter in range(0, 10000):
        candidate = name if counter == 0 else "%s-%d%s" % (stem, counter, extension)
        path = os.path.join(directory, candidate)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(handle)
        return path
    raise OSError("no free name for %r" % name)


def safe_name(filename):
    """Sanitise an uploaded name without silently renaming dotfiles.

    secure_filename() drops the leading dot, so ".env" would be stored as
    "env" and the caller would be told a different name than it sent.
    """
    base = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = secure_filename(base)
    if not cleaned:
        return ""
    if base.startswith(".") and not cleaned.startswith("."):
        cleaned = "." + cleaned
    return cleaned


def clamp_number(value, low, high, fallback):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return type(fallback)(min(high, max(low, value)))

def resolve_key(name):
    # A JSON object or list is not hashable, and a plain dict lookup would
    # raise TypeError instead of letting the caller answer 400.
    if not isinstance(name, str):
        return None
    key = SPECIAL_KEYS.get(name)
    if key is None:
        key = CONTROL_CHARS.get(name)
    if key is None and len(name) == 1:
        key = name
    return key

# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------

@app.get("/login")
def login_page():
    return app.send_static_file("login.html")

@app.post("/login")
def login_submit():
    ip = client_ip()
    if login_locked(ip):
        return redirect(url_for("login_page", error="locked"))
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    ok = hmac.compare_digest(username.encode(), USERNAME.encode())
    ok &= hmac.compare_digest(password.encode(), PASSWORD.encode())
    if not ok:
        record_login_failure(ip)
        return redirect(url_for("login_page", error=1))
    clear_login_failures(ip)
    session.clear()
    session["authenticated"] = True
    # Mint the per-browser id here rather than lazily: it keys both the stream
    # settings and the key-ownership map, and two requests arriving together on
    # first load must not each create their own.
    session_id()
    return redirect(url_for("index"))

@app.get("/csrf")
@authenticated
def csrf():
    return jsonify(token=csrf_token())


@app.post("/logout")
def logout():
    if not session.get("authenticated") or not csrf_ok():
        return "unauthorized", 401
    # Release only this session's held keys; another client may be using the
    # same host right now. session.get("sid") must not be allowed to be None
    # here: release_all_keys(None) means "every session", so a browser that
    # never pressed anything would let go of the keys a different browser is
    # holding. A session without a sid has no presses of its own, so there is
    # nothing to release.
    sid = session.get("sid")
    if isinstance(sid, str):
        with _device_lock:
            release_all_keys(sid)
    session.clear()
    return redirect(url_for("login_page"))

@app.get("/")
@authenticated
def index():
    return app.send_static_file("index.html")

@app.get("/ping")
@authenticated
def ping():
    # Touching the config keeps this client's stream entry alive even if the
    # frame generator has not had a chance to refresh it yet.
    token, _ = stream_config()
    return jsonify(ok=True, stream_error=stream_error(token))

@app.get("/screen")
@authenticated
def screen():
    token, _ = stream_config()
    return Response(
        frames(token), mimetype="multipart/x-mixed-replace; boundary=%s" % BOUNDARY
    )

@app.get("/monitors")
@authenticated
def monitor_list():
    if request.args.get("refresh"):
        monitors(refresh=True)
    _, config = stream_config()
    current, _, mons = resolve_monitor(config)
    return jsonify(
        monitors=[dict(m, index=i) for i, m in enumerate(mons)], current=current
    )

@app.post("/monitor")
@authenticated
def monitor_select():
    data = request.get_json(silent=True) or {}
    _, config = stream_config()
    mons = monitors(refresh=True)
    index = data.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(mons):
        return "invalid monitor", 400
    config["monitor"] = index
    config["monitor_key"] = monitor_geometry(mons[index])
    return "", 204

@app.post("/stream-settings")
@authenticated
def stream_settings():
    data = request.get_json(silent=True) or {}
    _, config = stream_config()
    config["fps"] = clamp_number(data.get("fps"), *FPS_RANGE, DEFAULT_STREAM["fps"])
    config["quality"] = clamp_number(
        data.get("quality"), *QUALITY_RANGE, DEFAULT_STREAM["quality"]
    )
    config["scale"] = clamp_number(
        data.get("scale"), *SCALE_RANGE, DEFAULT_STREAM["scale"]
    )
    return jsonify({k: config[k] for k in ("fps", "quality", "scale")})

@app.post("/keyboard")
@authenticated
def keyboard_event():
    data = request.get_json(silent=True) or {}
    event = data.get("event")
    name = data.get("key")
    if event not in ("down", "up"):
        return "invalid event", 400
    key = resolve_key(name)
    if key is None:
        return "invalid key", 400
    ident = data.get("id")
    if not isinstance(ident, str) or not ident:
        ident = name
    with _device_lock:
        if event == "down":
            key_down(session_id(), key, ident)
        else:
            key_up(session_id(), key, ident)
    return "", 204

@app.post("/keys/release")
@authenticated
def keys_release():
    with _device_lock:
        release_all_keys(session_id())
    return "", 204

@app.post("/text")
@authenticated
def text_event():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str) or not text:
        return "invalid text", 400
    if len(text) > 10000:
        return "text too long", 413
    delay = clamp_number(data.get("delay"), 0.0, 0.5, 0.0)
    # A slow job holds a worker thread for len(text) * delay seconds; cap the
    # total so one request cannot occupy a thread for the better part of an hour.
    if len(text) * delay > TEXT_MAX_SECONDS:
        return "typing job too long", 400
    typed = 0
    sid = session_id()
    for character in text:
        key = resolve_key(character)
        if key is None:
            continue
        # Go through the same press/release bookkeeping the virtual and
        # physical keyboards use. Injecting straight at the controller let this
        # loop release a key /keyboard was still holding, and a press whose
        # release raised left a stuck key that nothing tracked.
        ident = "text:%s" % next(_text_counter)
        try:
            with _device_lock:
                key_down(sid, key, ident)
                key_up(sid, key, ident)
        except Exception:
            continue
        typed += 1
        if delay:
            # Deliberately outside the lock: a slow job must not block input.
            time.sleep(delay)
    return jsonify(typed=typed, total=len(text))

@app.post("/mouse")
@authenticated
def mouse_event():
    data = request.get_json(silent=True) or {}
    event = data.get("event")
    x = data.get("x")
    y = data.get("y")
    # bool is a subclass of int, so a bare isinstance check lets true/false
    # through as coordinates and as wheel deltas.
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, float))
        or not isinstance(y, (int, float))
    ):
        return "invalid coordinates", 400
    x = min(1.0, max(0.0, float(x)))
    y = min(1.0, max(0.0, float(y)))
    monitor, _ = current_monitor()
    if monitor is None:
        return "no monitor", 503
    position = (
        monitor["left"] + round(x * monitor["width"]),
        monitor["top"] + round(y * monitor["height"]),
    )
    if event == "move":
        with _device_lock:
            mouse.position = position
    elif event == "click":
        name = data.get("button")
        button = BUTTONS.get(name) if isinstance(name, str) else None
        if button is None:
            return "invalid button", 400
        with _device_lock:
            mouse.position = position
            mouse.click(button)
    elif event == "scroll":
        dx = data.get("dx")
        dy = data.get("dy")
        # Units are wheel notches; pynput maps positive dy to "scroll up".
        if (
            isinstance(dx, bool)
            or isinstance(dy, bool)
            or not isinstance(dx, int)
            or not isinstance(dy, int)
        ):
            return "invalid scroll", 400
        with _device_lock:
            mouse.position = position
            mouse.scroll(dx, dy)
    else:
        return "invalid event", 400
    return "", 204

@app.get("/clipboard")
@authenticated
def clipboard_get():
    if pyperclip is None:
        return "clipboard unavailable", 501
    try:
        text = pyperclip.paste()
    except Exception as exc:
        return "clipboard error: %s" % exc, 500
    return jsonify(text=text if isinstance(text, str) else "")

@app.post("/clipboard")
@authenticated
def clipboard_set():
    if pyperclip is None:
        return "clipboard unavailable", 501
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return "invalid text", 400
    try:
        pyperclip.copy(text)
    except Exception as exc:
        return "clipboard error: %s" % exc, 500
    return "", 204

@app.post("/upload")
@authenticated
def upload():
    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        return "no file", 400
    try:
        os.makedirs(RCFB_DIR, exist_ok=True)
    except OSError as exc:
        # RCFB_DIR is not usable (a file sits at that path, or the volume is
        # gone). Left unhandled this surfaced as a 500 with a traceback.
        return "transfers directory is not usable: %s" % exc, 500
    used = directory_size(RCFB_DIR)
    if used is None:
        return "could not read the transfers directory", 500
    if used >= RCFB_DIR_MAX_BYTES:
        return "transfer directory is full", 507
    name = safe_name(upload_file.filename) or "upload.bin"
    try:
        target = unique_path(RCFB_DIR, name)
    except OSError:
        return "too many files with that name", 409
    # Stream to disk with a hard ceiling. The declared size and Flask's global
    # request limit are not a per-file cap, which is what stops one upload from
    # filling the whole transfer directory.
    written = 0
    try:
        with open(target, "wb") as handle:
            while True:
                chunk = upload_file.stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    raise ValueError("too large")
                handle.write(chunk)
    except ValueError:
        remove_file(target)
        return "file too large (limit %d bytes)" % UPLOAD_MAX_BYTES, 413
    except OSError as exc:
        remove_file(target)
        return "could not save the file: %s" % exc, 500
    # The quota was checked before this file existed, and a concurrent upload
    # can have pushed the directory over in the meantime. Confirm rather than
    # assume, so the cap is a limit and not a hint.
    used = directory_size(RCFB_DIR)
    if used is not None and used > RCFB_DIR_MAX_BYTES:
        remove_file(target)
        return "transfer directory is full", 507
    return jsonify(name=os.path.basename(target), path=target)

@app.post("/download")
@authenticated
def download():
    # POST rather than GET: a GET that returns a file can be triggered by any
    # page that can navigate the browser, and SameSite=Lax still sends the
    # cookie on a top-level navigation.
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    if not name or os.path.basename(name) != name:
        return "invalid name", 400
    if not os.path.isdir(RCFB_DIR):
        return "no downloads folder", 404
    return send_from_directory(RCFB_DIR, name, as_attachment=True)

@app.post("/special")
@authenticated
def special():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action == "lock":
        try:
            import ctypes

            with _device_lock:
                locked = ctypes.windll.user32.LockWorkStation()
            if not locked:
                return "lock failed", 500
        except Exception as exc:
            return "lock failed: %s" % exc, 500
        return "", 204
    if action == "ctrl-alt-del":
        # Windows only routes this through SendSAS, which needs a service or
        # SeTcbPrivilege; a plain user process is refused by the OS.
        try:
            import ctypes

            ctypes.windll.sas.SendSAS(False)
        except Exception as exc:
            return "ctrl+alt+del unavailable: %s" % exc, 501
        return "", 204
    return "invalid action", 400

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=6767, threaded=True)
