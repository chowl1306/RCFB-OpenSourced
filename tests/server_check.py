"""Regression suite for rcfb.

Runs against the real server.py with the input devices, clipboard and capture
stubbed out, so it never moves the real mouse, types on the real keyboard or
touches the real clipboard. Nothing is written outside a temp directory.

    python tests/server_check.py

Exits non-zero if any check fails. Each check names the bug it guards.
"""

import io
import json
import os
import sys
import tempfile
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TRANSFER = tempfile.mkdtemp(prefix="rcfb_transfer_")
os.environ["RCFB_DIR"] = TRANSFER

import server  # noqa: E402  (must follow the env var above)
from pynput.keyboard import Key  # noqa: E402

# Before RCFB_DIR lands, server.py still targets the real ~/Downloads. Point
# it at the temp directory so a test run can never write into the user's
# folder; the dedicated-directory check below still requires RCFB_DIR to exist.
server.RCFB_DIR = TRANSFER

CALLS = []


class FakeKeyboard:
    def press(self, key):
        CALLS.append(("down", key))

    def release(self, key):
        CALLS.append(("up", key))


class FakeMouse:
    _position = (0, 0)

    @property
    def position(self):
        return self._position

    @position.setter
    def position(self, value):
        self._position = value

    def click(self, button):
        CALLS.append(("click", button))

    def scroll(self, dx, dy):
        CALLS.append(("scroll", dx, dy))


class FakeClipboard:
    board = ""

    @classmethod
    def copy(cls, text):
        cls.board = text

    @classmethod
    def paste(cls):
        return cls.board


MONITORS = [
    {"left": 0, "top": 0, "width": 800, "height": 600},
    {"left": 800, "top": 0, "width": 400, "height": 300},
]

server.keyboard = FakeKeyboard()
server.mouse = FakeMouse()
server.pyperclip = FakeClipboard
server.monitors = lambda refresh=False: [dict(m) for m in MONITORS]

app = server.app
app.config["TESTING"] = True

FAILURES = []


def check(bug, name, condition, extra=""):
    label = "%s %s" % (bug, name)
    if condition:
        print("  PASS  " + label)
    else:
        print("  FAIL  " + label + ("   <- " + repr(extra) if extra else ""))
        FAILURES.append(label)


def client(authenticated=True):
    c = app.test_client()
    if authenticated:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "test-token"
    return c


def token(c):
    return {"X-CSRF-Token": "test-token"}


def presses():
    return [c for c in CALLS if c[0] == "down"]


def releases():
    return [c for c in CALLS if c[0] == "up"]


# ---------------------------------------------------------------------------
print("\nauth and no-store")
# ---------------------------------------------------------------------------
anon = app.test_client()
check("BASE", "anonymous index redirects", anon.get("/").status_code == 302)
check("BASE", "anonymous keyboard is 401",
      anon.post("/keyboard", json={"event": "down", "key": "a"}).status_code == 401)
c = client()
check("BASE", "authenticated index", c.get("/").status_code == 200)
check("BASE", "index is no-store", c.get("/").headers.get("Cache-Control") == "no-store")
check("BASE", "static is cacheable",
      c.get("/static/style.css").headers.get("Cache-Control") != "no-store")


# ---------------------------------------------------------------------------
print("\nBUG-13  forwarded header is only trusted from loopback")
# ---------------------------------------------------------------------------
with app.test_request_context("/login", headers={"CF-Connecting-IP": "9.9.9.9"},
                              environ_base={"REMOTE_ADDR": "203.0.113.7"}):
    check("BUG-13", "ignores header from a remote peer", server.client_ip() == "203.0.113.7")
with app.test_request_context("/login", headers={"CF-Connecting-IP": "9.9.9.9"},
                              environ_base={"REMOTE_ADDR": "127.0.0.1"}):
    check("BUG-13", "honours header from loopback", server.client_ip() == "9.9.9.9")


# ---------------------------------------------------------------------------
print("\nBUG-28  login failure table stays bounded")
# ---------------------------------------------------------------------------
server._login_failures.clear()
server.LOGIN_MAX_FAILURES = 8
for i in range(server.LOGIN_MAX_FAILURES):
    c.post("/login", data={"username": "Root", "password": "bad"})
check("BUG-28", "locks after the limit",
      "error=locked" in c.post("/login", data={"username": "Root", "password": "bad"}).headers["Location"])
check("BUG-28", "lockout refuses the correct password",
      "error=locked" in c.post("/login", data={"username": "Root", "password": server.PASSWORD}).headers["Location"])
keys_max = getattr(server, "LOGIN_FAILURE_KEYS_MAX", 0)
for i in range(keys_max + 200):
    server.record_login_failure("spoof-%d" % i)
check("BUG-28", "table is capped", keys_max and len(server._login_failures) <= keys_max,
      len(server._login_failures))
server._login_failures.clear()
ok = app.test_client()
r = ok.post("/login", data={"username": "Root", "password": server.PASSWORD},
            environ_base={"REMOTE_ADDR": "10.0.0.9"})
check("BUG-28", "correct login still works", r.headers["Location"].endswith("/"))


# ---------------------------------------------------------------------------
print("\nBUG-22  the session cookie is Secure only over https")
# ---------------------------------------------------------------------------
def cookie_for(forwarded):
    fresh = app.test_client()
    with fresh.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    headers = {"X-CSRF-Token": "tok"}
    if forwarded:
        headers["X-Forwarded-Proto"] = "https"
    r = fresh.post("/keyboard", json={"event": "down", "key": "a", "id": "c"},
                   headers=headers, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    return r.headers.get("Set-Cookie", "")


tunnel = cookie_for(True)
local = cookie_for(False)
check("BUG-22", "Secure behind the https tunnel", "Secure" in tunnel, tunnel)
check("BUG-22", "not Secure over plain http", local and "Secure" not in local, local)


# ---------------------------------------------------------------------------
print("\nBUG-24  device access is serialised")
# ---------------------------------------------------------------------------
class OverlapDetectingKeyboard:
    """Records whether two device calls ever run at the same time."""

    def __init__(self):
        self.inside = 0
        self.overlap = 0
        self.guard = threading.Lock()

    def _enter(self):
        with self.guard:
            self.inside += 1
            if self.inside > 1:
                self.overlap += 1

    def _exit(self):
        with self.guard:
            self.inside -= 1

    def press(self, key):
        self._enter()
        time.sleep(0.01)
        self._exit()

    def release(self, key):
        self._enter()
        time.sleep(0.01)
        self._exit()


saved_keyboard = server.keyboard
detector = OverlapDetectingKeyboard()
server.keyboard = detector
try:
    clients = []
    for i in range(8):
        cl = app.test_client()
        with cl.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf"] = "tok"
        clients.append(cl)

    def hammer(cl, i):
        cl.post("/keyboard", json={"event": "down", "key": "a", "id": "t%d" % i},
                headers={"X-CSRF-Token": "tok"})

    workers = [threading.Thread(target=hammer, args=(cl, i)) for i, cl in enumerate(clients)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10)
    check("BUG-24", "device calls never overlap", detector.overlap == 0, detector.overlap)
finally:
    server.keyboard = saved_keyboard
    server.release_all_keys()


# ---------------------------------------------------------------------------
print("\nBUG-21  CSRF token required on state-changing requests")
# ---------------------------------------------------------------------------
no_token = client()
r = no_token.post("/keyboard", json={"event": "down", "key": "a"})
check("BUG-21", "POST without a token is refused", r.status_code == 403, r.status_code)
r = no_token.post("/mouse", json={"event": "move", "x": 0.5, "y": 0.5})
check("BUG-21", "mouse without a token is refused", r.status_code == 403, r.status_code)
r = c.post("/keyboard", json={"event": "down", "key": "a"}, headers=token(c))
check("BUG-21", "POST with a token is accepted", r.status_code == 204, r.status_code)
r = c.post("/keyboard", json={"event": "down", "key": "a"},
           headers={"X-CSRF-Token": "wrong"})
check("BUG-21", "wrong token is refused", r.status_code == 403, r.status_code)
csrf = c.get("/csrf")
check("BUG-21", "csrf endpoint returns this session's token",
      csrf.status_code == 200 and csrf.get_json().get("token") == "test-token",
      csrf.get_json() if csrf.status_code == 200 else csrf.status_code)
fresh = app.test_client()
with fresh.session_transaction() as s:
    s["authenticated"] = True
check("BUG-21", "a fresh session gets a long random token",
      len(fresh.get("/csrf").get_json()["token"]) >= 16)
check("BUG-21", "download is not a GET anymore", c.get("/download?name=x").status_code == 405)


# ---------------------------------------------------------------------------
print("\nBUG-06 / BUG-07 / BUG-08  key ownership and release")
# ---------------------------------------------------------------------------
server.release_all_keys()
CALLS.clear()
c.post("/keyboard", json={"event": "down", "key": "Shift", "id": "shift-left"}, headers=token(c))
c.post("/keyboard", json={"event": "down", "key": "Shift", "id": "shift-right"}, headers=token(c))
check("BASE", "two holders press the key once", presses() == [("down", Key.shift)], CALLS)
CALLS.clear()
c.post("/keys/release", headers=token(c))
check("BUG-06", "release-all does not double-release",
      len([x for x in CALLS if x[0] == "up"]) == 1, CALLS)

server.release_all_keys()
CALLS.clear()
c1 = client()
with c1.session_transaction() as s:
    s["stream"] = "s1"
c2 = client()
with c2.session_transaction() as s:
    s["stream"] = "s2"
c1.post("/keyboard", json={"event": "down", "key": "Control", "id": "vk:Control"}, headers=token(c1))
CALLS.clear()
c2.post("/keys/release", headers=token(c2))
check("BUG-07", "another session's release-all does not touch our key", CALLS == [], CALLS)
c1.post("/keys/release", headers=token(c1))
check("BUG-07", "our own release-all frees it",
      [x for x in CALLS if x[0] == "up"] == [("up", Key.ctrl)], CALLS)

server.release_all_keys()
CALLS.clear()
c.post("/keyboard", json={"event": "up", "key": "z", "id": "never-pressed"}, headers=token(c))
check("BUG-08", "stray release for an unseen key is ignored", CALLS == [], CALLS)
c.post("/keyboard", json={"event": "down", "key": "z", "id": "vk:z"}, headers=token(c))
CALLS.clear()
# A different id must NOT release a key another id still holds (BUG-07).
c.post("/keyboard", json={"event": "up", "key": "z", "id": "some-other-id"}, headers=token(c))
check("BUG-08", "a different id cannot steal a held key",
      [x for x in CALLS if x[0] == "up"] == [], CALLS)
# If the tracking is lost but the press is recent (client reconnected with a
# new id, say), the release is still honoured so a key cannot stay stuck.
with server._pressed_lock:
    server._pressed.clear()
CALLS.clear()
c.post("/keyboard", json={"event": "up", "key": "z", "id": "vk:z"}, headers=token(c))
check("BUG-08", "a release for a recently pressed key is honoured",
      [x for x in CALLS if x[0] == "up"] == [("up", "z")], CALLS)
server.release_all_keys()


# ---------------------------------------------------------------------------
print("\nBUG-14  batch typing is bounded")
# ---------------------------------------------------------------------------
saved_budget = getattr(server, "TEXT_MAX_SECONDS", None)
if saved_budget is not None:
    server.TEXT_MAX_SECONDS = 0.5          # keep the check fast; 10 x 0.5s = 5s
try:
    started = time.time()
    r = c.post("/text", json={"text": "a" * 10, "delay": 0.5}, headers=token(c))
    elapsed = time.time() - started
    check("BUG-14", "an over-long job is refused", r.status_code == 400, r.status_code)
    check("BUG-14", "a refused job does not run to completion", elapsed < 3, elapsed)
    r = c.post("/text", json={"text": "abc", "delay": 0.0}, headers=token(c))
    check("BUG-14", "a normal job still runs", r.status_code == 200 and r.get_json()["typed"] == 3)
finally:
    if saved_budget is not None:
        server.TEXT_MAX_SECONDS = saved_budget


# ---------------------------------------------------------------------------
print("\nBUG-15  mouse moves do not rewrite the session cookie")
# ---------------------------------------------------------------------------
c3 = client()
with c3.session_transaction() as s:
    s["stream"] = "tok-a"
r = c3.post("/mouse", json={"event": "move", "x": 0.5, "y": 0.5}, headers=token(c3))
check("BUG-15", "no Set-Cookie on a move", "Set-Cookie" not in r.headers, r.headers.get("Set-Cookie"))
check("BUG-15", "move still works", r.status_code == 204 and server.mouse.position == (400, 300),
      server.mouse.position)


# ---------------------------------------------------------------------------
print("\nBUG-03  a streaming client keeps its settings")
# ---------------------------------------------------------------------------
server._streams.clear()
saved_ttl = server.STREAM_TTL
server.STREAM_TTL = 0.05
c4 = client()
c4.post("/stream-settings", json={"fps": 5, "quality": 45, "scale": 0.5}, headers=token(c4))
with c4.session_transaction() as s:
    tok_before = s.get("stream")

# A live viewer: the generator runs and refreshes the entry every frame, so
# the entry must survive a stale sweep that happens mid-stream.
def live_frames():
    gen = server.frames(tok_before)
    for _ in range(4):
        try:
            next(gen)
        except StopIteration:
            break
        time.sleep(0.03)
    gen.close()


server.mss = types.SimpleNamespace(
    mss=lambda: types.SimpleNamespace(
        grab=lambda m: types.SimpleNamespace(size=(8, 8), bgra=bytes(8 * 8 * 4)),
        close=lambda: None),
    monitors=[])
live_frames()
c4.get("/monitors")
with c4.session_transaction() as s:
    tok_after = s.get("stream")
check("BUG-03", "token survives while streaming",
      tok_before is not None and tok_before == tok_after, (tok_before, tok_after))
entry = server._streams.get(tok_before, {})
check("BUG-03", "settings survive", (entry.get("fps"), entry.get("quality")) == (5, 45), entry)

# A viewer whose stream is not yet running still keeps its entry alive by
# polling, which is what /ping is for.
server._streams.clear()
c5b = client()
c5b.post("/stream-settings", json={"fps": 7, "quality": 55}, headers=token(c5b))
with c5b.session_transaction() as s:
    tok_ping = s.get("stream")
for _ in range(6):
    time.sleep(0.03)
    c5b.get("/ping")
check("BUG-03", "polling alone keeps the entry alive", tok_ping in server._streams, tok_ping)
server.STREAM_TTL = saved_ttl


# ---------------------------------------------------------------------------
print("\nBUG-29  frame loop survives a zero fps")
# ---------------------------------------------------------------------------
server._streams.clear()
server._streams["z"] = dict(server.DEFAULT_STREAM, touched=time.monotonic(), fps=0)
server.mss = types.SimpleNamespace(
    mss=lambda: types.SimpleNamespace(
        grab=lambda m: types.SimpleNamespace(size=(8, 8), bgra=bytes(8 * 8 * 4)),
        close=lambda: None),
    monitors=[])
try:
    gen = server.frames("z")
    first = next(gen)
    # The interval is computed after the yield, so one frame is not enough:
    # the second is what reaches the division by fps.
    second = next(gen)
    gen.close()
    check("BUG-29", "zero fps survives two frames",
          first.startswith(b"--frame") and second.startswith(b"--frame"))
except Exception as exc:  # noqa: BLE001
    check("BUG-29", "zero fps survives two frames", False, repr(exc))


# ---------------------------------------------------------------------------
print("\nBUG-25  monitor selection follows the display, not the slot")
# ---------------------------------------------------------------------------
server._monitors = []
server._monitors_at = 0.0
c5 = client()
server.monitors = lambda refresh=False: [dict(MONITORS[0]), dict(MONITORS[1])]
r = c5.post("/monitor", json={"index": 1}, headers=token(c5))
check("BUG-25", "select the second display", r.status_code == 204, r.status_code)
c5.post("/mouse", json={"event": "move", "x": 0.5, "y": 0.5}, headers=token(c5))
check("BUG-25", "mouse maps to the selected display",
      server.mouse.position == (1000, 150), server.mouse.position)
# The small display is unplugged and a different one now occupies the same
# slot. The selection must not silently follow whatever sits at that index.
WIDE = {"left": 800, "top": 0, "width": 1920, "height": 1080}
server.monitors = lambda refresh=False: [dict(MONITORS[0]), dict(WIDE)]
reported = c5.get("/monitors").get_json()
check("BUG-25", "selection does not follow the replacement",
      reported["current"] == 0, reported)
c5.post("/mouse", json={"event": "move", "x": 0.5, "y": 0.5}, headers=token(c5))
check("BUG-25", "mouse stays on a display that exists",
      server.mouse.position == (400, 300), server.mouse.position)
server.monitors = lambda refresh=False: [dict(m) for m in MONITORS]


# ---------------------------------------------------------------------------
print("\nBUG-12 / BUG-23 / BUG-30  uploads")
# ---------------------------------------------------------------------------
r = c.post("/upload", data={"file": (io.BytesIO(b"dot"), ".env")},
           content_type="multipart/form-data", headers=token(c))
check("BUG-12", "dotfile keeps its leading dot",
      r.status_code == 200 and r.get_json()["name"] == ".env", r.get_json())
r = c.post("/upload", data={"file": (io.BytesIO(b"dot2"), ".env")},
           content_type="multipart/form-data", headers=token(c))
check("BUG-30", "second upload gets a distinct name",
      r.get_json()["name"] == ".env-1", r.get_json())

# Concurrent uploads of one name: the old check-then-save could pick the same
# target twice and silently lose a file.
racers = []
for _ in range(6):
    cl = app.test_client()
    with cl.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = "tok"
    racers.append(cl)
claimed = []
claim_lock = threading.Lock()


def race_upload(cl, index):
    resp = cl.post("/upload",
                   data={"file": (io.BytesIO(b"body-%d" % index), "race.txt")},
                   content_type="multipart/form-data",
                   headers={"X-CSRF-Token": "tok"})
    if resp.status_code == 200:
        with claim_lock:
            claimed.append(resp.get_json()["name"])


race_threads = [threading.Thread(target=race_upload, args=(cl, i))
                for i, cl in enumerate(racers)]
for t in race_threads:
    t.start()
for t in race_threads:
    t.join(15)
check("BUG-30", "concurrent uploads all succeed", len(claimed) == len(racers), claimed)
check("BUG-30", "concurrent uploads get distinct names",
      len(set(claimed)) == len(claimed), claimed)
bodies = set()
for name in claimed:
    with open(os.path.join(TRANSFER, name), "rb") as handle:
        bodies.add(handle.read())
check("BUG-30", "no upload was overwritten", len(bodies) == len(claimed), sorted(bodies))
r = c.post("/upload", data={"file": (io.BytesIO(b"x"), "../../evil.txt")},
           content_type="multipart/form-data", headers=token(c))
check("BUG-23", "names cannot escape the transfer directory",
      "/" not in r.get_json()["name"] and ".." not in r.get_json()["name"], r.get_json())
check("BUG-23", "files land in the dedicated directory",
      hasattr(server, "RCFB_DIR")
      and os.path.dirname(os.path.realpath(r.get_json()["path"])) == os.path.realpath(TRANSFER),
      r.get_json()["path"])
check("BUG-23", "upload without a token is refused",
      c.post("/upload", data={"file": (io.BytesIO(b"x"), "x.txt")},
             content_type="multipart/form-data").status_code == 403)

r = c.post("/download", json={"name": "evil.txt"}, headers=token(c))
check("BUG-21", "download works over POST",
      r.status_code == 200 and r.data == b"x", r.status_code)
check("BUG-21", "download refuses traversal",
      c.post("/download", json={"name": "../server.py"}, headers=token(c)).status_code == 400)
check("BUG-21", "download refuses a missing file",
      c.post("/download", json={"name": "absent.bin"}, headers=token(c)).status_code == 404)


# ---------------------------------------------------------------------------
print("\nBUG-26  a dead capture path is reported, not hidden")
# ---------------------------------------------------------------------------
server._streams.clear()
server._streams["e"] = dict(server.DEFAULT_STREAM, touched=time.monotonic())


def exploding_grab(monitor):
    raise RuntimeError("no display")


server.mss = types.SimpleNamespace(
    mss=lambda: types.SimpleNamespace(grab=exploding_grab, close=lambda: None), monitors=[])
# With the bug present, frames() retries internally and next() never returns,
# so consume the generator on a thread we are willing to abandon. Daemon=True
# means the process can still exit if it is still stuck.
gen = server.frames("e")
deadline = time.time() + 6


def drain():
    for _ in range(getattr(server, "STREAM_MAX_FAILURES", 5) + 2):
        try:
            next(gen)
        except StopIteration:
            return


worker = threading.Thread(target=drain, daemon=True)
worker.start()
worker.join(6)
check("BUG-26", "the frame loop stops retrying eventually",
      not worker.is_alive(), "still looping after 6s")
try:
    gen.close()
except Exception:  # noqa: BLE001
    pass
check("BUG-26", "repeated failures are recorded",
      server._streams.get("e", {}).get("error") is not None,
      server._streams.get("e"))
ping = c.get("/ping")
check("BUG-26", "ping reports stream health",
      ping.status_code == 200 and "stream_error" in ping.get_json(), ping.status_code)


# ---------------------------------------------------------------------------
print("\nBUG-35  logging out does not release another session's keys")
# ---------------------------------------------------------------------------
server.release_all_keys()
CALLS.clear()
c_hold = client()
c_hold.post("/keyboard", json={"event": "down", "key": "Control", "id": "vk:ctrl"},
            headers=token(c_hold))
# A browser that has not pressed anything yet has no sid, and release_all_keys()
# reads a missing sid as "every session".
c_fresh = client()
CALLS.clear()
c_fresh.post("/logout", headers=token(c_fresh))
check("BUG-35", "a logout with no sid leaves other keys alone", CALLS == [], CALLS)
c_hold.post("/keys/release", headers=token(c_hold))
check("BUG-35", "the holder can still release its own",
      [x for x in CALLS if x[0] == "up"] == [("up", Key.ctrl)], CALLS)
server.release_all_keys()

# ---------------------------------------------------------------------------
print("\nBUG-36  stream entries expire, on one clock")
# ---------------------------------------------------------------------------
server.mss = types.SimpleNamespace(
    mss=lambda: types.SimpleNamespace(
        grab=lambda m: types.SimpleNamespace(size=(8, 8), bgra=bytes(8 * 8 * 4)),
        close=lambda: None),
    monitors=[])
server._streams.clear()
c_s = client()
c_s.post("/stream-settings", json={"fps": 4, "quality": 44}, headers=token(c_s))
with c_s.session_transaction() as sess:
    tok_s = sess.get("stream")
gen = server.frames(tok_s)
try:
    next(gen)
except StopIteration:
    pass
stamp = server._streams.get(tok_s, {}).get("touched")
check("BUG-36", "the liveness stamp uses the sweep's clock",
      stamp is not None and abs(stamp - time.monotonic()) < 30, stamp)
gen.close()
# Idle for far longer than the TTL: the entry has to go, and the next request
# has to get the defaults back rather than the settings of a session that left.
server._streams[tok_s]["touched"] = time.monotonic() - (server.STREAM_TTL + 60)
c_s.get("/ping")
check("BUG-36", "an idle entry is evicted and rebuilt",
      server._streams.get(tok_s, {}).get("fps") == server.DEFAULT_STREAM["fps"],
      server._streams.get(tok_s))

# ---------------------------------------------------------------------------
print("\nBUG-37  uploads are capped per file, and the quota holds after the write")
# ---------------------------------------------------------------------------
saved_upload = server.UPLOAD_MAX_BYTES
saved_dir_cap = server.RCFB_DIR_MAX_BYTES
saved_rcfb = server.RCFB_DIR
quota_dir = tempfile.mkdtemp(prefix="rcfb_quota_")
try:
    server.RCFB_DIR = quota_dir
    server.UPLOAD_MAX_BYTES = 1500
    server.RCFB_DIR_MAX_BYTES = 3000
    r = c.post("/upload", headers=token(c),
               data={"file": (io.BytesIO(b"x" * 2000), "big.bin")})
    check("BUG-37", "a file over the per-file cap is refused", r.status_code == 413, r.status_code)
    check("BUG-37", "the refused file is not left behind",
          os.listdir(quota_dir) == [], os.listdir(quota_dir))
    codes = []
    for index in range(4):
        r = c.post("/upload", headers=token(c),
                   data={"file": (io.BytesIO(b"x" * 1000), "q%d.bin" % index)})
        codes.append(r.status_code)
    size = server.directory_size(quota_dir)
    check("BUG-37", "the directory never exceeds the cap", size <= 3000, (size, codes))
    check("BUG-37", "the upload past the cap is refused", 507 in codes, codes)
finally:
    server.UPLOAD_MAX_BYTES = saved_upload
    server.RCFB_DIR_MAX_BYTES = saved_dir_cap
    server.RCFB_DIR = saved_rcfb

# ---------------------------------------------------------------------------
print("\nBUG-38  unhashable JSON values are rejected, not crashed on")
# ---------------------------------------------------------------------------
check("BUG-38", "an object as a key name is 400",
      c.post("/keyboard", json={"event": "down", "key": {}, "id": "x"},
             headers=token(c)).status_code == 400)
check("BUG-38", "a list as a button is 400",
      c.post("/mouse", json={"event": "click", "x": 0.5, "y": 0.5, "button": []},
             headers=token(c)).status_code == 400)

# ---------------------------------------------------------------------------
print("\nBUG-39  a release releases the key that was pressed")
# ---------------------------------------------------------------------------
CALLS.clear()
c_k = client()
c_k.post("/keyboard", json={"event": "down", "key": "a", "id": "vk:a"}, headers=token(c_k))
# The client reports event.key, which changes case while Shift is held.
c_k.post("/keyboard", json={"event": "up", "key": "A", "id": "vk:a"}, headers=token(c_k))
check("BUG-39", "the stored key is the one released",
      [x for x in CALLS if x[0] in ("down", "up")] == [("down", "a"), ("up", "a")], CALLS)

# ---------------------------------------------------------------------------
print("\nBUG-40  batch typing shares the key state machine")
# ---------------------------------------------------------------------------
CALLS.clear()
r = c.post("/text", json={"text": "ab"}, headers=token(c))
check("BUG-40", "each character is pressed and released once",
      [x for x in CALLS if x[0] in ("down", "up")] ==
      [("down", "a"), ("up", "a"), ("down", "b"), ("up", "b")], CALLS)
check("BUG-40", "nothing is left held", server._pressed == {}, server._pressed)
c_h = client()
c_h.post("/keyboard", json={"event": "down", "key": "a", "id": "vk:a"}, headers=token(c_h))
CALLS.clear()
c.post("/text", json={"text": "a"}, headers=token(c))
check("BUG-40", "a batch does not release a key another client holds",
      [x for x in CALLS if x[0] == "up"] == [], CALLS)
c_h.post("/keys/release", headers=token(c_h))

# ---------------------------------------------------------------------------
print("\nBUG-41  a stream failure is reported to the stream it belongs to")
# ---------------------------------------------------------------------------
server._streams.clear()
c_a = client()
c_b = client()
for holder, fps in ((c_a, 6), (c_b, 7)):
    holder.post("/stream-settings", json={"fps": fps}, headers=token(holder))
with c_a.session_transaction() as sess:
    tok_a = sess.get("stream")
with c_b.session_transaction() as sess:
    tok_b = sess.get("stream")
server.set_stream_error(tok_a, "capture failing: no display")
check("BUG-41", "the failed stream reports its own error",
      c_a.get("/ping").get_json()["stream_error"] == "capture failing: no display",
      c_a.get("/ping").get_json())
check("BUG-41", "another session is not told about it",
      c_b.get("/ping").get_json()["stream_error"] is None,
      c_b.get("/ping").get_json())

# ---------------------------------------------------------------------------
print("\nBUG-42  no display available fails the stream instead of hanging")
# ---------------------------------------------------------------------------
saved_monitors = server.monitors
saved_failures = server.STREAM_MAX_FAILURES
server.monitors = lambda refresh=False: []
server.STREAM_MAX_FAILURES = 3
server._streams.clear()
c_n = client()
c_n.get("/ping")
with c_n.session_transaction() as sess:
    tok_n = sess.get("stream")
gen = server.frames(tok_n)
stopped = False
try:
    for _ in range(server.STREAM_MAX_FAILURES + 3):
        next(gen)
except StopIteration:
    stopped = True
check("BUG-42", "the frame loop gives up", stopped, "still looping")
check("BUG-42", "and records why",
      server._streams.get(tok_n, {}).get("error") is not None, server._streams.get(tok_n))
check("BUG-42", "ping reports it",
      c_n.get("/ping").get_json()["stream_error"] is not None, c_n.get("/ping").get_json())
try:
    gen.close()
except Exception:  # noqa: BLE001
    pass
server.monitors = saved_monitors
server.STREAM_MAX_FAILURES = saved_failures

# ---------------------------------------------------------------------------
print("\nBUG-43  concurrent first requests share one stream entry")
# ---------------------------------------------------------------------------
server._streams.clear()
c_c = client()
c_c.get("/ping")
with c_c.session_transaction() as sess:
    first = sess.get("stream")
    sid_c = sess.get("sid")
c_c.get("/monitors")
with c_c.session_transaction() as sess:
    second = sess.get("stream")
check("BUG-43", "the entry is keyed by the session id", first == sid_c, (first, sid_c))
check("BUG-43", "a later request reuses it", first == second, (first, second))
c_c.post("/stream-settings", json={"fps": 3}, headers=token(c_c))
check("BUG-43", "settings land in the entry the stream reads",
      server._streams.get(sid_c, {}).get("fps") == 3, server._streams.get(sid_c))

# ---------------------------------------------------------------------------
print("\nBUG-44  booleans are not numbers")
# ---------------------------------------------------------------------------
check("BUG-44", "a boolean wheel delta is refused",
      c.post("/mouse", json={"event": "scroll", "x": 0.5, "y": 0.5, "dx": True, "dy": False},
             headers=token(c)).status_code == 400)
check("BUG-44", "a boolean coordinate is refused",
      c.post("/mouse", json={"event": "move", "x": True, "y": False},
             headers=token(c)).status_code == 400)

# ---------------------------------------------------------------------------
print("\nAUDIT  the 2.0 review")
# ---------------------------------------------------------------------------
server.mss = types.SimpleNamespace(
    mss=lambda: types.SimpleNamespace(
        grab=lambda m: types.SimpleNamespace(size=(8, 8), bgra=bytes(8 * 8 * 4)),
        close=lambda: None),
    monitors=[])

# AUDIT-01  A logout from a session that never pressed anything has no sid,
# and release_all_keys(None) means "every session".
server.release_all_keys()
CALLS.clear()
c_hold = client()
c_hold.post("/keyboard", json={"event": "down", "key": "Control", "id": "vk:ctrl"},
            headers=token(c_hold))
c_fresh = client()
CALLS.clear()
c_fresh.post("/logout", headers=token(c_fresh))
check("AUDIT-01", "a logout with no sid releases nothing",
      [x for x in CALLS if x[0] == "up"] == [], CALLS)
c_hold.post("/keys/release", headers=token(c_hold))
check("AUDIT-01", "the holding session still releases its own",
      [x for x in CALLS if x[0] == "up"] == [("up", Key.ctrl)], CALLS)
server.release_all_keys()

# AUDIT-02  The liveness stamp and the stale sweep must read one clock.
server._streams.clear()
c_st = client()
with c_st.session_transaction() as s:
    s["stream"] = "stamp"
server._streams["stamp"] = dict(server.DEFAULT_STREAM, touched=0.0)
gen = server.frames("stamp")
next(gen)
stamp = server._streams["stamp"]["touched"]
check("AUDIT-02", "the liveness stamp uses the sweep's clock",
      abs(stamp - time.monotonic()) < 60, stamp)
gen.close()

# AUDIT-03  An entry left behind by a session that has gone must expire.
server._streams.clear()
c_idle = client()
c_idle.post("/stream-settings", json={"fps": 4}, headers=token(c_idle))
with c_idle.session_transaction() as s:
    tok_idle = s.get("stream")
server._streams[tok_idle]["touched"] = time.monotonic() - (server.STREAM_TTL + 60)
c_idle.get("/ping")
check("AUDIT-03", "an idle stream entry is evicted",
      server._streams[tok_idle]["fps"] == server.DEFAULT_STREAM["fps"],
      server._streams.get(tok_idle))

# AUDIT-04  UPLOAD_MAX_BYTES was defined and never used, and the directory
# quota was checked only before the write.
saved_dir = server.RCFB_DIR
saved_upload = server.UPLOAD_MAX_BYTES
saved_cap = server.RCFB_DIR_MAX_BYTES
upload_dir = tempfile.mkdtemp(prefix="rcfb_quota_")
try:
    server.RCFB_DIR = upload_dir
    server.UPLOAD_MAX_BYTES = 1500
    server.RCFB_DIR_MAX_BYTES = 3000
    r = c.post("/upload", headers=token(c),
               data={"file": (io.BytesIO(b"x" * 2000), "big.bin")})
    check("AUDIT-04", "a file over the per-file cap is refused", r.status_code == 413,
          r.status_code)
    check("AUDIT-04", "the refused file is not left behind",
          os.listdir(upload_dir) == [], os.listdir(upload_dir))
    codes = []
    for index in range(4):
        r = c.post("/upload", headers=token(c),
                   data={"file": (io.BytesIO(b"x" * 1000), "q%d.bin" % index)})
        codes.append(r.status_code)
    size = server.directory_size(upload_dir)
    check("AUDIT-04", "the transfer directory never exceeds its cap", size <= 3000,
          (size, codes))
    check("AUDIT-04", "the upload past the cap is refused", 507 in codes, codes)
finally:
    server.RCFB_DIR = saved_dir
    server.UPLOAD_MAX_BYTES = saved_upload
    server.RCFB_DIR_MAX_BYTES = saved_cap

# AUDIT-05  A JSON object or list is not hashable, so the dict lookups raised
# TypeError instead of answering 400.
check("AUDIT-05", "an unhashable key name is refused",
      c.post("/keyboard", json={"event": "down", "key": {}, "id": "x"},
             headers=token(c)).status_code == 400)
check("AUDIT-05", "an unhashable button is refused",
      c.post("/mouse", json={"event": "click", "x": 0.5, "y": 0.5, "button": []},
             headers=token(c)).status_code == 400)

# AUDIT-06  key_up() released the key named by the release event rather than
# the one recorded at press time.
CALLS.clear()
c_keys = client()
c_keys.post("/keyboard", json={"event": "down", "key": "a", "id": "vk:a"},
            headers=token(c_keys))
c_keys.post("/keyboard", json={"event": "up", "key": "A", "id": "vk:a"},
            headers=token(c_keys))
check("AUDIT-06", "the release uses the key that was pressed",
      [x for x in CALLS if x[0] in ("down", "up")] == [("down", "a"), ("up", "a")], CALLS)

# AUDIT-07  /text pressed and released straight at the controller, so it could
# let go of a key another source was holding.
server.release_all_keys()
CALLS.clear()
c_type = client()
c_type.post("/text", json={"text": "ab"}, headers=token(c_type))
check("AUDIT-07", "batch typing presses and releases each character",
      [x for x in CALLS if x[0] in ("down", "up")] ==
      [("down", "a"), ("up", "a"), ("down", "b"), ("up", "b")], CALLS)
check("AUDIT-07", "and leaves nothing held", not server._pressed, server._pressed)
c_other = client()
c_other.post("/keyboard", json={"event": "down", "key": "a", "id": "vk:a"},
             headers=token(c_other))
CALLS.clear()
c_type.post("/text", json={"text": "a"}, headers=token(c_type))
check("AUDIT-07", "a batch does not release another source's key",
      [x for x in CALLS if x[0] == "up"] == [], CALLS)
server.release_all_keys()

# AUDIT-08  One process-wide stream error was shared by every session.
server._streams.clear()
c_one = client()
c_two = client()
c_one.post("/stream-settings", json={"fps": 5}, headers=token(c_one))
c_two.post("/stream-settings", json={"fps": 6}, headers=token(c_two))
with c_one.session_transaction() as s:
    tok_one = s.get("stream")
server.set_stream_error(tok_one, "capture failing: no display")
check("AUDIT-08", "a failure is reported to its own stream",
      c_one.get("/ping").get_json()["stream_error"] == "capture failing: no display")
check("AUDIT-08", "and not to another session",
      c_two.get("/ping").get_json()["stream_error"] is None,
      c_two.get("/ping").get_json())

# AUDIT-09  A host with no enumerable display looped forever without saying so.
saved_monitors = server.monitors
saved_failures = server.STREAM_MAX_FAILURES
server.monitors = lambda refresh=False: []
server.STREAM_MAX_FAILURES = 3
server._streams.clear()
server._streams["nom"] = dict(server.DEFAULT_STREAM, touched=time.monotonic())
gen = server.frames("nom")
stopped = False
try:
    for _ in range(server.STREAM_MAX_FAILURES + 2):
        next(gen)
except StopIteration:
    stopped = True
check("AUDIT-09", "a missing display ends the stream", stopped, "still looping")
check("AUDIT-09", "and records why", server._streams["nom"].get("error") is not None,
      server._streams.get("nom"))
try:
    gen.close()
except Exception:  # noqa: BLE001
    pass
server.monitors = saved_monitors
server.STREAM_MAX_FAILURES = saved_failures

# AUDIT-10  Two requests at page load each minted their own stream token.
server._streams.clear()
c_boot = client()
c_boot.get("/ping")
with c_boot.session_transaction() as s:
    first = s.get("stream")
    sid = s.get("sid")
c_boot.get("/monitors")
with c_boot.session_transaction() as s:
    second = s.get("stream")
check("AUDIT-10", "the stream entry is keyed by the session id", first == sid, (first, sid))
check("AUDIT-10", "two first requests share one entry", first == second, (first, second))
c_boot.post("/stream-settings", json={"fps": 3}, headers=token(c_boot))
check("AUDIT-10", "settings land in the entry the stream reads",
      server._streams.get(sid, {}).get("fps") == 3, server._streams.get(sid))

# AUDIT-11  bool is an int subclass, so true/false passed numeric validation.
check("AUDIT-11", "a boolean coordinate is refused",
      c.post("/mouse", json={"event": "move", "x": True, "y": False},
             headers=token(c)).status_code == 400)
check("AUDIT-11", "a boolean wheel delta is refused",
      c.post("/mouse", json={"event": "scroll", "x": 0, "y": 0, "dx": True, "dy": False},
             headers=token(c)).status_code == 400)

# ---------------------------------------------------------------------------
print("\nBASE  behaviour that must not regress")
# ---------------------------------------------------------------------------
server.mss = types.SimpleNamespace(
    mss=lambda: types.SimpleNamespace(
        grab=lambda m: types.SimpleNamespace(size=(8, 8), bgra=bytes(8 * 8 * 4)),
        close=lambda: None),
    monitors=[])
server.monitors = lambda refresh=False: [dict(m) for m in MONITORS]
CALLS.clear()
c.post("/mouse", json={"event": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 3}, headers=token(c))
c.post("/mouse", json={"event": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": -2}, headers=token(c))
check("BASE", "scroll passes signed notches",
      [x for x in CALLS if x[0] == "scroll"] == [("scroll", 0, 3), ("scroll", 0, -2)], CALLS)
check("BASE", "fractional scroll is refused",
      c.post("/mouse", json={"event": "scroll", "x": 0, "y": 0, "dx": 0, "dy": 1.5},
             headers=token(c)).status_code == 400)
c.post("/mouse", json={"event": "move", "x": 5, "y": -5}, headers=token(c))
check("BASE", "coordinates are clamped", server.mouse.position == (800, 0), server.mouse.position)
check("BASE", "newline types as Enter",
      c.post("/text", json={"text": "a\nb"}, headers=token(c)).get_json()["typed"] == 3)
check("BASE", "ping is 200 JSON", c.get("/ping").status_code == 200)
check("BASE", "clipboard round-trips",
      c.post("/clipboard", json={"text": "hi"}, headers=token(c)).status_code == 204
      and c.get("/clipboard").get_json() == {"text": "hi"})
check("BASE", "bad special action is 400",
      c.post("/special", json={"action": "nope"}, headers=token(c)).status_code == 400)
check("BASE", "logout clears the session",
      c.post("/logout", headers=token(c)).status_code == 302
      and c.get("/ping").status_code == 401)

print("\n" + ("ALL CHECKS PASSED" if not FAILURES else "%d FAILURE(S):" % len(FAILURES)))
for f in FAILURES:
    print("  - " + f)
sys.exit(1 if FAILURES else 0)
