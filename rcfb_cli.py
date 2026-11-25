#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rcfb-cli - a menu-driven control panel for the rcfb project.

    python rcfb_cli.py

WHAT THIS TOOL DELIBERATELY DOES NOT DO
---------------------------------------
* No login, no signup, no accounts, no authentication of any kind. rcfb-cli
  itself never asks who you are. (The rcfb *server* still has its own single
  password, which this tool neither creates nor bypasses.)
* No outbound network access, with exactly one exception: a single version
  verification request to the project's public GitHub blob URL. Everything
  else - every probe, every socket, the server it starts - is 127.0.0.1.
* No cloudflared, no tunnels, no port forwarding, no exposing anything
  externally. "Open" opens the *local* URL and nothing else.
* No typed commands, subcommands, or flags. You never type a command to use
  this tool. The only text you ever type is a masked value in the password
  setup screen and in Config -> Edit; both mask every character as '*' and
  neither is ever echoed, logged, or written anywhere except the config file.
* No third-party runtime packages. Standard library only: msvcrt on Windows,
  termios/tty on Unix. curses, rich, blessed, urwid and windows-curses are
  deliberately not used and must never become required.
* No installer, daemon, Windows service, or scheduled task.
* No telemetry, tracking, analytics, or persistence of anything beyond the
  small local state file used to remember the PID and log path of a server
  this tool started.

RUNNING AS SOURCE OR AS A COMPILED BUILD
----------------------------------------
This file runs directly (python rcfb_cli.py) and also compiles unchanged with
Nuitka. When compiled, __file__ points into a temporary unpack directory, so
the server is located relative to the executable's own folder instead.

It looks for a server to control in this order: rcfb.exe beside this program,
then rcfb.exe in the current directory and its parents, then server.py in the
same places. The compiled server is preferred, and when one is found the CLI
runs it directly and needs no Python at all - which is the normal case for the
protected release. Only when falling back to server.py does an interpreter
matter, and then sys.executable is not it (in a compiled build that is this
program), so a real one is resolved instead. RCFB_CLI_SERVER points at a
specific executable or folder; RCFB_CLI_PYTHON overrides the interpreter.

KEY BINDINGS (the hint bar at the bottom of every screen repeats these)
----------------------------------------------------------------------
  Up / Down      move the selection through a list
  Left / Right   move within a row, between rows of a grid, or across the
                 action bar / Yes-No toggle at the bottom
  Enter          select the highlighted item; on a leaf, also Right
  Backspace      go back one level
  Escape         go back one level, or quit from the main menu
  Q              quit from any screen
  Ctrl+C         always quits, including on the bad-version screen

LAYOUT: title bar (tool name + project version) / content / status line /
hint bar. Redraws on terminal resize and copes with 60x20; below that it
degrades to a "terminal too small" notice instead of crashing.
"""

import datetime
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque

# --------------------------------------------------------------------------
# Console primitives: the key reader is picked at import time.
# --------------------------------------------------------------------------

if os.name == "nt":  # pragma: no cover - platform specific
    import msvcrt
else:  # pragma: no cover - platform specific
    import select
    import termios
    import tty

APP_NAME = "rcfb-cli"
APP_VERSION = "1.0"

HERE = os.path.dirname(os.path.abspath(__file__))

# A Nuitka build injects __compiled__ into the module globals (not into sys).
# For a onefile build __file__ points into the temporary unpack directory that
# is deleted on exit, so anything that has to be found relative to the shipped
# program - the project, a sibling executable - is anchored to containing_dir,
# which is the folder holding the executable.
_compiled = globals().get("__compiled__")
IS_COMPILED = _compiled is not None
EXE_DIR = (getattr(_compiled, "containing_dir", None) or HERE) if IS_COMPILED else HERE

# The one and only non-local host this program ever talks to.
VERSION_HOST = "github.com"
VERSION_REPO = "chowl1306/RCFB-VERSION-CONTROL-ADELROIKUYTHWGVSE5I6JW046YVYGW0745YJCW4E"
VERSION_URL_TEMPLATE = "https://github.com/%s/blob/main/%s" % (VERSION_REPO, "%s")
VERSION_TIMEOUT = 5.0

MIN_COLS = 60
MIN_ROWS = 20

BAD_VERSION_LINES = (
    "Bad version detected.",
    "HIT CTRL+C and create a ticket with your payment ID",
)

# Populated by setup_console().
ANSI = True
UNICODE = False
GLYPH_ARROW = ">"
GLYPH_CHECK = "v"
GLYPH_CROSS = "x"
GLYPH_RETURN = "enter"
RULE_CHAR = "-"


def setup_console():
    """Enable ANSI output and UTF-8 where the platform allows it.

    Everything here is best effort. If a console refuses virtual terminal
    processing or UTF-8, we fall back to ASCII glyphs and a clear-and-reprint
    renderer rather than emitting garbage or crashing.
    """
    global ANSI, UNICODE, GLYPH_ARROW, GLYPH_CHECK, GLYPH_CROSS, GLYPH_RETURN
    global RULE_CHAR

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            try:
                kernel32.SetConsoleOutputCP(65001)
            except Exception:
                pass
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                    ANSI = False
            else:
                ANSI = False
        except Exception:
            ANSI = False

    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    UNICODE = "utf" in encoding
    if UNICODE:
        GLYPH_ARROW = "\u25b8"      # ▸
        GLYPH_CHECK = "\u2713"      # ✓
        GLYPH_CROSS = "\u2717"      # ✗
        GLYPH_RETURN = "\u21b5"     # ↵
        RULE_CHAR = "\u2500"        # ─
    return ANSI


def term_size():
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
    except Exception:
        pass
    fallback = shutil.get_terminal_size((80, 24))
    return max(1, fallback.columns), max(1, fallback.lines)


# --------------------------------------------------------------------------
# Key reading
# --------------------------------------------------------------------------

KEY_UP = "up"
KEY_DOWN = "down"
KEY_LEFT = "left"
KEY_RIGHT = "right"
KEY_ENTER = "enter"
KEY_ESC = "esc"
KEY_BACKSPACE = "backspace"
KEY_HOME = "home"
KEY_END = "end"
KEY_PGUP = "pgup"
KEY_PGDN = "pgdn"
KEY_CTRL_C = "ctrl-c"
KEY_CTRL_U = "ctrl-u"

_WIN_ARROWS = {
    b"H": KEY_UP,
    b"P": KEY_DOWN,
    b"K": KEY_LEFT,
    b"M": KEY_RIGHT,
    b"G": KEY_HOME,
    b"O": KEY_END,
    b"I": KEY_PGUP,
    b"Q": KEY_PGDN,
    b"S": "delete",
}

_WIN_CHARS = {
    b"\r": KEY_ENTER,
    b"\n": KEY_ENTER,
    b"\x1b": KEY_ESC,
    b"\x08": KEY_BACKSPACE,
    b"\x7f": KEY_BACKSPACE,
    b"\x03": KEY_CTRL_C,
    b"\x15": KEY_CTRL_U,
    b"\t": "tab",
}

# Final byte of an ANSI CSI/SS3 sequence, which some terminals send instead of
# the \x00 / \xe0 prefixed form the Windows console uses natively.
_WIN_CSI = {
    b"A": KEY_UP,
    b"B": KEY_DOWN,
    b"C": KEY_RIGHT,
    b"D": KEY_LEFT,
    b"H": KEY_HOME,
    b"F": KEY_END,
}


def _win_next_byte(timeout):
    """Consume one byte if one arrives within timeout, else None."""
    deadline = time.time() + timeout
    while True:
        if msvcrt.kbhit():
            return msvcrt.getch()
        if time.time() >= deadline:
            return None
        time.sleep(0.01)


def _win_read_key(timeout):
    first = _win_next_byte(timeout)
    if first is None:
        return None
    if first in (b"\x00", b"\xe0"):
        # What the Windows console sends for the arrow, Home/End and Page keys.
        second = msvcrt.getch()
        return _WIN_ARROWS.get(second, "other")
    if first == b"\x1b":
        # Escape, or an ANSI CSI/SS3 sequence. The Windows console does not
        # send this form, but a terminal wrapper, a pty, or a redirected
        # console can, and mistaking a sequence for a bare Escape would quit
        # the program on the first arrow key press.
        second = _win_next_byte(0.05)
        if second in (b"[", b"O"):
            final = _win_next_byte(0.05)
            return _WIN_CSI.get(final, KEY_ESC)
        return KEY_ESC
    named = _WIN_CHARS.get(first)
    if named is not None:
        return named
    try:
        char = first.decode("utf-8")
    except UnicodeDecodeError:
        return "other"
    return char if char.isprintable() else "other"


def _unix_read_byte(timeout):
    if not select.select([sys.stdin], [], [], timeout)[0]:
        return None
    data = os.read(sys.stdin.fileno(), 1)
    return data if data else None


def _unix_read_key(timeout):
    first = _unix_read_byte(timeout)
    if first is None:
        return None
    if first == b"\x03":
        return KEY_CTRL_C
    if first == b"\x15":
        return KEY_CTRL_U
    if first in (b"\r", b"\n"):
        return KEY_ENTER
    if first in (b"\x08", b"\x7f"):
        return KEY_BACKSPACE
    if first == b"\x1b":
        # Escape, or the start of a CSI sequence. A bare Escape arrives alone.
        second = _unix_read_byte(0.05)
        if second is None:
            return KEY_ESC
        if second not in (b"[", b"O"):
            return KEY_ESC
        seq = b""
        while len(seq) < 8:
            nxt = _unix_read_byte(0.05)
            if nxt is None:
                break
            seq += nxt
            if nxt.isalpha() or nxt == b"~":
                break
        return {
            b"A": KEY_UP,
            b"B": KEY_DOWN,
            b"C": KEY_RIGHT,
            b"D": KEY_LEFT,
            b"H": KEY_HOME,
            b"F": KEY_END,
            b"1~": KEY_HOME,
            b"4~": KEY_END,
            b"5~": KEY_PGUP,
            b"6~": KEY_PGDN,
        }.get(seq, "other")
    try:
        char = first.decode("utf-8", "replace")
    except Exception:
        return "other"
    return char if char.isprintable() else "other"


def read_key(timeout=0.1):
    """Return a key token, a single printable character, or None on timeout."""
    if os.name == "nt":
        return _win_read_key(timeout)
    return _unix_read_key(timeout)


class RawTerminal(object):
    """Puts the terminal into raw mode on Unix; a no-op on Windows.

    msvcrt.getch() is already unbuffered and echo-free, so Windows needs
    nothing. Unix needs termios so arrow keys arrive without Enter.
    """

    def __init__(self):
        self._fd = None
        self._saved = None

    def enter(self):
        if os.name == "nt":
            return
        try:
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        except Exception:
            self._fd = None
            self._saved = None

    def exit(self):
        if os.name == "nt" or self._fd is None or self._saved is None:
            return
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        except Exception:
            pass
        self._fd = None
        self._saved = None


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------

STYLE_NONE = None
STYLE_SEL = "sel"
STYLE_DIM = "dim"
STYLE_OK = "ok"
STYLE_BAD = "bad"
STYLE_TITLE = "title"


def _pad(text, width):
    if len(text) >= width:
        return text[:width]
    return text + " " * (width - len(text))


_DEGRADE = {
    "\u2191": "^", "\u2193": "v", "\u2190": "<", "\u2192": ">",
    "\u21b5": "enter", "\u00b7": "-", "\u2500": "-", "\u25b8": ">",
    "\u2713": "v", "\u2717": "x",
}


def degrade(text):
    """Fall back to ASCII when the console cannot render the nicer glyphs."""
    if UNICODE:
        return text
    for fancy, plain in _DEGRADE.items():
        if fancy in text:
            text = text.replace(fancy, plain)
    return text


def style_text(text, style):
    if not ANSI or style is None:
        return text
    return {
        STYLE_SEL: "\033[7m",
        STYLE_DIM: "\033[2m",
        STYLE_OK: "\033[32m",
        STYLE_BAD: "\033[31m",
        STYLE_TITLE: "\033[1;36m",
    }.get(style, "") + text + "\033[0m"


class Painter(object):
    """Draws a frame: title bar, content, status line, hint bar."""

    def __init__(self):
        self._wrote = False

    def clear(self):
        if ANSI:
            sys.stdout.write("\033[2J\033[H")
        else:
            os.system("cls" if os.name == "nt" else "clear")

    def draw(self, title, content, status, hint, cols, rows):
        """Draw one frame at the given size.

        The size is passed in rather than re-queried so the frame can never be
        laid out for one terminal size and written to another (a resize landing
        between the two calls would otherwise corrupt the frame).
        """
        if ANSI:
            sys.stdout.write("\033[?25l\033[H")
        else:
            self.clear()

        if rows >= 6:
            # title + rule + body + rule + status + hint
            head = [(style_text(_pad(degrade(title), cols), STYLE_TITLE), cols),
                    (RULE_CHAR * cols, cols)]
            tail = [(RULE_CHAR * cols, cols),
                    (style_text(_pad(degrade(status), cols), STYLE_DIM), cols),
                    (style_text(_pad(degrade(hint), cols), STYLE_DIM), cols)]
        else:
            # Too short for the title bar and the rules. The status line and the
            # hint bar are the only lines that must survive, because between them
            # they carry the reason the terminal is unusable and the key that
            # gets the user out, so the chrome is what gets dropped.
            head = []
            tail = [(style_text(_pad(degrade(status), cols), STYLE_DIM), cols),
                    (style_text(_pad(degrade(hint), cols), STYLE_DIM), cols)]

        body_rows = max(0, rows - len(head) - len(tail))
        body = list(content)[:body_rows]
        while len(body) < body_rows:
            body.append(("", STYLE_NONE))

        lines = list(head)
        for text, style in body:
            lines.append((style_text(_pad(degrade(text), cols), style), cols))
        lines.extend(tail)

        out = []
        for text, _ in lines[:rows]:
            if ANSI:
                out.append("\033[2K" + text + "\r\n")
            else:
                out.append(text + "\n")
        if ANSI:
            out.append("\033[J\033[0m")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._wrote = True

    def restore(self):
        if ANSI and self._wrote:
            sys.stdout.write("\033[?25h\033[0m\r\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------
# Project discovery and the version check
# --------------------------------------------------------------------------

VERSION_DECL_RE = re.compile(
    r"""^[ \t]*(?:__version__|VERSION|VERSION_STRING)[ \t]*=[ \t]*["'](\d+\.\d+)["']""",
    re.MULTILINE,
)
CHANGELOG_VERSION_RE = re.compile(r"Current version:[ \t]*(\d+\.\d+)")

# The compiled server, as produced by the project's build.bat / this tool's
# Build menu. When one of these is found the CLI runs it directly and needs no
# Python at all, which is the normal case for the protected release.
SERVER_EXE_NAME = "rcfb.exe" if os.name == "nt" else "rcfb"
SOURCE_NAME = "server.py"


class ServerTarget(object):
    """How to start the server, and where its files live.

    kind is "exe" for a compiled server (run it directly) or "source" for a
    checkout (run it with an interpreter). home is the working directory and
    the folder whose .env the server will actually read.
    """

    def __init__(self, kind, path, home):
        self.kind = kind
        self.path = path
        self.home = home

    @property
    def is_compiled(self):
        return self.kind == "exe"

    @property
    def env_path(self):
        return os.path.join(self.home, ".env")

    def describe(self):
        if self.is_compiled:
            return "compiled server (%s)" % os.path.basename(self.path)
        return "Python source (%s)" % SOURCE_NAME

    def location(self):
        return self.path if self.is_compiled else self.home


def search_dirs():
    """Folders to look in, in priority order.

    EXE_DIR rather than __file__: in a onefile build __file__ is inside the
    temporary unpack directory, which has no relationship to where the user
    put the program.
    """
    ordered = []
    seen = set()

    def add(path):
        if not path:
            return
        absolute = os.path.abspath(path)
        if absolute not in seen:
            seen.add(absolute)
            ordered.append(absolute)

    add(os.environ.get("RCFB_CLI_PROJECT"))
    add(EXE_DIR)
    add(os.getcwd())
    for start in (EXE_DIR, os.getcwd()):
        current = start
        for _ in range(4):
            current = os.path.dirname(current)
            if not current or current == os.path.dirname(current):
                break
            add(current)
    return ordered


def find_server_target():
    """Locate something runnable to serve the app.

    RCFB_CLI_SERVER overrides everything and may name either the compiled
    executable or a folder to search. Otherwise each candidate folder is
    checked in priority order, and within a folder the compiled server wins
    over the source: a release folder holding rcfb.exe should never fall back
    to compiling Python from a parent directory.
    """
    override = os.environ.get("RCFB_CLI_SERVER")
    if override:
        path = os.path.abspath(override)
        if os.path.isfile(path):
            return ServerTarget("exe", path, os.path.dirname(path))
        if os.path.isdir(path):
            exe = os.path.join(path, SERVER_EXE_NAME)
            if os.path.isfile(exe):
                return ServerTarget("exe", exe, path)
            if os.path.isfile(os.path.join(path, SOURCE_NAME)):
                return ServerTarget("source", path, path)
    for folder in search_dirs():
        exe = os.path.join(folder, SERVER_EXE_NAME)
        if os.path.isfile(exe):
            return ServerTarget("exe", exe, folder)
        if os.path.isfile(os.path.join(folder, SOURCE_NAME)):
            return ServerTarget("source", folder, folder)
    return None


def find_project_dir():
    """Locate the rcfb project source, for the version and requirements.

    RCFB_CLI_PROJECT overrides. Returns None when only a compiled release is
    present, which is not an error: the version check then reports itself as
    skipped and the menu still opens.
    """
    folders = search_dirs()
    for marker in (SOURCE_NAME, "CHANGELOGS.txt"):
        for folder in folders:
            if os.path.isfile(os.path.join(folder, marker)):
                return folder
    return None


def read_version(project_dir):
    """Read the project's declared version.

    The project declares it in CHANGELOGS.txt ("Current version: 1.9"); a
    VERSION assignment in server.py is honoured first if one is ever added.
    Returns None when no declaration can be found.
    """
    server = os.path.join(project_dir, "server.py")
    try:
        with open(server, encoding="utf-8", errors="replace") as handle:
            found = VERSION_DECL_RE.search(handle.read())
        if found:
            return found.group(1)
    except OSError:
        pass
    changelog = os.path.join(project_dir, "CHANGELOGS.txt")
    try:
        with open(changelog, encoding="utf-8", errors="replace") as handle:
            found = CHANGELOG_VERSION_RE.search(handle.read())
        if found:
            return found.group(1)
    except OSError:
        pass
    return None


def read_server_port(project_dir, default=6767):
    """Take the port from server.py's app.run() so the two stay in step."""
    try:
        with open(os.path.join(project_dir, "server.py"), encoding="utf-8",
                  errors="replace") as handle:
            text = handle.read()
    except OSError:
        return default
    found = re.search(r"""app\.run\([^)]*port[ \t]*=[ \t]*(\d+)""", text)
    return int(found.group(1)) if found else default


class _StrictRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect that leaves github.com."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = (urllib.parse.urlsplit(newurl).hostname or "").lower()
        if host == VERSION_HOST or host.endswith("." + VERSION_HOST):
            return urllib.request.HTTPRedirectHandler.redirect_request(
                self, req, fp, code, msg, headers, newurl
            )
        raise urllib.error.URLError("refusing redirect off %s to %s" % (VERSION_HOST, host))


def check_version(version):
    """One request to the project's public blob URL.

    Returns ("ok", None)          - the blob for this version exists
            ("bad", reason)       - a clean 404: this version was never published
            ("skipped", reason)   - anything else; startup must continue

    Only a clean 404 is treated as a bad version, because it is the one answer
    that means the version genuinely is not published. A 200 whose body is not
    the expected file - a captive portal, a TLS-inspecting proxy, an HTML
    interstitial - is not evidence of anything about the version, and calling
    it "bad" used to drop the user into a screen with no menu and no way past
    it except Ctrl+C.
    """
    if not version:
        return "skipped", "no version declared in the project"
    url = VERSION_URL_TEMPLATE % urllib.parse.quote(version)
    request = urllib.request.Request(url, headers={"User-Agent": "%s/%s" % (APP_NAME, APP_VERSION)})
    opener = urllib.request.build_opener(_StrictRedirects)
    try:
        with opener.open(request, timeout=VERSION_TIMEOUT) as response:
            body = response.read(262144).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "bad", "the version blob does not exist (HTTP 404)"
        return "skipped", "HTTP %s from %s" % (exc.code, VERSION_HOST)
    except Exception as exc:
        return "skipped", "%s" % exc
    if version not in body:
        return "skipped", "unexpected response body from %s" % VERSION_HOST
    return "ok", None


def show_bad_version(reason):
    """The only screen that is allowed to stop the tool dead.

    No menu, no continue option, no fallback. Waits for Ctrl+C.
    """
    painter = Painter()
    cols, rows = term_size()
    painter.clear()
    top = max(0, (rows - len(BAD_VERSION_LINES) - 2) // 2)
    sys.stdout.write("\n" * top)
    for line in BAD_VERSION_LINES:
        pad = max(0, (cols - len(line)) // 2)
        sys.stdout.write(" " * pad + line + "\n")
    sys.stdout.write("\n")
    sys.stdout.flush()
    while True:
        time.sleep(0.5)


# --------------------------------------------------------------------------
# Local state (PID + log of the server this tool started)
# --------------------------------------------------------------------------

def state_dir():
    base = os.environ.get("RCFB_CLI_HOME")
    if not base:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
        base = os.path.join(base, APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def state_path():
    return os.path.join(state_dir(), "state.json")


def load_state():
    import json

    try:
        with open(state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data):
    import json

    try:
        with open(state_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


def clear_state():
    try:
        os.remove(state_path())
    except OSError:
        pass

def server_pids(port):
    """The live pids that are actually serving port, plus who really holds it.

    Returns (pids, holder, stale). A recorded pid is trusted only when the port
    is genuinely held by one of the recorded processes. Being alive is not
    enough: Windows reuses pids, so a recorded number can end up belonging to
    an unrelated python.exe, and the menu used to report it as the running
    server, invent an uptime for it, and let Stop terminate it. stale is True
    when a record exists but nothing in it is serving the port any more.
    """
    state = load_state()
    recorded = []
    for key in ("server_pid", "pid"):
        pid = state.get(key)
        if pid and pid not in recorded:
            recorded.append(pid)
    alive = [pid for pid in recorded if pid_alive(pid)]
    holder = find_port_holder(port)
    if holder is not None and holder in alive:
        # The port holder is part of the record, so the whole record belongs
        # together: for a onefile build that is the child holding the port plus
        # the bootstrap that launched it, and both have to go.
        return alive, holder, False
    return [], holder, bool(alive)


def log_path():
    return os.path.join(state_dir(), "server.log")


_PYTHON_CACHE = []


def python_executable():
    """The real Python interpreter, or None when there is not one.

    A compiled build sets sys.executable to this program rather than to
    Python, so the subprocesses that genuinely need an interpreter - the
    server itself, PyInstaller, Nuitka, the capture probe - cannot use it.
    RCFB_CLI_PYTHON overrides the search. Each candidate is actually run
    before it is trusted, because on Windows a bare "python" is often the
    Microsoft Store stub, which exists on PATH but cannot run anything.
    """
    if _PYTHON_CACHE:
        return _PYTHON_CACHE[0]
    candidates = []
    override = os.environ.get("RCFB_CLI_PYTHON")
    if override:
        candidates.append(override)
    elif not IS_COMPILED:
        candidates.append(sys.executable)
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        try:
            done = subprocess.run(
                [candidate, "-c", "print(1)"],
                capture_output=True, timeout=20, stdin=subprocess.DEVNULL,
            )
        except Exception:
            continue
        if done.returncode == 0 and b"1" in done.stdout:
            _PYTHON_CACHE.append(candidate)
            return candidate
    _PYTHON_CACHE.append(None)
    return None


# --------------------------------------------------------------------------
# Process / port helpers
# --------------------------------------------------------------------------

SAFE_IMAGES = {
    "python", "python.exe", "pythonw.exe", "python3", "python3.exe",
    "py", "py.exe", "rcfb.exe", "rcfb",
}


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def process_image(pid):
    """Basename of the executable behind a PID, or None."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            try:
                size = wintypes.DWORD(1024)
                buffer = ctypes.create_unicode_buffer(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return os.path.basename(buffer.value)
                return None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        return os.path.basename(os.readlink("/proc/%d/exe" % int(pid)))
    except Exception:
        return None


def terminate_pid(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0001, False, int(pid))  # TERMINATE
            if not handle:
                return False
            try:
                return bool(kernel32.TerminateProcess(handle, 1))
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        import signal

        os.kill(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False


def port_in_use(port, host="127.0.0.1"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def find_port_holder(port):
    """PID listening on 127.0.0.1:port, or None. Local commands only."""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            return None
        needle = ":%d" % port
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(needle):
                    try:
                        return int(parts[4])
                    except ValueError:
                        continue
        return None
    # lsof -ti prints one pid per line and nothing else, so anything non-numeric
    # in it is noise. ss -ltnp embeds the pid as "pid=NNN" inside the socket
    # line; scanning the whole output for bare integers picks up address octets
    # and header numbers instead, and pid 1 is alive on every Unix, so the old
    # loop happily reported init as the port holder.
    try:
        out = subprocess.run(
            ["lsof", "-ti", "tcp:%d" % port], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        out = ""
    for line in out.split():
        if line.isdigit():
            pid = int(line)
            if pid_alive(pid):
                return pid
    needle = ":%d" % port
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "LISTEN" not in line.upper() or needle not in line:
            continue
        for token in re.findall(r"pid=(\d+)", line):
            pid = int(token)
            if pid_alive(pid):
                return pid
    return None


def probe_login(url, timeout=1.5):
    """One local HTTP probe. Returns (ok, detail)."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, "HTTP %s" % response.status
    except urllib.error.HTTPError as exc:
        # Any HTTP answer at all means something is listening and speaking.
        return True, "HTTP %s" % exc.code
    except Exception as exc:
        return False, "%s" % exc


# --------------------------------------------------------------------------
# .env handling (config screen)
# --------------------------------------------------------------------------

SECRET_KEY_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|CREDENTIAL", re.IGNORECASE)

# requirements.txt distribution name -> actual import name.
IMPORT_NAMES = {
    "pillow": "PIL",
    "pyinstaller": "PyInstaller",
    "pywin32": "win32api",
    "opencv-python": "cv2",
    "python-dateutil": "dateutil",
    "beautifulsoup4": "bs4",
    "pyyaml": "yaml",
}


def module_candidates(name):
    """Import names worth trying for a requirement.

    find_spec is case sensitive even on Windows, so a requirements.txt entry of
    "Flask" would be reported missing while "flask" imports fine.
    """
    options = [name, name.lower()]
    alias = IMPORT_NAMES.get(name.lower())
    if alias:
        options.append(alias)
    ordered = []
    for option in options:
        if option and option not in ordered:
            ordered.append(option)
    return ordered


def module_available(name):
    """Return the importable name for a module, or None. This process only."""
    for candidate in module_candidates(name):
        try:
            if importlib.util.find_spec(candidate) is not None:
                return candidate
        except (ImportError, ValueError, ModuleNotFoundError, AttributeError):
            continue
    return None


_PROBE_CACHE = {}

_PROBE_CODE = (
    "import importlib.util, json, sys\n"
    "payload = json.loads(sys.argv[1])\n"
    "imports = {}\n"
    "for name, options in payload.items():\n"
    "    found = False\n"
    "    for option in options:\n"
    "        try:\n"
    "            if importlib.util.find_spec(option) is not None:\n"
    "                found = True\n"
    "                break\n"
    "        except Exception:\n"
    "            pass\n"
    "    imports[name] = found\n"
    "print(json.dumps({'executable': sys.executable,\n"
    "                  'version': '%d.%d.%d' % sys.version_info[:3],\n"
    "                  'imports': imports}))\n"
)


def probe_python(requirements=()):
    """Ask the interpreter that will actually run the server about itself.

    The compiled build carries its own embedded CPython and cannot see the
    system site-packages, so asking this process whether flask is importable
    would answer the wrong question - and answer it wrongly. The interpreter
    that matters is the one Start will launch, so that is the one we ask.
    Returns {"executable", "version", "imports"} or None when there is no
    interpreter at all.
    """
    key = tuple(sorted(requirements))
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    result = None
    python = python_executable()
    if python is not None:
        payload = json.dumps({name: module_candidates(name) for name in requirements})
        try:
            done = subprocess.run(
                [python, "-c", _PROBE_CODE, payload],
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
            )
            if done.returncode == 0:
                result = json.loads(done.stdout.strip().splitlines()[-1])
        except Exception:
            result = None
    _PROBE_CACHE[key] = result
    return result


def bundler_available(module_name):
    """Whether a bundler is installed for the interpreter that would run it."""
    probed = probe_python([module_name])
    if probed is None:
        return module_available(module_name) is not None
    return bool(probed.get("imports", {}).get(module_name))


def env_file(project_dir):
    return os.path.join(project_dir, ".env")


def read_env_entries(path):
    """[(kind, key, value, raw_line)] where kind is 'kv' or 'raw'."""
    entries = []
    if not os.path.isfile(path):
        return entries
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.rstrip("\r\n")
                if not stripped.strip() or stripped.lstrip().startswith("#"):
                    entries.append(("raw", None, None, stripped))
                elif "=" in stripped:
                    key, value = stripped.split("=", 1)
                    entries.append(("kv", key.strip(), value.strip().strip("\"'"), stripped))
                else:
                    entries.append(("raw", None, None, stripped))
    except OSError:
        pass
    return entries


def env_values(path):
    values = {}
    for kind, key, value, _ in read_env_entries(path):
        if kind == "kv":
            values[key] = value
    return values


def write_env_value(path, key, new_value):
    """Rewrite one key in place, preserving comments and ordering."""
    entries = read_env_entries(path)
    replaced = False
    lines = []
    for kind, entry_key, value, raw in entries:
        if kind == "kv" and entry_key == key:
            lines.append("%s=%s" % (key, new_value))
            replaced = True
        else:
            lines.append(raw)
    if not replaced:
        lines.append("%s=%s" % (key, new_value))
    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.replace(temporary, path)


def redact(key, value):
    if SECRET_KEY_RE.search(key or ""):
        return "*" * min(12, max(8, len(value))) + "  (%d chars, hidden)" % len(value)
    return value

def normalize_env_value(value):
    """(value as the server will read it back, whether that differs from typed).

    server.py parses a line as `value.strip().strip("\\"'")`, so a value with
    surrounding whitespace or wrapping quotes is not stored as typed: the
    password the user believes they set and the one the server checks are
    different strings. Since the value is masked as it is typed, there is no
    way for the user to notice. Callers refuse the value and say so rather than
    writing something that will not round-trip.
    """
    normalized = value.strip().strip("\"'")
    return normalized, normalized != value


def directory_size(path):
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
    except OSError:
        return 0
    return total


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size


def uptime_text(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%dh %dm %ds" % (hours, minutes, secs)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

class Screen(object):
    """A screen renders content lines and reacts to keys."""

    interval = 0.0  # >0 asks the app to re-render on a timer
    # True when the screen is collecting free text, so the app must not
    # interpret plain characters (q, escape) as navigation or quit.
    wants_text = False
    hint = "\u2191\u2193 move   \u21b5 select   esc back   q quit"

    def __init__(self, app):
        self.app = app

    def content(self, cols, rows):
        return []

    def status(self):
        return ""

    def key(self, token):
        return None


class MaskedPrompt(Screen):
    """A masked single-line value entry.

    Every character is echoed as '*'. Nothing is logged or stored anywhere
    except the caller's chosen destination. Escape cancels.
    """

    wants_text = True

    def __init__(self, app, title, hint="type the value   \u21b5 accept   esc cancel"):
        Screen.__init__(self, app)
        self.title = title
        self.hint = hint
        self.buffer = ""
        self.message = ""

    def content(self, cols, rows):
        lines = [("", STYLE_NONE), (self.title, STYLE_NONE), ("", STYLE_NONE)]
        lines.append(("    " + "*" * len(self.buffer) + "_", STYLE_SEL))
        lines.append(("", STYLE_NONE))
        lines.append(("    %d characters" % len(self.buffer), STYLE_DIM))
        if self.message:
            lines.append(("", STYLE_NONE))
            lines.append((self.message, STYLE_BAD))
        return lines

    def key(self, token):
        if token == KEY_ESC:
            return "cancel"
        if token == KEY_ENTER:
            return "accept"
        if token in (KEY_BACKSPACE, KEY_CTRL_U):
            if token == KEY_CTRL_U:
                self.buffer = ""
            else:
                self.buffer = self.buffer[:-1]
            return None
        if isinstance(token, str) and len(token) == 1 and token.isprintable():
            self.buffer += token
        return None


class FirstRunScreen(MaskedPrompt):
    """Shown when the project has no password configured yet."""

    def __init__(self, app, env_path):
        MaskedPrompt.__init__(
            self, app, "No password is configured for the rcfb server.",
            "type the new password (masked)   \u21b5 save   esc back",
        )
        self.env_path = env_path
        self.confirm = None

    def content(self, cols, rows):
        lines = [
            ("", STYLE_NONE),
            ("This project has no PASSWORD in its .env file.", STYLE_NONE),
            ("The rcfb server refuses to start without one.", STYLE_NONE),
            ("", STYLE_NONE),
            ("Enter one now. It is masked, never logged, and written only to", STYLE_DIM),
            ("%s" % self.env_path, STYLE_DIM),
        ]
        if self.confirm is None:
            title = "Choose a password for the rcfb server."
        else:
            title = self.title
        lines += [("", STYLE_NONE), (title, STYLE_NONE), ("", STYLE_NONE),
                  ("    " + "*" * len(self.buffer) + "_", STYLE_SEL)]
        if self.message:
            lines += [("", STYLE_NONE), (self.message, STYLE_BAD)]
        return lines

    def key(self, token):
        if token == KEY_ESC:
            return "cancel"
        if token == KEY_ENTER:
            if self.confirm is None:
                if len(self.buffer) < 4:
                    self.message = "Too short - use at least 4 characters."
                    return None
                self.confirm = self.buffer
                self.buffer = ""
                self.title = "Type it again to confirm."
                return None
            if self.buffer != self.confirm:
                self.message = "They do not match. Start again."
                self.confirm = None
                self.buffer = ""
                self.title = "Choose a password for the rcfb server."
                return None
            normalized, changed = normalize_env_value(self.buffer)
            if changed:
                self.message = ("Surrounding spaces and quotes are stripped when the "
                                "server reads .env, so that password would not be the "
                                "one in force. Start again.")
                self.confirm = None
                self.buffer = ""
                self.title = "Choose a password for the rcfb server."
                return None
            try:
                write_env_value(self.env_path, "PASSWORD", normalized)
            except OSError as exc:
                self.message = "Could not write .env: %s" % exc
                return None
            self.buffer = ""
            return "saved"
        if token in (KEY_BACKSPACE, KEY_CTRL_U):
            self.buffer = "" if token == KEY_CTRL_U else self.buffer[:-1]
            return None
        if isinstance(token, str) and len(token) == 1 and token.isprintable():
            self.buffer += token
        return None


class Toggle(object):
    """A left/right Yes-No (or Build-Cancel) row - the grid's action bar."""

    def __init__(self, options, index=0):
        self.options = list(options)
        self.index = index

    def left(self):
        self.index = (self.index - 1) % len(self.options)

    def right(self):
        self.index = (self.index + 1) % len(self.options)

    def selected(self):
        return self.options[self.index]

    def render(self):
        parts = []
        for position, option in enumerate(self.options):
            label = " %s " % option
            if position == self.index:
                parts.append(style_text("[%s]" % label, STYLE_SEL))
            else:
                parts.append("[" + label + "]")
        return "   ".join(parts)


class MainMenuScreen(Screen):
    """Vertical list plus a bottom action bar, navigable as one grid."""

    hint = "\u2191\u2193 move   \u2190 action bar   \u2192 select   \u21b5 select   esc quit   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.items = [
            ("Status", "is the server running? pid, uptime, probe", StatusScreen),
            ("Start", "launch the server detached and wait for it", StartScreen),
            ("Stop", "stop the server this tool started", StopScreen),
            ("Open", "open the local url in your browser", OpenScreen),
            ("Logs", "scroll the server's output", LogsScreen),
            ("Build to EXE", "bundle the server into an executable", BuildMenuScreen),
            ("Doctor", "local-only health checks", DoctorScreen),
            ("Config", "view or edit the project's .env", ConfigMenuScreen),
            ("Quit", "leave rcfb-cli", None),
        ]
        self.index = 0
        self.row = 0          # 0 = the list, 1 = the action bar
        self.actions = Toggle(["Open", "Quit"], 0)

    def content(self, cols, rows):
        lines = []
        for position, (label, description, _) in enumerate(self.items):
            selected = (self.row == 0 and position == self.index)
            marker = GLYPH_ARROW + " " if selected else "  "
            text = "%s%-14s %s" % (marker, label, description)
            lines.append((text, STYLE_SEL if selected else STYLE_NONE))
        lines.append(("", STYLE_NONE))
        if self.row == 1:
            lines.append(("  " + self.actions.render(), STYLE_NONE))
        else:
            plain = "   ".join("[ %s ]" % option for option in self.actions.options)
            lines.append(("  " + plain, STYLE_DIM))
        return lines

    def key(self, token):
        if token == KEY_UP:
            if self.row == 1:
                self.row = 0
                self.index = len(self.items) - 1
            else:
                self.index = (self.index - 1) % len(self.items)
        elif token == KEY_DOWN:
            if self.row == 0 and self.index == len(self.items) - 1:
                self.row = 1
            elif self.row == 1:
                self.row = 0
                self.index = 0
            else:
                self.index = (self.index + 1) % len(self.items)
        elif token == KEY_LEFT:
            # Within the action bar this walks the row; on the list it is the
            # way into the bar.
            if self.row == 1:
                self.actions.left()
            else:
                self.row = 1
        elif token == KEY_RIGHT:
            # Within the action bar this walks the row. On a list item, Right
            # selects it, exactly like Enter.
            if self.row == 1:
                self.actions.right()
            else:
                return self._activate(self.items[self.index])
        elif token == KEY_ENTER:
            if self.row == 1:
                return self._action(self.actions.selected())
            return self._activate(self.items[self.index])
        elif token == KEY_BACKSPACE:
            return "back"
        return None

    def _action(self, name):
        if name == "Quit":
            return "quit"
        if name == "Open":
            self.app.open_browser()
            self.row = 0
        return None

    def _activate(self, item):
        label, _description, factory = item
        if label == "Quit":
            return "quit"
        if label == "Open":
            self.app.open_browser()
            return None
        return factory(self.app)


class StatusScreen(Screen):
    interval = 2.0
    hint = "this screen is read-only   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.probe = ("not checked", STYLE_DIM)
        self.last_probe = 0.0
        self.pid = None
        self.started = None
        self.holder = None
        self.stale = False

    def _refresh(self):
        state = load_state()
        started = state.get("started")
        # Prefer the process that actually holds the port: a onefile rcfb.exe
        # runs as a bootstrap plus a child, and the child is the server. A
        # recorded pid counts only when the port is held by one of the recorded
        # processes - a pid that is merely alive can be an unrelated process
        # that inherited a recycled number, and calling it the server also
        # invents an uptime for it.
        pids, holder, stale = server_pids(self.app.port)
        self.pid = pids[0] if pids else None
        self.started = started if pids else None
        self.holder = holder if not pids else None
        self.stale = stale
        ok, detail = probe_login(self.app.login_url)
        self.probe = ("%s (%s)" % ("reachable" if ok else "no answer", detail),
                      STYLE_OK if ok else STYLE_BAD)
        self.last_probe = time.time()

    def content(self, cols, rows):
        if time.time() - self.last_probe > 1.0:
            self._refresh()
        lines = []
        if self.pid:
            lines.append(("server        running, pid %s" % self.pid, STYLE_OK))
            if self.started:
                lines.append(("uptime        %s" % uptime_text(time.time() - self.started), STYLE_NONE))
        elif self.holder:
            lines.append(("server        port %d held by pid %s" % (self.app.port, self.holder),
                          STYLE_OK))
            lines.append(("started by    something else - not this tool", STYLE_DIM))
        elif self.stale:
            lines.append(("server        not running (the recorded pid is gone)", STYLE_BAD))
        else:
            lines.append(("server        not running (no live pid recorded)", STYLE_BAD))
        lines.append(("local url     %s" % self.app.base_url, STYLE_NONE))
        lines.append(("login probe   %s" % self.probe[0], self.probe[1]))
        if self.app.target is not None:
            lines.append(("server        %s" % self.app.target.describe(), STYLE_DIM))
            lines.append(("location      %s" % self.app.target.location(), STYLE_DIM))
        else:
            lines.append(("server        none found (%s)" % SERVER_EXE_NAME, STYLE_BAD))
        lines.append(("config file   %s" % (self.app.env_path or "none"), STYLE_DIM))
        lines.append(("version       %s" % (self.app.version or "unknown"), STYLE_DIM))
        lines.append(("log file      %s" % log_path(), STYLE_DIM))
        lines.append(("state file    %s" % state_path(), STYLE_DIM))
        lines.append(("", STYLE_NONE))
        lines.append(("The probe is a single request to %s only." % self.app.base_url, STYLE_DIM))
        return lines


class StartScreen(Screen):
    interval = 0.2
    hint = "starting the server...   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.phase = "checking"
        self.message = ""
        self.attempts = 0
        self.deadline = time.time() + 45.0
        self.started_at = time.time()
        self.process = None
        self.server_pid = None

    def _begin(self):
        port = self.app.port
        target = self.app.target
        if target is None:
            self.phase = "error"
            self.message = ("No server to run. Put %s or %s in this folder, or set "
                            "RCFB_CLI_SERVER to one." % (SERVER_EXE_NAME, SOURCE_NAME))
            return
        if port_in_use(port):
            holder = find_port_holder(port)
            image = process_image(holder) if holder else None
            self.phase = "error"
            self.message = ("Port %d is already in use by pid %s (%s). "
                            "Use Stop first, or check Status." % (port, holder, image or "unknown"))
            return
        if target.is_compiled:
            # The compiled server carries its own interpreter, its own
            # dependencies and its own copy of the web interface, so it is run
            # directly. No Python installation is needed for this path.
            command = [target.path]
        else:
            python = python_executable()
            if python is None:
                self.phase = "error"
                self.message = ("Only %s was found, and running it needs a Python "
                                "interpreter. Install Python, or set RCFB_CLI_PYTHON "
                                "to its path." % SOURCE_NAME)
                return
            command = [python, SOURCE_NAME]
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            log = open(log_path(), "ab", buffering=0)
            log.write(("\n=== started %s by %s (%s) ===\n" % (
                datetime.datetime.now().isoformat(timespec="seconds"), APP_NAME,
                target.describe())).encode())
            self.process = subprocess.Popen(
                command,
                cwd=target.home,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
                close_fds=True,
            )
        except Exception as exc:
            self.phase = "error"
            self.message = "Could not launch the server: %s" % exc
            return
        save_state({
            "pid": self.process.pid,
            "started": time.time(),
            "log": log_path(),
            "url": self.app.base_url,
            "cwd": target.home,
            "kind": target.kind,
            "target": target.location(),
        })
        self.phase = "waiting"

    def _remember_server_pid(self):
        """Record the process that actually holds the port.

        A onefile rcfb.exe is a bootstrap that unpacks itself and then starts
        the real server as a child. The pid Popen returns belongs to the
        bootstrap, and terminating it does not stop the child, so the port
        holder has to be recorded separately or Stop would leave a live server
        behind.
        """
        holder = find_port_holder(self.app.port)
        if not holder:
            return
        state = load_state()
        state["server_pid"] = holder
        state["image"] = process_image(holder)
        save_state(state)
        self.server_pid = holder

    def content(self, cols, rows):
        if self.phase == "checking":
            self._begin()
        elif self.phase == "waiting":
            if time.time() > self.deadline:
                self.phase = "error"
                if self.app.target is not None and self.app.target.is_compiled:
                    self.message = ("The server did not answer %s within 45s. Check "
                                    "Logs; the usual cause is a bad .env."
                                    % self.app.login_url)
                else:
                    self.message = ("The server did not answer %s within 45s. Check "
                                    "Logs; the usual cause is a missing dependency or "
                                    "a bad .env." % self.app.login_url)
            else:
                self.attempts += 1
                ok, detail = probe_login(self.app.login_url, timeout=1.0)
                if ok:
                    self.phase = "done"
                    self._remember_server_pid()
                    self.message = "The server answered %s." % detail
                elif self.process is not None and self.process.poll() is not None:
                    self.phase = "error"
                    self.message = ("The server exited immediately with code %s. "
                                    "Check Logs for the reason." % self.process.returncode)
        lines = []
        if self.phase == "checking":
            lines.append(("Preparing...", STYLE_NONE))
        elif self.phase == "waiting":
            spin = "|/-\\"[self.attempts % 4]
            elapsed = time.time() - self.started_at
            lines.append(("%s launching, pid %s" % (spin, self.process.pid), STYLE_NONE))
            lines.append(("running       %s" % self.app.target.describe(), STYLE_DIM))
            lines.append(("waiting for %s  (%.1fs)" % (self.app.login_url, elapsed), STYLE_DIM))
            lines.append(("probe attempts: %d" % self.attempts, STYLE_DIM))
            lines.append(("", STYLE_NONE))
            lines.append(("Output is being written to:", STYLE_DIM))
            lines.append(("  %s" % log_path(), STYLE_DIM))
        elif self.phase == "done":
            lines.append(("Server started.", STYLE_OK))
            lines.append(("running       %s" % self.app.target.describe(), STYLE_NONE))
            lines.append(("launcher pid  %s" % (self.process.pid if self.process else "?"), STYLE_NONE))
            if getattr(self, "server_pid", None):
                lines.append(("server pid    %s" % self.server_pid, STYLE_NONE))
            lines.append(("local url     %s" % self.app.base_url, STYLE_NONE))
            lines.append(("probe         %s" % self.message, STYLE_OK))
            lines.append(("", STYLE_NONE))
            lines.append(("Enter opens it in the browser, or esc returns to the menu.", STYLE_DIM))
        else:
            lines.append(("Start failed.", STYLE_BAD))
            lines.append(("", STYLE_NONE))
            for chunk in _wrap(self.message, cols - 4):
                lines.append(("  " + chunk, STYLE_NONE))
            lines.append(("", STYLE_NONE))
            lines.append(("Nothing was changed. esc returns to the menu.", STYLE_DIM))
        return lines

    def key(self, token):
        if self.phase == "done" and token == KEY_ENTER:
            self.app.open_browser()
        return None


class StopScreen(Screen):
    hint = "\u2190\u2192 choose   \u21b5 confirm   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.toggle = Toggle(["Yes, stop it", "No, go back"], 1)
        self.done = False
        self.message = ""
        self.targets = []
        self.source = ""
        self._resolve()

    def _resolve(self):
        """Collect every process that has to die for the port to be free.

        A onefile rcfb.exe is a bootstrap plus a child, and killing the
        bootstrap leaves the child serving, so both the recorded launcher pid
        and the process actually holding the port are collected. The record is
        only trusted while the port agrees with it: a recorded pid that is
        merely alive can be an unrelated program that inherited a recycled
        number, and stopping it would kill something this tool never started.
        Anything whose image is not a python or rcfb process is skipped too.
        """
        pids, holder, stale = server_pids(self.app.port)
        found = []

        def consider(pid, why):
            if not pid or pid in [p for p, _w in found]:
                return True
            if not pid_alive(pid):
                return True
            image = process_image(pid)
            if image and image.lower() in SAFE_IMAGES:
                found.append((pid, why))
                return True
            self.message = ("pid %s is a %s, which is not a server this tool started; "
                            "it will not be touched." % (pid, image))
            return False

        if pids:
            for pid in pids:
                consider(pid, "the server this tool started")
        elif holder:
            # The port is held by something the record does not name. Same
            # fallback as before, but only while the port is really in use.
            consider(holder, "the process holding port %d" % self.app.port)
        if not found and stale:
            self.message = ("The recorded server is gone and port %d is free, so "
                            "there is nothing to stop." % self.app.port)
        self.targets = found
        if found:
            self.source = found[0][1]

    def content(self, cols, rows):
        lines = []
        if self.done:
            lines.append(("Stop finished.", STYLE_OK))
            lines.append(("", STYLE_NONE))
            for chunk in _wrap(self.message, cols - 4):
                lines.append((chunk, STYLE_NONE))
            lines.append(("", STYLE_NONE))
            lines.append(("esc returns to the menu.", STYLE_DIM))
            return lines
        if not self.targets:
            lines.append(("Nothing to stop.", STYLE_BAD))
            lines.append(("", STYLE_NONE))
            for chunk in _wrap(
                self.message or "No recorded pid and nothing is listening on port %d."
                % self.app.port, cols - 4
            ):
                lines.append((chunk, STYLE_NONE))
            lines.append(("", STYLE_NONE))
            lines.append(("esc returns to the menu.", STYLE_DIM))
            return lines
        lines.append(("Found via: %s" % self.source, STYLE_DIM))
        lines.append(("", STYLE_NONE))
        for pid, why in self.targets:
            lines.append(("pid %-8s %-14s %s" % (pid, process_image(pid) or "?",
                                                 _shorten(why, cols - 28)), STYLE_NONE))
        lines.append(("", STYLE_NONE))
        lines.append(("Stop the server?", STYLE_NONE))
        lines.append(("", STYLE_NONE))
        lines.append(("  " + self.toggle.render(), STYLE_NONE))
        lines.append(("", STYLE_NONE))
        if len(self.targets) > 1:
            lines.append(("These are one server: a launcher and the process it runs.", STYLE_DIM))
        else:
            lines.append(("Only this process is terminated; nothing else is touched.", STYLE_DIM))
        return lines

    def key(self, token):
        if self.done or not self.targets:
            return None
        if token == KEY_LEFT:
            self.toggle.left()
        elif token == KEY_RIGHT:
            self.toggle.right()
        elif token == KEY_ENTER:
            if self.toggle.selected().startswith("Yes"):
                results = []
                stopped_all = True
                for pid, _why in self.targets:
                    if terminate_pid(pid):
                        results.append("terminated pid %s" % pid)
                    else:
                        stopped_all = False
                        results.append("could not terminate pid %s" % pid)
                # Give the port a moment, then say whether it is actually free
                # rather than claiming success on the strength of a syscall.
                for _ in range(20):
                    if not port_in_use(self.app.port):
                        break
                    time.sleep(0.15)
                if port_in_use(self.app.port):
                    holder = find_port_holder(self.app.port)
                    results.append("port %d is still in use by pid %s"
                                   % (self.app.port, holder))
                else:
                    results.append("port %d is now free" % self.app.port)
                # The record is only discarded once every process it names has
                # actually stopped. Clearing it after a failed termination threw
                # away the pids while the process was still running, so the next
                # Stop could no longer identify the onefile bootstrap and had
                # nothing to work from but the port.
                if stopped_all:
                    clear_state()
                else:
                    results.append("the recorded pid is kept so Stop can be retried")
                self.message = "; ".join(results) + "."
                self.done = True
        return None


class OpenScreen(Screen):
    hint = "\u21b5 open again   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.result = app.open_browser()

    def content(self, cols, rows):
        return [
            ("Opening the local url in your default browser.", STYLE_NONE),
            ("", STYLE_NONE),
            ("    %s" % self.app.base_url, STYLE_OK),
            ("", STYLE_NONE),
            (self.result, STYLE_NONE),
            ("", STYLE_NONE),
            ("This is the loopback address only. rcfb-cli never opens a tunnel,", STYLE_DIM),
            ("never exposes the server, and has no public url to offer.", STYLE_DIM),
        ]

    def key(self, token):
        if token == KEY_ENTER:
            self.result = self.app.open_browser()
        return None


class LogsScreen(Screen):
    interval = 1.0
    hint = "\u2191\u2193 scroll   \u2190\u2192 page   home/end jump   esc back   q quit"

    PAGE = 10

    def __init__(self, app):
        Screen.__init__(self, app)
        self.offset = 0
        self.lines = []
        self._load()

    def _load(self):
        path = log_path()
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 2 * 1024 * 1024))
                data = handle.read().decode("utf-8", "replace")
            self.lines = data.splitlines()[-5000:]
        except OSError:
            self.lines = []

    def content(self, cols, rows):
        self._load()
        visible = max(1, rows - 3)
        total = len(self.lines)
        if not total:
            return [("No server output yet.", STYLE_DIM),
                    ("", STYLE_NONE),
                    ("Start the server and its output will appear here.", STYLE_DIM),
                    ("Log file: %s" % log_path(), STYLE_DIM)]
        self.offset = max(0, min(self.offset, max(0, total - visible)))
        window = self.lines[self.offset:self.offset + visible]
        lines = [("showing %d-%d of %d lines   %s" % (
            self.offset + 1, self.offset + len(window), total, log_path()), STYLE_DIM)]
        for entry in window:
            lines.append((entry[:cols], STYLE_NONE))
        return lines

    def key(self, token):
        if token == KEY_UP:
            self.offset -= 1
        elif token == KEY_DOWN:
            self.offset += 1
        elif token == KEY_LEFT:
            self.offset -= self.PAGE
        elif token == KEY_RIGHT:
            self.offset += self.PAGE
        elif token == KEY_PGUP:
            self.offset -= self.PAGE * 3
        elif token == KEY_PGDN:
            self.offset += self.PAGE * 3
        elif token == KEY_HOME:
            self.offset = 0
        elif token == KEY_END:
            self.offset = 10 ** 9
        self.offset = max(0, self.offset)
        return None


class BuildJob(object):
    """A bundler subprocess whose output is streamed into a ring buffer."""

    def __init__(self, command, cwd):
        self.command = command
        self.cwd = cwd
        self.lines = deque(maxlen=4000)
        self.lock = threading.Lock()
        self.done = False
        self.returncode = None
        self.started = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def append(self, text):
        with self.lock:
            self.lines.append(text)

    def snapshot(self):
        with self.lock:
            return list(self.lines)

    def _run(self):
        try:
            process = subprocess.Popen(
                self.command, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1,
            )
        except Exception as exc:
            self.append("could not start the bundler: %s" % exc)
            self.done = True
            self.returncode = -1
            return
        try:
            for line in process.stdout:
                self.append(line.rstrip("\r\n"))
        except Exception as exc:
            self.append("output stream ended: %s" % exc)
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
        self.returncode = process.wait()
        self.done = True
        self.append("=== finished with exit code %s after %s ===" % (
            self.returncode, uptime_text(time.time() - self.started)))


def build_flavors(project_dir):
    """(key, label, description, command, module, dist) for each bundler choice.

    The commands invoke a real Python interpreter, which in a compiled build is
    not sys.executable. When there is no interpreter the command is still
    rendered for reference, but the confirmation screen refuses to run it.
    """
    separator = ";" if os.name == "nt" else ":"
    exe = "rcfb.exe" if os.name == "nt" else "rcfb"
    build_root = os.path.join(state_dir(), "build")
    python = python_executable() or "python"

    def paths(name):
        root = os.path.join(build_root, name)
        return (os.path.join(root, "dist"), os.path.join(root, "work"),
                os.path.join(root, "spec"))

    dist_one, work_one, spec_one = paths("pyinstaller-onefile")
    pyi_one = [
        python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "rcfb", "--distpath", dist_one, "--workpath", work_one,
        "--specpath", spec_one,
        "--add-data", "static%sstatic" % separator,
        "--add-data", ".env%s." % separator,
        "server.py",
    ]

    dist_dir, work_dir, spec_dir = paths("pyinstaller-onedir")
    pyi_dir = [
        python, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "rcfb", "--distpath", dist_dir, "--workpath", work_dir,
        "--specpath", spec_dir,
        "--add-data", "static%sstatic" % separator,
        "--add-data", ".env%s." % separator,
        "server.py",
    ]

    dist_nuitka, _work_nuitka, _spec_nuitka = paths("nuitka-onefile")
    nuitka_one = [
        python, "-m", "nuitka", "--onefile", "--assume-yes-for-downloads",
        "--windows-console-mode=force", "--output-dir", dist_nuitka,
        "--output-filename", exe, "--remove-output", "--python-flag=no_docstrings",
        "--include-data-dir", "static=static",
        "--include-data-files", ".env=.env",
        "--include-module", "pynput.keyboard._win32",
        "--include-module", "pynput.mouse._win32",
        "--include-module", "mss.windows",
        "server.py",
    ]

    return [
        ("pyinstaller-onefile", "PyInstaller \u00b7 onefile",
         "one self-contained %s (no python needed on the target)" % exe,
         pyi_one, "PyInstaller", dist_one),
        ("pyinstaller-onedir", "PyInstaller \u00b7 onedir",
         "a folder with the exe and its libraries next to it",
         pyi_dir, "PyInstaller", dist_dir),
        ("nuitka-onefile", "Nuitka \u00b7 onefile",
         "compiles to C first - strongest protection, slowest build",
         nuitka_one, "nuitka", dist_nuitka),
    ]


class BuildMenuScreen(Screen):
    hint = "\u2191\u2193 move   \u21b5 choose   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.flavors = build_flavors(app.project_dir)
        self.index = 0
        # Bundling compiles the Python source. A release folder that holds only
        # the compiled server has nothing to build from, so say so instead of
        # offering commands that cannot work.
        self.source_dir = app.project_dir
        self.has_source = bool(
            self.source_dir and os.path.isfile(os.path.join(self.source_dir, SOURCE_NAME))
        )

    def content(self, cols, rows):
        lines = [("Bundle the server into a standalone executable.", STYLE_NONE),
                 ("Output goes to %s, never into the project tree." % os.path.join(state_dir(), "build"),
                  STYLE_DIM),
                 ("", STYLE_NONE)]
        if not self.has_source:
            lines.append(("No %s found to build from." % SOURCE_NAME, STYLE_BAD))
            lines.append(("", STYLE_NONE))
            for chunk in _wrap(
                "Bundling compiles the Python source, so it needs the project "
                "folder, not just the compiled server. Put this tool next to the "
                "project, or set RCFB_CLI_PROJECT to it.", cols - 4
            ):
                lines.append((chunk, STYLE_NONE))
            lines.append(("", STYLE_NONE))
            lines.append(("esc goes back.", STYLE_DIM))
            return lines
        lines.append(("Source: %s" % self.source_dir, STYLE_DIM))
        lines.append(("", STYLE_NONE))
        for position, flavor in enumerate(self.flavors):
            _key, label, description, _command, module, _dist = flavor
            selected = position == self.index
            mark = GLYPH_ARROW + " " if selected else "  "
            state = "ready" if bundler_available(module) else "not installed"
            style = STYLE_SEL if selected else STYLE_NONE
            lines.append(("%s%-24s %s" % (mark, label, description), style))
            lines.append(("  %-24s %s" % ("", state),
                          STYLE_OK if bundler_available(module) else STYLE_BAD))
        lines.append(("", STYLE_NONE))
        lines.append(("Nothing is installed automatically and nothing is downloaded.", STYLE_DIM))
        return lines

    def key(self, token):
        if not self.has_source:
            return None
        if token == KEY_UP:
            self.index = (self.index - 1) % len(self.flavors)
        elif token == KEY_DOWN:
            self.index = (self.index + 1) % len(self.flavors)
        elif token in (KEY_ENTER, KEY_RIGHT):
            return BuildConfirmScreen(self.app, self.flavors[self.index])
        return None


class BuildConfirmScreen(Screen):
    hint = "\u2190\u2192 choose   \u21b5 confirm   esc back   q quit"

    def __init__(self, app, flavor):
        Screen.__init__(self, app)
        self.flavor = flavor
        self.toggle = Toggle(["Build", "Cancel"], 1)

    def content(self, cols, rows):
        _key, label, _description, command, module, dist = self.flavor
        lines = [(label, STYLE_TITLE), ("", STYLE_NONE)]
        if python_executable() is None:
            lines.append(("No Python interpreter was found.", STYLE_BAD))
            lines.append(("", STYLE_NONE))
            lines.append(("PyInstaller and Nuitka are Python tools, so this build cannot", STYLE_NONE))
            lines.append(("run without one. Install Python, or set RCFB_CLI_PYTHON.", STYLE_NONE))
            lines.append(("", STYLE_NONE))
            lines.append(("esc goes back.", STYLE_DIM))
            return lines
        if not bundler_available(module):
            lines.append(("%s is not installed, so this build cannot run." % module, STYLE_BAD))
            lines.append(("", STYLE_NONE))
            lines.append(("Install it yourself with:", STYLE_NONE))
            for chunk in _command_lines(
                [python_executable() or "python", "-m", "pip", "install", module.lower()],
                cols - 6
            ):
                lines.append(("    " + chunk, STYLE_SEL))
            lines.append(("", STYLE_NONE))
            lines.append(("rcfb-cli will not install anything and will not fetch anything.", STYLE_DIM))
            lines.append(("esc goes back.", STYLE_DIM))
            return lines
        lines.append(("This exact command will be run:", STYLE_NONE))
        lines.append(("", STYLE_NONE))
        for chunk in _command_lines(command, cols - 4):
            lines.append(("    " + chunk, STYLE_DIM))
        lines.append(("", STYLE_NONE))
        lines.append(("Working directory: %s" % self.app.project_dir, STYLE_DIM))
        lines.append(("Output directory:  %s" % dist, STYLE_DIM))
        lines.append(("", STYLE_NONE))
        lines.append(("rcfb-cli will not install anything and will not fetch anything.", STYLE_DIM))
        lines.append(("", STYLE_NONE))
        lines.append(("  " + self.toggle.render(), STYLE_NONE))
        return lines

    def key(self, token):
        _key, _label, _description, command, module, _dist = self.flavor
        if token == KEY_LEFT:
            self.toggle.left()
        elif token == KEY_RIGHT:
            self.toggle.right()
        elif token == KEY_ENTER:
            if python_executable() is None:
                return None
            if not bundler_available(module):
                return None
            if self.toggle.selected() == "Build":
                job = BuildJob(command, self.app.project_dir)
                job.start()
                self.app.build_job = job
                return BuildRunScreen(self.app, job, self.flavor)
        return None


class BuildRunScreen(Screen):
    interval = 0.3
    hint = "\u2191\u2193 scroll   \u2190\u2192 page   end follow   esc back   q quit"

    def __init__(self, app, job, flavor):
        Screen.__init__(self, app)
        self.job = job
        self.flavor = flavor
        self.offset = 10 ** 9
        self.follow = True

    def _artifact(self):
        _key, _label, _description, _command, _module, dist = self.flavor
        candidates = []
        if os.path.isdir(dist):
            for root, _dirs, files in os.walk(dist):
                for name in files:
                    if name.lower() in ("rcfb.exe", "rcfb") or name.lower().endswith(".exe"):
                        candidates.append(os.path.join(root, name))
        return candidates

    def content(self, cols, rows):
        snapshot = self.job.snapshot()
        visible = max(1, rows - 3)
        total = len(snapshot)
        if self.follow:
            self.offset = max(0, total - visible)
        self.offset = max(0, min(self.offset, max(0, total - visible)))
        head = "running" if not self.job.done else "finished (exit %s)" % self.job.returncode
        lines = [("%s - %s" % (self.flavor[1], head), STYLE_DIM)]
        for entry in snapshot[self.offset:self.offset + visible]:
            lines.append((entry[:cols], STYLE_NONE))
        if self.job.done:
            lines.append(("", STYLE_NONE))
            if self.job.returncode == 0:
                found = self._artifact()
                if found:
                    lines.append(("Build succeeded. Output:", STYLE_OK))
                    for path in found[:5]:
                        lines.append(("    %s" % path, STYLE_OK))
                else:
                    lines.append(("Build reported success but no artifact was found in %s."
                                  % self.flavor[5], STYLE_BAD))
            else:
                lines.append(("Build failed. Scroll up for the first error.", STYLE_BAD))
        return lines

    def key(self, token):
        if token == KEY_UP:
            self.follow = False
            self.offset -= 1
        elif token == KEY_DOWN:
            self.offset += 1
        elif token == KEY_LEFT:
            self.follow = False
            self.offset -= 10
        elif token == KEY_RIGHT:
            self.offset += 10
        elif token == KEY_PGUP:
            self.follow = False
            self.offset -= 30
        elif token == KEY_PGDN:
            self.offset += 30
        elif token == KEY_HOME:
            self.follow = False
            self.offset = 0
        elif token == KEY_END:
            self.follow = True
        self.offset = max(0, self.offset)
        return None


class DoctorScreen(Screen):
    interval = 5.0
    hint = "read-only checks   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.results = []
        self.checked = 0.0

    def _checks(self):
        project = self.app.project_dir
        target = self.app.target
        results = []

        if target is None:
            results.append((False, "server", "not found - put %s in this folder"
                            % SERVER_EXE_NAME))
        else:
            results.append((True, "server", "%s  %s" % (target.describe(), target.location())))

        compiled = target is not None and target.is_compiled

        if compiled:
            # The compiled server carries its own interpreter and dependencies,
            # so Python and the five packages are not needed to run it. Saying
            # otherwise would report a healthy release as broken.
            results.append((True, "python dependencies",
                            "not required - %s is self-contained" % SERVER_EXE_NAME))
        else:
            requirements = os.path.join(project or "", "requirements.txt")
            wanted = []
            try:
                with open(requirements, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        name = line.split("#")[0].strip()
                        if name:
                            wanted.append(re.split(r"[<>=!\[ ]", name)[0])
            except OSError:
                results.append((False, "requirements.txt",
                                "not found in %s" % (project or "?")))

            # One subprocess answers for the interpreter that will run the
            # server, which is the environment that decides whether it starts.
            probed = probe_python(wanted)
            if probed is None:
                results.append((False, "python interpreter",
                                "not found - the server cannot be launched"))
            else:
                version = probed.get("version", "?")
                major, minor = (version.split(".") + ["0", "0"])[:2]
                ok = (int(major), int(minor)) >= (3, 8)
                results.append((ok, "python interpreter",
                                "%s  (%s)" % (probed.get("executable", "?"), version)))
                results.append((ok, "python version", "%s (need 3.8+)" % version))

            for name in wanted:
                # A distribution name is not always the import name: the
                # project requires "Pillow", which imports as "PIL".
                if probed is None:
                    import_name = module_available(name)
                    found = import_name is not None
                    label = import_name or IMPORT_NAMES.get(name.lower(), name)
                else:
                    found = bool(probed.get("imports", {}).get(name))
                    label = IMPORT_NAMES.get(name.lower(), name)
                results.append((found, "import %s" % label,
                                "importable" if found else
                                "MISSING - the server will not start"))

        env_path = self.app.env_path
        values = env_values(env_path) if env_path else {}
        has_password = bool(values.get("PASSWORD"))
        results.append((has_password, "password config",
                        "%s has PASSWORD set" % env_path if has_password
                        else "no PASSWORD in %s" % env_path))

        # The server creates transfers next to itself, which for a compiled
        # release is the folder holding the exe.
        home = target.home if target else (project or os.getcwd())
        transfers = os.path.join(home, "transfers")
        if os.path.isdir(transfers):
            results.append((True, "transfers directory",
                            "present, %s" % human_size(directory_size(transfers))))
        else:
            results.append((False, "transfers directory",
                            "not created yet - the server makes it on demand"))

        pids, holder, stale = server_pids(self.app.port)
        if pids:
            results.append((True, "local port %d" % self.app.port,
                            "in use by pid %s, the server this tool started" % pids[0]))
        elif holder:
            results.append((True, "local port %d" % self.app.port,
                            "in use by pid %s (not started by this tool)" % holder))
        elif stale:
            results.append((False, "local port %d" % self.app.port,
                            "free - the recorded server is gone, the state is stale"))
        else:
            results.append((True, "local port %d" % self.app.port, "free"))

        ok, detail = self._capture_check()
        results.append((ok, "screen capture backend", detail))
        return results

    def _capture_check(self):
        """Enumerate a monitor in a short-lived subprocess so a crash cannot
        take the menu down with it.

        Returns True, False, or None for "cannot be determined from here". The
        last case matters for a compiled release with no interpreter present:
        rcfb.exe captures natively, so failing to probe is not a fault.
        """
        python = python_executable()
        if python is None:
            return None, "not checked - no interpreter to run the probe with"
        code = ("import mss; mons = mss.mss().monitors[1:]; "
                "print('enumerated %d monitor(s)' % len(mons))")
        try:
            done = subprocess.run([python, "-c", code], capture_output=True,
                                  text=True, timeout=20, stdin=subprocess.DEVNULL)
        except Exception as exc:
            return False, "could not run the check: %s" % exc
        if done.returncode == 0:
            return True, done.stdout.strip() or "ok"
        return False, (done.stderr.strip().splitlines() or ["failed"])[-1]

    def content(self, cols, rows):
        if time.time() - self.checked > 3.0:
            self.results = self._checks()
            self.checked = time.time()
        lines = [("Local checks only - nothing here leaves this machine.", STYLE_DIM), ("", STYLE_NONE)]
        for ok, label, detail in self.results:
            if ok is None:
                mark, style = "?", STYLE_DIM
            else:
                mark, style = (GLYPH_CHECK if ok else GLYPH_CROSS), (STYLE_OK if ok else STYLE_BAD)
            lines.append(("%s  %-26s %s" % (mark, label, detail), style))
        return lines


class ConfigMenuScreen(Screen):
    hint = "\u2191\u2193 move   \u21b5 select   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.index = 0
        self.items = [
            ("View", "show the config keys, secrets redacted", ConfigViewScreen),
            ("Edit", "pick a key and change only its value", ConfigEditScreen),
        ]

    def content(self, cols, rows):
        lines = [("Config file: %s" % self.app.env_path, STYLE_DIM), ("", STYLE_NONE)]
        for position, (label, description, _factory) in enumerate(self.items):
            selected = position == self.index
            lines.append(("%s%-8s %s" % (GLYPH_ARROW + " " if selected else "  ", label, description),
                          STYLE_SEL if selected else STYLE_NONE))
        return lines

    def key(self, token):
        if token == KEY_UP:
            self.index = (self.index - 1) % len(self.items)
        elif token == KEY_DOWN:
            self.index = (self.index + 1) % len(self.items)
        elif token in (KEY_ENTER, KEY_RIGHT):
            return self.items[self.index][2](self.app)
        return None


class ConfigViewScreen(Screen):
    interval = 2.0
    hint = "secrets stay masked   esc back   q quit"

    def content(self, cols, rows):
        path = self.app.env_path
        entries = read_env_entries(path)
        values = env_values(path)
        lines = [("Values are redacted for anything that looks like a secret.", STYLE_DIM), ("", STYLE_NONE)]
        if not values:
            lines.append(("No keys found in %s." % path, STYLE_BAD))
            lines.append(("", STYLE_NONE))
            lines.append(("Use Edit to add one, or the first-run screen to set a password.", STYLE_DIM))
            return lines
        width = max(len(key) for key in values)
        for key in values:
            lines.append(("%-*s  %s" % (width, key, redact(key, values[key])), STYLE_NONE))
        lines.append(("", STYLE_NONE))
        lines.append(("%d key(s). Comment lines and ordering are preserved on edit." % len(values),
                      STYLE_DIM))
        return lines


class ConfigEditScreen(Screen):
    hint = "\u2191\u2193 pick a key   \u21b5 edit its value   esc back   q quit"

    def __init__(self, app):
        Screen.__init__(self, app)
        self.index = 0
        self.message = ""
        self.prompt = None
        self.target_key = None
        self.wants_text = False

    def _keys(self):
        return list(env_values(self.app.env_path).keys())

    def content(self, cols, rows):
        if self.prompt is not None:
            lines = [("Editing %s" % self.target_key, STYLE_NONE), ("", STYLE_NONE)]
            lines += self.prompt.content(cols, rows - 2)
            return lines
        keys = self._keys()
        lines = [("Pick a key with up/down. Only its value can change.", STYLE_NONE), ("", STYLE_NONE)]
        if not keys:
            lines.append(("There are no keys to edit.", STYLE_BAD))
            return lines
        self.index = min(self.index, len(keys) - 1)
        for position, key in enumerate(keys):
            selected = position == self.index
            lines.append(("%s%s" % (GLYPH_ARROW + " " if selected else "  ", key),
                          STYLE_SEL if selected else STYLE_NONE))
        lines.append(("", STYLE_NONE))
        lines.append(("Values are typed masked, exactly like the password screen.", STYLE_DIM))
        if self.message:
            lines.append(("", STYLE_NONE))
            lines.append((self.message, STYLE_OK))
        return lines

    def key(self, token):
        if self.prompt is not None:
            outcome = self.prompt.key(token)
            if outcome == "cancel":
                self.prompt = None
                self.wants_text = False
            elif outcome == "accept":
                new_value = self.prompt.buffer
                normalized, changed = normalize_env_value(new_value)
                if not normalized:
                    # An empty value is written as "KEY=" and the server reads
                    # that as no value at all: PASSWORD= makes it exit with
                    # "PASSWORD is missing from .env" while the screen had just
                    # reported the edit as saved.
                    self.message = "A value cannot be empty. Nothing was saved."
                elif changed:
                    self.message = ("Surrounding spaces and quotes are stripped by the "
                                    "server, so that value would not be read back as "
                                    "typed. Nothing was saved.")
                elif self.target_key == "PASSWORD" and len(normalized) < 4:
                    self.message = "Too short - use at least 4 characters. Nothing was saved."
                else:
                    try:
                        write_env_value(self.app.env_path, self.target_key, normalized)
                        self.message = "Saved %s (%d characters)." % (
                            self.target_key, len(normalized))
                    except OSError as exc:
                        self.message = "Could not save: %s" % exc
                self.prompt = None
                self.wants_text = False
            return None
        keys = self._keys()
        if not keys:
            return None
        if token == KEY_UP:
            self.index = (self.index - 1) % len(keys)
        elif token == KEY_DOWN:
            self.index = (self.index + 1) % len(keys)
        elif token in (KEY_ENTER, KEY_RIGHT):
            self.target_key = keys[self.index]
            self.message = ""
            self.prompt = MaskedPrompt(
                self.app,
                "New value for %s (masked)." % self.target_key,
                "type the value   \u21b5 save   esc cancel",
            )
            self.wants_text = True
        return None


class TooSmallScreen(Screen):
    hint = "q quit   resize the window to continue"

    def content(self, cols, rows):
        # The notice comes first so it survives even when only one body row fits.
        return [("Terminal too small.", STYLE_BAD),
                ("", STYLE_NONE),
                ("Need at least %dx%d; this window is %dx%d." % (MIN_COLS, MIN_ROWS, cols, rows),
                 STYLE_NONE),
                ("", STYLE_NONE),
                ("Resize and the menu comes back.", STYLE_DIM)]


def _wrap(text, width):
    width = max(10, width)
    words = str(text).split()
    lines = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _shorten(text, width):
    """Trim a phrase to fit, keeping it recognisable."""
    text = str(text)
    if width < 4 or len(text) <= width:
        return text
    return text[:max(1, width - 3)] + "..."


def _command_lines(command, width):
    """Render a command list readably, one argument per line when it is long."""
    joined = " ".join(_quote(part) for part in command)
    if len(joined) <= width:
        return [joined]
    lines = []
    current = ""
    for part in command:
        piece = _quote(part)
        if not current:
            current = piece
        elif len(current) + 1 + len(piece) <= width:
            current += " " + piece
        else:
            lines.append(current)
            current = "    " + piece
    if current:
        lines.append(current)
    return lines


def _quote(part):
    return '"%s"' % part if " " in part else part


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class App(object):
    def __init__(self, target, project_dir, version):
        self.target = target
        self.project_dir = project_dir
        self.version = version
        self.port = read_server_port(project_dir) if project_dir else 6767
        self.base_url = "http://127.0.0.1:%d/" % self.port
        self.login_url = "http://127.0.0.1:%d/login" % self.port
        self.painter = Painter()
        self.terminal = RawTerminal()
        self.stack = []
        self.running = True
        self.dirty = True
        self.last_size = (0, 0)
        self.last_draw = 0.0
        self.status = ""
        self.build_job = None

    @property
    def env_path(self):
        """The .env the server will actually read.

        This follows the server target, not the project source: a compiled
        rcfb.exe reads the .env beside it, so that is the file Config and the
        first-run screen must edit.
        """
        if self.target is not None:
            return self.target.env_path
        if self.project_dir:
            return env_file(self.project_dir)
        return None

    # -- navigation --------------------------------------------------------
    def push(self, screen):
        self.stack.append(screen)
        self.dirty = True
        return None

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()
        self.dirty = True
        return None

    def replace_stack(self, screen):
        self.stack = [screen]
        self.dirty = True

    @property
    def screen(self):
        return self.stack[-1]

    def at_root(self):
        return len(self.stack) <= 1

    # -- actions -----------------------------------------------------------
    def open_browser(self):
        try:
            opened = webbrowser.open(self.base_url, new=2)
        except Exception as exc:
            return "Could not open a browser: %s" % exc
        return ("Asked the default browser to open it." if opened
                else "The system reported that no browser could be opened.")

    # -- key handling ------------------------------------------------------
    def handle(self, token):
        if token == KEY_CTRL_C:
            self.running = False
            return

        # A screen that is collecting text owns every key: plain characters
        # belong in its buffer, and it decides what escape means.
        if self.screen.wants_text:
            self._dispatch(self.screen.key(token))
            self.dirty = True
            return

        if isinstance(token, str) and len(token) == 1 and token.lower() == "q":
            self.running = False
            return
        if token == KEY_ESC:
            if self.at_root():
                self.running = False
            else:
                self.pop()
            return
        if token == KEY_BACKSPACE:
            if not self.at_root():
                self.pop()
            return
        self._dispatch(self.screen.key(token))

    def _dispatch(self, outcome):
        """Apply whatever a screen's key() returned."""
        if outcome == "quit":
            self.running = False
        elif outcome == "back":
            self.pop()
        elif outcome == "saved":
            self.status = "Password saved."
            self.replace_stack(MainMenuScreen(self))
        elif outcome == "cancel":
            pass  # the screen already handled it
        elif isinstance(outcome, Screen):
            self.push(outcome)
        elif outcome is not None:
            self.status = str(outcome)
        self.dirty = True

    # -- main loop ---------------------------------------------------------
    def run(self):
        self.terminal.enter()
        try:
            while self.running:
                cols, rows = term_size()
                if (cols, rows) != self.last_size:
                    self.last_size = (cols, rows)
                    self.dirty = True
                    self.painter.clear()
                if self.screen.interval and time.time() - self.last_draw >= self.screen.interval:
                    self.dirty = True
                if self.dirty:
                    self.draw(cols, rows)
                    self.dirty = False
                    self.last_draw = time.time()
                token = read_key(0.1)
                if token is not None:
                    self.handle(token)
        finally:
            self.terminal.exit()
            self.painter.restore()

    def draw(self, cols, rows):
        # A standalone release has no source to read a version from, in which
        # case the title simply omits it rather than claiming "unknown".
        if self.version:
            title = " %s  \u00b7  rcfb %s " % (APP_NAME, self.version)
        else:
            title = " %s " % APP_NAME
        if cols < MIN_COLS or rows < MIN_ROWS:
            content = TooSmallScreen(self).content(cols, rows)
            hint = TooSmallScreen.hint
            # The notice has to survive an extremely short terminal, where the
            # body gets no rows at all, so it also goes in the status line - and
            # it outranks any stale status message left by an earlier action.
            # The first words are the ones that matter: at 10 columns only
            # "too small" fits, so that is what goes first.
            status = "too small: need at least %dx%d" % (MIN_COLS, MIN_ROWS)
        else:
            content = self.screen.content(cols, rows - 5)
            hint = self.screen.hint
            status = self.status or self.screen.status()
        self.painter.draw(title, content, status, hint, cols, rows)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    setup_console()

    # What to run, and separately what to read the version from. A protected
    # release holds the compiled server with no source beside it, so the two
    # are looked up independently: the server is required, the source is not.
    target = find_server_target()
    project_dir = find_project_dir()
    if target is None and project_dir is None:
        sys.stdout.write(
            "%s: could not find a server to control.\n"
            "\n"
            "Looked in this program's own folder, the current directory, and\n"
            "their parent folders for %s or %s. Do one of these:\n"
            "  * put this tool next to the server, or\n"
            "  * set RCFB_CLI_SERVER to the %s file or its folder, e.g.\n"
            "        set RCFB_CLI_SERVER=C:\\path\\to\\%s\n"
            % (APP_NAME, SERVER_EXE_NAME, SOURCE_NAME, SERVER_EXE_NAME, SERVER_EXE_NAME)
        )
        return 2

    version = read_version(project_dir) if project_dir else None

    # The one outbound call this program ever makes, before anything is drawn.
    verdict, reason = check_version(version)
    if verdict == "bad":
        try:
            show_bad_version(reason)
        except KeyboardInterrupt:
            pass
        return 1

    app = App(target, project_dir, version)
    if verdict == "skipped":
        app.status = "version check skipped (%s)" % reason

    if target is not None and target.is_compiled:
        app.status = app.status or "running the compiled server %s" % os.path.basename(target.path)

    env_path = app.env_path
    if env_path and not env_values(env_path).get("PASSWORD"):
        app.stack = [FirstRunScreen(app, env_path)]
    else:
        app.stack = [MainMenuScreen(app)]

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.terminal.exit()
        app.painter.restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
