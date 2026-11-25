<!--
About-RCFB.md - product reference for rcfb
Pinned in #about-rcfb. Update the version number and the changelog
section whenever a new build ships. Search for [ to find every
placeholder that still needs your commercial terms filled in.
-->

# About rcfb

rcfb is a self-hosted, browser-based remote desktop for Windows.

It runs on the machine you want to control, opens a password-gated web
page, and lets you see that machine's screen and drive its keyboard and
mouse from any browser on any device, including a phone or a tablet.
Nothing is installed on the device you control it from. Nothing is
routed through a third-party service. The screen leaves your machine
only as a stream to the browser you opened.

---

## At a glance

| | |
|---|---|
| Current version | 1.8 |
| Platform | Windows (primary). The server runs on Linux/macOS in principle; the host-lock and Ctrl+Alt+Del actions are Windows-only. |
| Server side | Python 3, Flask, pynput, mss, Pillow, pyperclip |
| Client side | Any modern browser. No extension, no app, no install. |
| Transport | HTTP on localhost, or HTTPS through any tunnel or reverse proxy you choose |
| Delivery | Standalone `rcfb.exe` (no Python required) or run from source |
| Auth | Single password, single fixed username |
| Data stored | Only what you place in the transfers directory. No telemetry. No analytics. No phone-home. |

---

## What it does

### Screen

- Live MJPEG stream of the selected monitor, captured with `mss`
  and encoded with Pillow.
- Multi-monitor support with a picker in the System tab. Displays
  are re-enumerated on a 5-second cache, so docking, unplugging, or a
  resolution change is picked up without restarting the server.
- The selected display is remembered by its geometry, not by its slot
  index. Unplugging monitor 2 will not hand your session to whatever
  is plugged in next.
- Three quality presets (Low, Medium, High) set fps, JPEG quality, and
  scale independently, and apply to an open stream without reconnecting.
- If the stream drops, the client retries with exponential backoff
  (500 ms doubling to a 10 s cap) and returns to "connected" on its
  own. A backgrounded tab that loses its stream is reopened when you
  come back.

### Input

- On-screen keyboard with 83 keys, all function keys F1 to F12, arrows,
  and the navigation cluster. Both Shift, Ctrl, Alt, and Meta keys are
  tracked separately.
- Touchpad: one finger drags the remote cursor, two fingers scroll, and
  a pinch zooms. Tap to click. Tuned so a sub-notch trackpad movement
  never fires a stray wheel event.
- Virtual mouse buttons for left, middle, and right, plus scroll up and
  down arrows. All show a held state and report `aria-pressed`.
- Physical keyboard passthrough. Type on your real keyboard and it goes
  to the remote, while the local browser keeps Tab for its own focus
  navigation. Send Tab to the remote with the on-screen key.
- Batch text: type or paste a whole string, optionally with a
  per-character delay for slow remote applications. Control characters
  map correctly (`\n` to Enter, `\t` to Tab, `\b` to Backspace,
  `\x1b` to Esc, `\x7f` to Delete).
- Every held key is released when the tab loses focus, the page is
  hidden, or you navigate away. No key is left physically held on the
  host after you disconnect.

### Clipboard and files

- Read the remote clipboard into your device, or push your device's
  clipboard to the remote.
- Drag files onto the screen area to send them, or fetch a named file
  from the remote. Transfers live in a dedicated directory, not in your
  real Downloads folder.
- Upload name collisions are resolved atomically with suffixes
  (`file.txt`, `file-1.txt`, and so on), so two simultaneous uploads
  of the same name can never overwrite each other.

### System

- Host lock: locks the Windows session remotely, after a confirmation
  prompt.
- Ctrl+Alt+Del: attempts to send the secure attention sequence.
  Windows only accepts this from a privileged service, so expect it to
  be refused; the prompt says so up front.

### Security and safety

The full model is below. In short: password-gated, CSRF-protected,
rate-limited, hardened cookies, sandboxed transfers, and all device
access serialized.

---

## Security model

rcfb assumes the person on the other end of the connection is you. It
is not a multi-user product and does not pretend to be one.

### Authentication

- One fixed username (`Root`) and one password, read from a `.env` file
  beside the executable. Change the password by editing `.env`; no
  rebuild is required.
- Sessions are Flask sessions, signed with a key derived from the
  password.

### Session cookie

- `HttpOnly`, so JavaScript cannot reach it.
- `SameSite=Lax`, so it is not sent on cross-site form posts.
- `Secure` is set automatically when the request arrives over HTTPS,
  detected via the proxy's forwarded scheme. It is left off over plain
  `http://127.0.0.1`, where a Secure cookie would never be sent back.

### CSRF

- Every state-changing request carries a per-session token, issued by
  the server and sent as a header, a form field, or a body field (for
  `sendBeacon`, which cannot set headers).
- A `403` triggers one automatic re-fetch and retry, so a session
  replaced in another tab does not silently reject every keystroke.

### Rate limiting

- Eight failed logins within 300 seconds lock the client out. The
  lockout is keyed on the client's real address. A forwarded header is
  trusted only when the request genuinely arrived from the local proxy.
- The failure table is bounded, so a flood of forged addresses cannot
  grow it without limit.

### Transfers

- Uploads and downloads are confined to a dedicated transfers
  directory. Your real `~/Downloads` is never exposed and never
  written to.
- Per-upload cap is 256 MB. The transfers-directory cap is 2 GB, and
  the server refuses uploads with `507` once the directory is full.
- `/download` is POST-only, so a stray cross-site navigation cannot
  pull a file.
- Path traversal attempts (`../`, `..\`, and encoded variants) are
  rejected.

### Device access

- All keyboard, mouse, and special-action calls go through a single
  lock, so concurrent requests cannot interleave OS input events.
- The lock is held only around individual calls, never across a typing
  delay, so a slow typing job never blocks other input.

### Key ownership

Held keys are tracked per session. Logging out on your phone cannot
release a key you are holding from your laptop.

### What rcfb does not do

- No account system, no signup, no per-user permissions.
- No telemetry, no analytics, no update checks, no external network
  calls of any kind. The server speaks HTTP on `127.0.0.1:6767` and
  nothing else.
- No built-in TLS. Put it behind a tunnel or reverse proxy if you need
  HTTPS, or keep it on localhost if you do not.

---

## How it works

```
your browser ──HTTP──▶ rcfb server ──▶ OS input queue (pynput)
(any device)            (on the host) ──▶ screen capture (mss)
                                      │
                                      ▼
                          JPEG frames (MJPEG)
```

The server is a single Flask process listening on `127.0.0.1:6767`.
It serves a small HTML/JS client, streams JPEG frames as
`multipart/x-mixed-replace`, and accepts JSON commands for input,
clipboard, files, and stream settings. Each session gets its own
stream configuration, so two browsers can watch at different quality
without interfering with each other.

---

## Installation

### Option A: the standalone executable

1. Place `rcfb.exe` anywhere you like.
2. Put a `.env` file beside it containing:

```
PASSWORD=your-password-here
```

   If you do not, the password bundled into the build is used, and you
   should change it.
3. Run `rcfb.exe`.
4. Open `http://127.0.0.1:6767` in a browser.

No Python, no pip, no dependencies. Everything the program needs is
inside the one file. The transfers directory is created next to the
executable the first time you upload something.

### Option B: from source

1. Install Python 3.
2. Run `pip install -r requirements.txt`.
3. Create `.env` with `PASSWORD=...` beside the source.
4. Run `python server.py`.
5. Open `http://127.0.0.1:6767`.

On Windows, `start.bat` runs steps 2 to 4 and also opens a Cloudflare
quick tunnel if `cloudflared` is on your PATH.

### Reaching it from outside the machine

rcfb itself does not expose anything. To reach the server from another
device you need to put something in front of it:

- A tunnel (Cloudflare, ngrok, Tailscale Funnel, and the like) pointing
  at `localhost:6767`. This is what `start.bat` does by default.
- A reverse proxy on your LAN, if the machine is already on a network
  you trust.
- Port forwarding, if you understand the risk. Not recommended without
  TLS and additional access control.

Whichever you choose, the server itself stays bound to loopback.

---

## Configuration

### `.env`

| Key | Meaning |
|---|---|
| `PASSWORD` | Required. The login password. Everything else is derived from it. |

When running from `rcfb.exe`, `.env` is read from beside the executable
first, and from the copy bundled inside the build second. That means
you can change the password without rebuilding.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `RCFB_DIR` | `./transfers` | Where uploads are stored and downloads are served from. Must be writable. |

### Tunable constants (in `server.py`)

Change these only if you know why you are changing them.

| Constant | Default | Meaning |
|---|---|---|
| `FPS_RANGE` | 1 to 30 | Bounds for stream fps |
| `QUALITY_RANGE` | 20 to 95 | Bounds for JPEG quality |
| `SCALE_RANGE` | 0.25 to 1.0 | Bounds for stream downscale |
| `LOGIN_MAX_FAILURES` | 8 | Failed logins before lockout |
| `LOGIN_WINDOW` | 300 s | Window over which failures are counted |
| `MONITOR_TTL` | 5 s | How often displays are re-enumerated |
| `STREAM_TTL` | 600 s | How long an idle stream config survives |
| `TEXT_MAX_SECONDS` | 30 s | Longest a single batch-typing job may run |
| `STREAM_MAX_FAILURES` | 20 | Consecutive capture failures before the stream gives up |
| `UPLOAD_MAX_BYTES` | 256 MB | Per-upload cap |
| `RCFB_DIR_MAX_BYTES` | 2 GB | Total transfers-directory cap |

---

## Using it

### Logging in

Open the URL and enter the password. The username is fixed (`Root`) and
is pre-filled.

### The three tabs

The Keyboard tab has a text box for batch typing, Paste and Copy
buttons for the clipboard, and the full on-screen keyboard. The
function-key row scrolls horizontally; the rest of the keyboard fits
the panel.

The Mouse tab has a touchpad and the five virtual mouse buttons. One
finger on the touchpad drags the cursor, two fingers scroll and pinch
to zoom, and a tap is a left click.

The System tab has the monitor picker, stream presets, typing speed,
file send and fetch, host lock, and Ctrl+Alt+Del.

### Viewing the screen

- Zoom with the `+` and `-` controls, or pinch on the touchpad. The
  zoom anchors on the remote cursor, so zooming in keeps the pointer
  where it is.
- While zoomed, the picture pans to keep the remote cursor centred,
  clamped so the image never pulls away from the viewport edge.

### Sending files

Drag files onto the screen area. They land in the transfers directory
on the host. To fetch a file, type its name in the System tab's file
box and press Get. The file is saved to your device's downloads.

### Signing out

The Sign out button in the top bar releases every held key, ends the
session, and returns you to the login page.

---

## Supported keys

Everything on a standard keyboard: A to Z, 0 to 9, all punctuation,
Enter, Space, Tab, Escape, Backspace, Delete, Insert, Home, End, Page
Up, Page Down, all four arrows, Caps Lock, Shift, Control, Alt, Meta
(both sides tracked independently), and F1 to F12.

Control characters sent through batch text map as follows:

| Character | Sends |
|---|---|
| `\n` | Enter |
| `\r` | Enter |
| `\t` | Tab |
| `\b` | Backspace |
| `\x1b` (Esc) | Escape |
| `\x7f` (Del) | Delete |

---

## Known limits

These are stated rather than hidden.

- Single user. One password, one username. rcfb is designed for you
  reaching your own machine, not for teams.
- No built-in TLS. You bring the tunnel or proxy if you need HTTPS.
- Downloads buffer in memory. Fetching a file loads it fully into
  browser memory before saving. Fine for documents, not for
  multi-gigabyte archives.
- Ctrl+Alt+Del usually fails. Windows only accepts the secure
  attention sequence from a privileged service. The button exists and
  the prompt warns you.
- Host lock and Ctrl+Alt+Del are the only untested paths. They were not
  exercised during testing because doing so would lock the machine
  under test. Their wiring is verified; their effect is not.
- Touch gestures were verified as applied styling, not as a real
  gesture. The test harness has no touchscreen.
- Windows first. The server will start on Linux and macOS, but the
  host-lock and Ctrl+Alt+Del actions are Windows-specific and the
  executable build targets Windows.

---

## Versioning

rcfb uses a two-part version number, `n.n`, incremented by exactly
`0.1` per release. There is no three-part version and no suffix.
Crossing `1.0` carried no special meaning; it was just the next step.

The single source of truth for the version is the project's changelog
file. The current version is 1.8.

### Recent releases

| Version | Date | Summary |
|---|---|---|
| 1.8 | 2026-10-08 | Packaged as a single executable |
| 1.7 | 2026-10-06 | Bug 34: separate scroll accumulators |
| 1.6 | 2026-10-06 | Bugs 31 to 33: nested scroll, background polling, staleness |
| 1.5 | 2026-10-06 | Bugs 28 to 30: bounded table, fps guard, atomic uploads |
| 1.4 | 2026-10-06 | Bugs 25 to 27: display identity, stream health, confirms |
| 1.3 | 2026-10-06 | Bugs 22 to 24: cookie security, transfer dir, device lock |
| 1.2 | 2026-10-06 | Bugs 19 to 21: lost text, clobbered box, CSRF |
| 1.1 | 2026-10-06 | Bugs 16 to 18: pre-load geometry, gestures, hidden tab |
| 1.0 | 2026-10-06 | Bugs 13 to 15: trust, typing budget, session churn |

Full history is in the project's changelog.

---

## Licensing and purchase

<!-- FILL IN: replace every bracketed block below with your real terms. -->

**Price:** [YOUR PRICE]

**What the license covers:** [e.g. one machine, one user, perpetual
use of the purchased major version, free updates within that major
version]

**What the license does not cover:** [e.g. redistribution, resale,
reverse engineering, use on machines you do not own or control]

**Refund policy:** [e.g. refunds within 14 days if the software does
not work as described, minus payment processor fees]

**Payment methods:** [list them]

**How to buy:** Open a purchase ticket in this server. A staff member
will confirm the details and provide payment instructions.

**How delivery works:** After payment is confirmed you receive the
Customer role, which unlocks the downloads channel. Your license key
and the current build are posted there.

---

## Support

Before opening a ticket, check the FAQ and Known Issues channels. Most
questions are answered there.

When opening a ticket, include:

- Your license key (or the email you bought with)
- The version of rcfb you are running
- What you expected to happen
- What actually happened
- Any error message, in full
- Your Windows version

**Support contact:** [FILL IN - e.g. "Open a ticket in #support" or an
email address]

**Response time:** [FILL IN - e.g. "usually within 24 hours"]

---

## FAQ

**Does rcfb send my screen anywhere?**
No. The screen is streamed only to the browser you opened. There is no
telemetry, no analytics, no cloud service, and no external network
call of any kind. You can verify this by watching the process's
connections. It only ever talks to `127.0.0.1:6767`.

**Why does Windows Defender flag it?**
Remote-desktop tools and executables built with Nuitka or PyInstaller
are often flagged heuristically because they capture the screen and
inject input, behaviour that malware also exhibits. The binary is not
signed. Add an exclusion if you trust the source, which is this server.

**Can I change the password?**
Yes. Edit the `.env` file beside the executable and restart. No
rebuild is required.

**Can I use it on more than one machine?**
See the license section above. Technically, the executable will run
anywhere; the license governs what you are permitted to do.

**Does it work on Mac or Linux?**
The server will start and the screen and input will work. Host lock
and Ctrl+Alt+Del are Windows-only. The packaged executable is built
for Windows.

**Can two people connect at once?**
Yes, and their keyboard state is kept separate. Logging out on one
device will not release keys held on the other.

**Where do uploaded files go?**
Into the transfers directory, which is `transfers/` beside the
executable unless you override it with the `RCFB_DIR` environment
variable. Not into your real Downloads folder.

**Is there a mobile app?**
No. The client is a web page. It works in any modern mobile browser,
which is better than an app because there is nothing to install.

**Why does Ctrl+Alt+Del not work?**
Windows only accepts the secure attention sequence from a process with
special privileges. The operating system refuses a normal user
process. This is a Windows restriction, not an rcfb limitation.

**Can I hide the fact that it is running?**
No, and you should not want to. It is a normal foreground process. It
does not install a service, does not add a startup entry, and does not
hide from the task manager.

---

## Troubleshooting

**The login page will not load.**
The server is not running, or something else is on port 6767. Check the
console output of `rcfb.exe` or `server.py`.

**"unauthorized" on every action.**
Your session expired. Reload the page and log in again.

**"bad csrf token".**
Your session was replaced, usually because you logged in from another
tab or device. Reload the page.

**The stream shows "reconnecting" forever.**
The host may have gone to sleep, or the capture backend is failing. If
the status line shows a capture error, restart the server.

**Uploads are refused.**
You have hit the 2 GB transfers-directory cap. Clear old files from
the transfers directory.

**Files I uploaded are not in my Downloads folder.**
Correct. Uploads go to the transfers directory by design, not to your
real Downloads. Fetch them back through the client, or copy them out
manually.

---

## Technical reference

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/login` | Login page |
| POST | `/login` | Password submission |
| GET | `/` | Main client |
| GET | `/csrf` | Issue this session's CSRF token |
| POST | `/logout` | End the session, release held keys |
| GET | `/ping` | Liveness and current stream error |
| GET | `/screen` | MJPEG stream |
| GET | `/monitors` | List displays (`?refresh=1` to re-enumerate) |
| POST | `/monitor` | Select the active display |
| POST | `/stream-settings` | Set fps, quality, scale |
| POST | `/keyboard` | Key down or up |
| POST | `/keys/release` | Release this session's held keys |
| POST | `/text` | Type a whole string |
| POST | `/mouse` | Move, click, or scroll |
| GET | `/clipboard` | Read the remote clipboard |
| POST | `/clipboard` | Set the remote clipboard |
| POST | `/upload` | Multipart upload |
| POST | `/download` | Fetch a named file |
| POST | `/special` | Host lock, Ctrl+Alt+Del |

Every state-changing endpoint requires the CSRF token and an
authenticated session.

### Stream presets

| Preset | fps | JPEG quality | Scale |
|---|---|---|---|
| Low | 5 | 45 | 0.5 |
| Medium | 10 | 70 | 0.75 |
| High | 15 | 85 | 1.0 |

Presets are applied to an open stream without reconnecting.

### Client-side timings

| Constant | Value | Meaning |
|---|---|---|
| Retry base | 500 ms | First reconnect delay |
| Retry cap | 10 s | Maximum reconnect delay |
| Ping interval | 5 s | Latency poll (paused while the tab is hidden) |
| Touchpad sensitivity | 1.5 | Pointer-drag multiplier |
| Tap threshold | 300 ms | Longest press counted as a tap |
| Zoom range | 1x to 4x | Viewport zoom |
| Zoom step | 1.5x | Per button press |
| Wheel notch | 100 px | Pixels per scroll notch |
| Line-mode notch | 16 px | Pixels per notch for line-mode deltas |

---

rcfb is a local tool. It talks only to the address you give it.
