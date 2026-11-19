#!/usr/bin/env python3
"""Serve the console desktop as live frames and inject input.

Run in the console session (so clicks and keys reach the GUI):

    launchctl asuser <uid> python3 desktop-server.py <port> <token-hash> <host>

The second argument is the lowercase hex SHA-256 of the session token, never the token
itself: the hash is what the workflow was dispatched with, so a run's metadata is not
an access credential. A request carries the token in `?t=` and is served only when its
hash matches.

Frames are written by a separate capture process (bin/screen-stream.swift) to FRAME;
this process only serves the newest one. Capturing from here would put this interpreter
in the TCC responsibility chain, and macOS raises a modal consent dialog there that
ignores synthetic input.

The page holds one WebSocket: frames come down as binary messages and input goes up as
JSON. The writer always sends the newest frame and the page drops a frame if it is still
painting the previous one, so a slow link shows the present instead of a backlog. Input
is fire-and-forget: TCP keeps the order, and a keystroke costs one one-way trip, not a
request/response round trip per character.

`/stats` reports liveness and, through the flags the workflow's self-test writes, whether
input was verified: `input_checked` says the probe ran, `input_ok` says the click changed
the screen. A client turns its light green only when frames and input both work.

Uploads go the other way: POST /upload?t=<token>&name=<dir> stores a tar.gz in the
tester's home, so a directory from their machine appears inside the session.
"""
import base64
import collections
import hashlib
import hmac
import http.server
import json
import os
import pwd
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

PORT = int(sys.argv[1])
# Only the SHA-256 of the session token reaches the runner: it is a workflow input,
# visible in the run's metadata, and a raw token there would be the access credential.
TOKEN_HASH = sys.argv[2].lower()
if not re.fullmatch(r"[0-9a-f]{64}", TOKEN_HASH):
    print("token hash must be 64 hex chars", file=sys.stderr)
    sys.exit(2)
BIND = sys.argv[3] if len(sys.argv) > 3 else "0.0.0.0"
FRAME = "/tmp/desktop-frame.jpg"
BEAT = FRAME + ".beat"                  # touched every second by the capture process
STAT = FRAME + ".stat"                  # capture counters written by the same process
INPUT_OK = "/tmp/desktop-input-ok"      # the workflow's self-test saw the click land
INPUT_CHECKED = "/tmp/desktop-input-checked"   # the self-test ran at all
UPLOAD_MAX = 4 * 1024 ** 3              # refuse anything larger: 4 GiB of tar.gz
UPLOAD_CHUNK = 1024 * 1024
UPLOAD_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
INPUT_PORT = int(os.environ.get("INPUT_PORT", "7777"))
INPUT_SOCKET = [None]                   # connection to the capture process's input agent
INPUT_QUEUE = collections.deque()       # one ordered queue: keystrokes must not reorder
INPUT_COND = threading.Condition()

LOCK = threading.Lock()
COND = threading.Condition(LOCK)
CACHE = [0.0, b""]                      # mtime, bytes of the newest frame
LAST_FULL = [b""]                       # newest full frame, for a viewer's first paint
BARE_SEQ = [0]                          # header counter for headerless capture output
STATS = {"frames": 0, "recent": []}     # capture timestamps in the last two seconds
DIMS = [0, 0]
KEYCODES = {
    "Enter": 36, "Return": 36, "Tab": 48, "Backspace": 51, "Delete": 117,
    "Escape": 53, " ": 49, "ArrowLeft": 123, "ArrowRight": 124, "ArrowDown": 125,
    "ArrowUp": 126, "Home": 115, "End": 119, "PageUp": 116, "PageDown": 121,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}
APPLE_MODIFIERS = {"cmd": "command down", "ctrl": "control down",
                   "alt": "option down", "shift": "shift down"}
# Virtual keycodes for chord targets: the physical key, so Cmd+Shift+4 is the
# screenshot shortcut and not Cmd+$.
CHAR_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26,
    "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45,
    "m": 46, ".": 47, "`": 50,
}


def cliclick_path():
    for candidate in ("/opt/homebrew/bin/cliclick", "/usr/local/bin/cliclick"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("cliclick") or ""


CLICLICK = cliclick_path()


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(cmd, 1, "", "")


def home_dir():
    """The tester's home directory, where uploads land.

    `launchctl asuser` re-associates the session but does not change the uid: this
    process still runs as root, so neither HOME nor the uid's home is the tester's
    (~/.config under /var/root). The console user is the account the desk belongs to.
    """
    configured = os.environ.get("IOS_CI_UPLOAD_HOME")
    if configured:
        return configured
    account = console_account()
    if account:
        return pwd.getpwuid(account[0]).pw_dir
    return pwd.getpwuid(os.getuid()).pw_dir


def console_account():
    """(uid, gid) of the user owning /dev/console, or None when it cannot be read."""
    try:
        result = subprocess.run(["stat", "-f%Su", "/dev/console"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    user = result.stdout.strip()
    if result.returncode or not user or user == "root":
        return None
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_gid


def hand_over(path):
    """Give an upload to the console user; the server itself runs as root."""
    account = console_account()
    if os.getuid() != 0 or account is None:
        return
    uid, gid = account
    for base, dirs, names in os.walk(path):
        for name in dirs + names:
            try:
                os.lchown(os.path.join(base, name), uid, gid)
            except OSError:
                pass
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def archive_problem(path):
    """Why this tar.gz cannot be extracted safely, or "" when it is fine."""
    try:
        listing = subprocess.run(["tar", "-tzf", path], capture_output=True,
                                 text=True, timeout=120)
        verbose = subprocess.run(["tar", "-tvzf", path], capture_output=True,
                                 text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return f"could not read the archive: {error}"
    if listing.returncode or verbose.returncode:
        return "the upload is not a tar.gz"
    for member in listing.stdout.splitlines():
        if not member:
            continue
        if member.startswith("/") or ".." in member.replace("\\", "/").split("/"):
            return f"unsafe member: {member[:120]}"
    for line in verbose.stdout.splitlines():
        # bsdtar/GNU both render a link as "name -> target".
        if " -> " not in line:
            continue
        target = line.split(" -> ", 1)[1].strip()
        if target.startswith("/") or ".." in target.split("/"):
            return f"unsafe link target: {target[:120]}"
    return ""


def tree_size(root):
    files = 0
    size = 0
    for base, _dirs, names in os.walk(root):
        for entry in names:
            files += 1
            try:
                size += os.lstat(os.path.join(base, entry)).st_size
            except OSError:
                pass
    return files, size


def osa(script):
    return run(["osascript", "-e", script])


def streamer_stat():
    try:
        with open(STAT, "rb") as handle:
            return json.loads(handle.read() or b"{}")
    except (OSError, ValueError):
        return {}


DIMS_CACHE = [0.0, 1024, 768]


def display_size():
    now = time.time()
    if DIMS_CACHE[0] > now:
        return DIMS_CACHE[1], DIMS_CACHE[2]
    stat = streamer_stat()
    if stat.get("dw") and stat.get("dh"):
        size = int(stat["dw"]), int(stat["dh"])
    else:
        size = dimensions(frame()[1])
    DIMS_CACHE[:] = [now + 2.0, size[0], size[1]]
    return size


def input_socket():
    connection = INPUT_SOCKET[0]
    if connection is not None:
        return connection
    try:
        connection = socket.create_connection(("127.0.0.1", INPUT_PORT), timeout=1.0)
        connection.settimeout(2.0)
        INPUT_SOCKET[0] = connection
        return connection
    except OSError:
        return None


def deliver(item):
    """Send one command to the capture process; False when its agent is absent."""
    connection = input_socket()
    if connection is None:
        return False
    try:
        connection.sendall(json.dumps(item).encode() + b"\n")
        return True
    except OSError:
        INPUT_SOCKET[0] = None
        try:
            connection.close()
        except OSError:
            pass
        return False


def fallback(item):
    """Slow path (cliclick/osascript) for when the capture process is not running."""
    kind = item.get("t")
    if kind == "char":
        text = item.get("s", "")
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        osa(f'tell application "System Events" to keystroke "{escaped}"')
    elif kind == "key":
        code = item.get("code")
        mods = item.get("flags") or []
        using = ""
        if mods:
            using = " using {" + ", ".join(APPLE_MODIFIERS[m] for m in mods) + "}"
        osa(f'tell application "System Events" to key code {code}{using}')
    elif kind == "scroll":
        dy = item.get("dy", 0)
        code = "121" if dy > 0 else "116"
        for _ in range(min(5, max(1, abs(int(dy)) // 60 or 1))):
            osa(f'tell application "System Events" to key code {code}')
    elif kind == "mouse" and CLICLICK:
        x, y = int(item.get("x", 0)), int(item.get("y", 0))
        action = item.get("type")
        button = item.get("button", "left")
        if action in ("move", "drag"):
            run([CLICLICK, f"dm:{x},{y}"])
        elif action == "down" and button == "left":
            run([CLICLICK, f"dd:{x},{y}"])
        elif action == "up" and button == "left":
            run([CLICLICK, f"du:{x},{y}"])
        elif action == "up" and button == "right":
            run([CLICLICK, f"rc:{x},{y}"])


def enqueue(item):
    with INPUT_COND:
        if len(INPUT_QUEUE) > 400:
            return                      # far behind: dropping beats growing latency
        INPUT_QUEUE.append(item)
        INPUT_COND.notify()


def input_worker():
    """One thread, in order: concurrent cliclick calls used to reorder keystrokes."""
    while True:
        with INPUT_COND:
            while not INPUT_QUEUE:
                INPUT_COND.wait()
            item = INPUT_QUEUE.popleft()
            # A run of pointer moves only needs its newest position; skipping the
            # older ones keeps a drag from lagging behind the pointer.
            while (item.get("t") == "mouse" and item.get("type") == "drag"
                   and INPUT_QUEUE
                   and INPUT_QUEUE[0].get("t") == "mouse"
                   and INPUT_QUEUE[0].get("type") == "drag"):
                item = INPUT_QUEUE.popleft()
        if not deliver(item):
            fallback(item)


def dimensions(data):
    if not data.startswith(b"\xff\xd8") and not data.startswith(b"\x89PNG"):
        # Frames may carry a "RECT ...\n" header; measure the image, not the header.
        start = data.find(b"\xff\xd8")
        if start < 0:
            start = data.find(b"\x89PNG")
        if start > 0:
            data = data[start:]
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":            # JPEG: walk to the first SOF frame header
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return (int.from_bytes(data[i + 7:i + 9], "big"),
                        int.from_bytes(data[i + 5:i + 7], "big"))
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return 1024, 768


def frame_locked():
    """Return (mtime, bytes) of the newest frame. Caller holds COND."""
    try:
        stamp = os.stat(FRAME).st_mtime
    except OSError:
        return CACHE[0], CACHE[1]
    if stamp != CACHE[0] or not CACHE[1]:
        try:
            CACHE[1] = open(FRAME, "rb").read()
            CACHE[0] = stamp
        except OSError:
            pass
    return CACHE[0], CACHE[1]


def frame():
    with COND:
        return frame_locked()


def watch():
    """Notice new frames, keep the cache warm, and track the capture rate."""
    last = None
    while True:
        time.sleep(0.004)
        try:
            stamp = os.stat(FRAME).st_mtime
        except OSError:
            continue
        if stamp == last:
            continue
        last = stamp
        with COND:
            _, data = frame_locked()
            if data and not data.startswith(b"RECT "):
                # The screencapture fallback writes a bare JPEG. Every such frame is a
                # whole picture, so give it the header the page expects; a client that
                # asks for a full frame just gets the next one.
                width, height = dimensions(data)
                BARE_SEQ[0] += 1
                data = (f"RECT F 0 0 {width} {height} {BARE_SEQ[0]}\n".encode() + data)
            if data.startswith(b"RECT F "):
                LAST_FULL[0] = data
            now = time.time()
            STATS["frames"] += 1
            STATS["recent"] = [t for t in STATS["recent"] if now - t <= 2.0] + [now]
            DIMS[:] = list(dimensions(data))
            COND.notify_all()


WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(sock, count):
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def ws_frame(payload, opcode):
    size = len(payload)
    if size < 126:
        head = bytes((0x80 | opcode, size))
    elif size < 65536:
        head = bytes((0x80 | opcode, 126)) + struct.pack(">H", size)
    else:
        head = bytes((0x80 | opcode, 127)) + struct.pack(">Q", size)
    return head + payload


def ws_message(sock):
    """Read one client message; returns (opcode, payload) or None when closed."""
    opcode = None
    parts = []
    while True:
        b0, b1 = recv_exact(sock, 2)
        fin = b0 & 0x80
        op = b0 & 0x0F
        masked = b1 & 0x80
        size = b1 & 0x7F
        if size == 126:
            size = struct.unpack(">H", recv_exact(sock, 2))[0]
        elif size == 127:
            size = struct.unpack(">Q", recv_exact(sock, 8))[0]
        mask = recv_exact(sock, 4) if masked else b"\0\0\0\0"
        data = recv_exact(sock, size) if size else b""
        if masked:
            data = bytes(byte ^ mask[i & 3] for i, byte in enumerate(data))
        if op == 0x8:
            return None
        if op == 0x9:
            sock.sendall(ws_frame(data, 0xA))
            continue
        if op == 0xA:
            continue
        if op in (0x1, 0x2):
            opcode = op
            parts = [data]
        elif op == 0x0:
            parts.append(data)
        if fin:
            return opcode, b"".join(parts)


def ws_items(event):
    """Translate one client input message into agent commands, in order."""
    kind = event.get("t")
    width, height = display_size()

    def point():
        fx = max(0.0, min(1.0, float(event.get("fx", 0.5))))
        fy = max(0.0, min(1.0, float(event.get("fy", 0.5))))
        return fx * width, fy * height

    if kind == "full":
        return [{"t": "full"}]
    if kind == "mouse":
        x, y = point()
        action = event.get("e")
        if action == "drag":
            return [{"t": "mouse", "type": "drag", "x": x, "y": y}]
        if action == "down":
            return [{"t": "mouse", "type": "down", "x": x, "y": y, "button": "left"}]
        if action == "up":
            return [{"t": "mouse", "type": "up", "x": x, "y": y, "button": "left"}]
        if action in ("click", "rclick"):
            button = "right" if action == "rclick" else "left"
            try:
                count = max(1, min(3, int(event.get("count", 1))))
            except (TypeError, ValueError):
                count = 1
            out = []
            for state in range(1, count + 1):
                out.append({"t": "mouse", "type": "down", "x": x, "y": y,
                            "button": button, "count": state})
                out.append({"t": "mouse", "type": "up", "x": x, "y": y,
                            "button": button, "count": state})
            return out
        return []
    if kind == "wheel":
        x, y = point()
        try:
            dy = max(-240.0, min(240.0, float(event.get("dy", 0))))
        except (TypeError, ValueError):
            return []
        return [{"t": "scroll", "dy": dy, "x": x, "y": y}]
    if kind == "type":
        text = event.get("s", "")
        return [{"t": "char", "s": text}] if text else []
    if kind == "key":
        name = event.get("name", "")
        mods = [m for m in event.get("mods", "").split(",") if m in APPLE_MODIFIERS]
        if mods:
            code = CHAR_CODES.get(name, KEYCODES.get(name))
            return [{"t": "key", "code": code, "flags": mods}] if code is not None else []
        if len(name) == 1:
            return [{"t": "char", "s": name}]
        code = KEYCODES.get(name)
        return [{"t": "key", "code": code}] if code is not None else []
    if kind == "config":
        item = {"t": "config"}
        for key, cast in (("width", int), ("fps", float), ("quality", float)):
            if key in event:
                try:
                    item[key] = cast(event[key])
                except (TypeError, ValueError):
                    pass
        return [item] if len(item) > 1 else []
    return []


PAGE = """<!doctype html><meta charset="utf-8">
<title>ios-ci desktop</title>
<style>
 html,body{margin:0;height:100%;background:#000;overflow:hidden;
   font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#9aa0a6}
 #screen{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
   background:#000;cursor:crosshair;touch-action:none;user-select:none;
   -webkit-user-select:none;-webkit-user-drag:none}
 #hud{position:fixed;left:10px;bottom:10px;background:#000b;padding:6px 9px;
   border-radius:6px;white-space:pre;pointer-events:none}
</style>
<canvas id=screen width=1024 height=768></canvas><div id=hud>connecting…</div>
<script>
const token = new URLSearchParams(location.search).get('t');
const canvas = document.getElementById('screen');
const ctx = canvas.getContext('2d', {alpha: false});
const hud = document.getElementById('hud');
const KEYNAMES = new Set(['Enter','Tab','Backspace','Delete','Escape','ArrowUp',
  'ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown',
  'F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12']);
const MODIFIER_KEYS = new Set(['Shift','Control','Alt','Meta','CapsLock','OS','AltGraph']);
const PUNCTUATION = {Minus:'-',Equal:'=',BracketLeft:'[',BracketRight:']',Backslash:'\\\\',
  Semicolon:';',Quote:"'",Comma:',',Period:'.',Slash:'/',Backquote:'`',Space:' '};
let hint = '';

function log(message) { hud.textContent = message; }

// One WebSocket carries frames down and input up. Input never waits for a reply:
// TCP keeps the order, so a keystroke costs one one-way trip instead of a full
// request/response round trip per character.
let ws = null, decoding = false, haveFull = false, shownAt = [];
let lastSeq = 0, fullAskedAt = 0;
// A crop only composes onto the frame before it. When one is missed - the page was
// still decoding, the server coalesced two frames, the decode failed - painting the
// next crop would leave the missed change on screen forever, which looks like streaks
// and torn regions. So a gap freezes the canvas and asks the streamer for a full
// frame; one is also written every 30 frames, so the ask is a shortcut, not a need.
function needFull(urgent) {
  const unresolved = !haveFull;
  haveFull = false;
  const now = performance.now();
  // Ask whenever a good frame turns into a gap. While the gap stays unresolved, back
  // off - unless a frame was dropped mid-decode, which may have been the full frame
  // being delivered, and on an idle screen nothing else would replace it.
  if (unresolved && now - fullAskedAt < (urgent ? 200 : 1000)) return;
  fullAskedAt = now;
  send({t: 'full'});
}
let streamWidth = 0, tuningAt = 0, stepDirection = 0, stepAt = 0;
let arrivalAt = [], displayWidth = 0;
// A slow client paints far below the rate frames arrive, and every frame costs pixels
// twice over: once to decode and once to fill the canvas. Compare what arrived with
// what painted - not with the capture rate, which is also low when the screen is idle
// - and step the size to the client. A client that keeps up grows back toward the
// display's own width instead of stopping at an arbitrary cap.
function tune() {
  const now = performance.now();
  arrivalAt = arrivalAt.filter(t => now - t <= 2000);
  if (now - tuningAt < 6000 || !streamWidth || arrivalAt.length < 8) return;
  const arrived = arrivalAt.length / 2;
  const shown = shownAt.filter(t => now - t <= 2000).length / 2;
  const cap = Math.max(560, Math.min(displayWidth || 960, 1440));
  let next = streamWidth;
  if (shown < arrived * 0.6) next = Math.max(560, Math.round(streamWidth * 0.8 / 40) * 40);
  else if (streamWidth < cap) next = Math.min(cap, Math.round(streamWidth * 1.15 / 40) * 40);
  if (next === streamWidth) return;
  // The restart itself starves the next window, so never reverse a step within 20 s.
  const up = next > streamWidth;
  if (stepDirection !== 0 && (up !== (stepDirection > 0)) && now - stepAt < 20000) return;
  tuningAt = now;
  stepDirection = up ? 1 : -1;
  stepAt = now;
  streamWidth = next;
  fetch('/config?width=' + next + '&t=' + encodeURIComponent(token));
}
setInterval(tune, 3000);
function connect() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(scheme + '://' + location.host + '/ws?t=' + encodeURIComponent(token));
  ws.binaryType = 'blob';
  ws.onmessage = async event => {
    if (typeof event.data === 'string') return;
    arrivalAt.push(performance.now());
    if (decoding) { needFull(true); return; }   // lost mid-decode: maybe the full
    decoding = true;
    try {
      const bytes = new Uint8Array(await event.data.arrayBuffer());
      let end = 0;
      while (end < bytes.length && bytes[end] !== 10) end++;
      const fields = new TextDecoder().decode(bytes.subarray(0, end)).split(' ');
      if (fields[0] !== 'RECT' || fields.length !== 7) return;
      const full = fields[1] === 'F';
      const x = +fields[2], y = +fields[3], w = +fields[4], h = +fields[5];
      const seq = +fields[6];
      if (w <= 0 || h <= 0 || !seq) return;
      if (full) {
        haveFull = true;
        lastSeq = seq;
        streamWidth = w;
        if (canvas.width !== w || canvas.height !== h) {
          canvas.width = w;
          canvas.height = h;
          fit();
        }
      } else if (!haveFull || seq !== lastSeq + 1) {
        needFull();
        return;                        // a crop without its predecessor would streak
      }
      const bmp = await createImageBitmap(new Blob([bytes.subarray(end + 1)]));
      ctx.drawImage(bmp, x, y);
      lastSeq = seq;
      bmp.close();
      const now = performance.now();
      shownAt.push(now);
      while (shownAt.length && now - shownAt[0] > 2000) shownAt.shift();
    } catch (err) {
    } finally {
      decoding = false;
    }
  };
  ws.onclose = () => { ws = null; log('stream lost; reconnecting'); setTimeout(connect, 1000); };
  ws.onerror = () => { try { ws.close(); } catch (err) {} };
}
connect();

function send(message) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(message));
}

// One scale for both axes, centred: the picture keeps the screen's aspect, and the
// menu bar and Dock are not stretched sideways. The element is sized to the picture,
// so a pointer maps straight through its own box.
function fit() {
  const nw = canvas.width, nh = canvas.height;
  if (!nw || !nh) return;
  const scale = Math.min(innerWidth / nw, innerHeight / nh);
  canvas.style.width = Math.round(nw * scale) + 'px';
  canvas.style.height = Math.round(nh * scale) + 'px';
}
addEventListener('resize', fit);
fit();
function imageXY(e) {
  const r = canvas.getBoundingClientRect();
  if (!r.width || !r.height || !canvas.width || !canvas.height) return null;
  return [(e.clientX - r.left) * canvas.width / r.width,
          (e.clientY - r.top) * canvas.height / r.height];
}

let down = false, start = [0, 0], dragging = false;
canvas.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;          // right button belongs to contextmenu alone
  const point = imageXY(e);
  if (!point) return;
  canvas.setPointerCapture(e.pointerId);
  start = point; down = true; dragging = false;
  e.preventDefault();
});
canvas.addEventListener('pointermove', e => {
  if (!down) return;
  const point = imageXY(e);
  if (!point) return;
  const [fx, fy] = point;
  if (!dragging && Math.abs(fx - start[0]) * canvas.width < 3 &&
      Math.abs(fy - start[1]) * canvas.height < 3) return;
  if (!dragging) { dragging = true; send({t:'mouse', e:'down', fx:start[0], fy:start[1]}); }
  send({t:'mouse', e:'drag', fx, fy});
});
canvas.addEventListener('pointerup', e => {
  if (e.button !== 0 || !down) return;
  down = false;
  const point = imageXY(e);
  if (!point) return;
  if (dragging) send({t:'mouse', e:'up', fx:point[0], fy:point[1]});
  else send({t:'mouse', e:'click', fx:point[0], fy:point[1], count:e.detail || 1});
  dragging = false;
});
canvas.addEventListener('pointercancel', () => { down = false; dragging = false; });
canvas.addEventListener('dragstart', e => e.preventDefault());
canvas.addEventListener('contextmenu', e => {
  e.preventDefault();
  const point = imageXY(e);
  if (point) send({t:'mouse', e:'rclick', fx:point[0], fy:point[1]});
});
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const point = imageXY(e) || [0.5, 0.5];
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
  const dy = Math.round(Math.max(-240, Math.min(240, e.deltaY * unit)));
  send({t:'wheel', dy, fx:point[0], fy:point[1]});
}, {passive: false});

let text = '', flushTimer = null;
function flush() {
  if (!text) return;
  const t = text; text = '';
  send({t:'type', s:t});
}
function modifiers(e) {
  const list = [];
  if (e.metaKey) list.push('cmd');       // Super on Linux/Windows, Command on macOS
  if (e.ctrlKey) list.push('ctrl');
  if (e.altKey) list.push('alt');
  if (e.shiftKey) list.push('shift');
  return list.join(',');
}
// Chords use the physical key (e.code), so Shift+4 sends Command+Shift+4 rather than
// the "$" the browser reports for that keystroke.
function chordKey(e) {
  const code = e.code || '';
  if (/^Key[A-Z]$/.test(code)) return code[3].toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code[5];
  if (/^Numpad[0-9]$/.test(code)) return code[6];
  if (PUNCTUATION[code] !== undefined) return PUNCTUATION[code];
  if (KEYNAMES.has(e.key)) return e.key;
  return null;
}
addEventListener('keydown', e => {
  if (MODIFIER_KEYS.has(e.key)) return;   // flags are read from the next key
  const held = modifiers(e);
  if (held) {
    const name = chordKey(e);
    if (name) {
      e.preventDefault(); flush();
      send({t:'key', name, mods:held});
    }
    return;
  }
  if (e.key.length === 1) {
    text += e.key;
    clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 20);
    e.preventDefault();
  } else if (KEYNAMES.has(e.key)) {
    e.preventDefault(); flush(); send({t:'key', name:e.key});
  }
});
addEventListener('blur', flush);

let statsFailures = 0;
setInterval(async () => {
  try {
    const started = performance.now();
    const r = await fetch('/stats?t=' + token, {cache: 'no-store'});
    const s = await r.json();
    const ping = Math.round(performance.now() - started);
    statsFailures = 0;
    if (s.dw) displayWidth = s.dw;
    const size = canvas.width ? canvas.width + 'x' + canvas.height : 'no frame';
    const stalled = s.alive === false;
    const encode = s.ms ? '  encode ' + Math.round(s.ms) + ' ms' : '';
    const now = performance.now();
    const shown = shownAt.filter(t => now - t <= 2000).length / 2;
    log('desktop ' + size + '  show ' + shown.toFixed(1) + ' fps  capture ' + s.fps +
        ' fps  ping ' + ping + ' ms' + encode + (stalled ? '  STALLED (capture stopped)' : '') + hint);
  } catch (err) {
    statsFailures += 1;
    log('stats error: ' + err.message + hint);
    if (statsFailures >= 3) { statsFailures = 0; connect(); }
  }
}, 1000);
</script>"""


def allowed(value):
    """True when the presented token hashes to the dispatched hash."""
    digest = hashlib.sha256((value or "").encode()).hexdigest()
    return hmac.compare_digest(digest, TOKEN_HASH)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, kind="text/plain; charset=utf-8", status=200, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def stream_frames(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.settimeout(30)
        except OSError:
            pass
        sent = None
        while True:
            with COND:
                deadline = time.time() + 1.0
                while True:
                    stamp, data = frame_locked()
                    if data and stamp != sent:
                        break
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    COND.wait(left)
            if not data:
                return
            if stamp == sent:
                continue
            part = (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
            try:
                self.wfile.write(part)
            except OSError:
                return
            sent = stamp

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            return self.send("websocket upgrade required", status=400)
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(25)
        stop = threading.Event()
        lock = threading.Lock()
        try:
            if LAST_FULL[0]:
                with lock:
                    sock.sendall(ws_frame(LAST_FULL[0], 0x2))
        except OSError:
            return
        threading.Thread(target=self.ws_frames, args=(sock, stop, lock),
                         daemon=True).start()
        # A new viewer starts with no picture: ask the capture for a full frame.
        enqueue({"t": "full"})
        try:
            while not stop.is_set():
                try:
                    message = ws_message(sock)
                except TimeoutError:
                    try:
                        with lock:
                            sock.sendall(ws_frame(b"", 0x9))   # keepalive ping
                    except OSError:
                        break
                    continue
                if message is None:
                    break
                opcode, data = message
                if opcode != 0x1:
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                for item in ws_items(event):
                    enqueue(item)
        except (OSError, ConnectionError):
            pass
        finally:
            stop.set()

    def ws_frames(self, sock, stop, lock):
        """Send the newest frame; a slow link skips frames instead of queueing them."""
        sent = None
        while not stop.is_set():
            with COND:
                deadline = time.time() + 1.0
                while not stop.is_set():
                    stamp, data = frame_locked()
                    if data and stamp != sent:
                        break
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    COND.wait(left)
            if stop.is_set() or not data or stamp == sent:
                continue
            try:
                with lock:
                    sock.sendall(ws_frame(data, 0x2))
            except OSError:
                stop.set()
                return
            sent = stamp

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not allowed(query.get("t", [""])[0]):
            return self.send("forbidden", status=403)
        if url.path == "/":
            return self.send(PAGE.encode(), kind="text/html; charset=utf-8")
        if url.path == "/ws":
            try:
                return self.websocket()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        if url.path == "/stream.mjpg":
            try:
                return self.stream_frames()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        if url.path == "/stats":
            now = time.time()
            with COND:
                stamp, data = frame_locked()
                recent = [t for t in STATS["recent"] if now - t <= 2.0]
                try:
                    beat_age = now - os.stat(BEAT).st_mtime
                except OSError:
                    beat_age = None
                # A fresh heartbeat or a fresh frame means capture is alive; an idle
                # screen produces no frames, which is not a stall.
                alive = ((beat_age is not None and beat_age < 5)
                         or (bool(stamp) and now - stamp < 3))
                stat = streamer_stat()
                payload = {"stamp": round(stamp, 3), "frames": STATS["frames"],
                           "fps": round(len(recent) / 2.0, 1),
                           "width": stat.get("dw") or DIMS[0],
                           "height": stat.get("dh") or DIMS[1], "alive": alive,
                           "input_ok": os.path.exists(INPUT_OK),
                           "input_checked": os.path.exists(INPUT_CHECKED),
                           "age": round(now - stamp, 1) if stamp and data else None,
                           "ms": stat.get("ms"), "delivered": stat.get("delivered"),
                           "encoded": stat.get("encoded"), "skipped": stat.get("skipped"),
                           "dw": stat.get("dw"), "dh": stat.get("dh")}
            return self.send(json.dumps(payload), kind="application/json")
        if url.path in ("/frame.jpg", "/frame.png"):
            since = query.get("since", ["0"])[0]
            try:
                wait = min(5.0, max(0.0, float(query.get("wait", ["0"])[0] or 0)))
            except ValueError:
                wait = 0.0
            stamp, data = frame()
            if wait > 0 and data:
                deadline = time.time() + wait
                with COND:
                    while True:
                        stamp, data = frame_locked()
                        if not data or f"{stamp:.3f}" != since:
                            break
                        left = deadline - time.time()
                        if left <= 0:
                            break
                        COND.wait(left)
            if not data:
                return self.send("capture unavailable", status=503)
            if f"{stamp:.3f}" == since:
                return self.send(b"", status=204)
            return self.send(data, kind="image/jpeg",
                             extra={"X-Frame-Time": f"{stamp:.3f}"})
        if url.path == "/config":
            change = {}
            for key, cast in (("width", int), ("fps", float), ("quality", float)):
                value = query.get(key, [""])[0]
                if value:
                    try:
                        change[key] = cast(value)
                    except ValueError:
                        return self.send("bad value", status=400)
            if not change:
                return self.send("nothing to change", status=400)
            item = {"t": "config"}
            item.update(change)
            # Queued like every other command: one thread owns the agent socket.
            enqueue(item)
            available = INPUT_SOCKET[0] is not None
            return self.send("config " + ("queued" if available else "queued (agent not connected)"))
        if url.path in ("/click", "/rclick", "/down", "/move", "/up", "/scroll"):
            width, height = display_size()

            def at():
                fx = max(0.0, min(1.0, float(query.get("fx", ["0.5"])[0])))
                fy = max(0.0, min(1.0, float(query.get("fy", ["0.5"])[0])))
                return fx * width, fy * height

            x, y = at()
            if url.path == "/scroll":
                dy = max(-240.0, min(240.0, float(query.get("dy", ["0"])[0])))
                enqueue({"t": "scroll", "dy": dy, "x": x, "y": y})
                return self.send(f"scroll {int(dy)}")
            if url.path == "/move":
                enqueue({"t": "mouse", "type": "drag", "x": x, "y": y})
                return self.send(f"/move {int(x)},{int(y)}")
            if url.path == "/down":
                enqueue({"t": "mouse", "type": "down", "x": x, "y": y, "button": "left"})
                return self.send(f"/down {int(x)},{int(y)}")
            if url.path == "/up":
                enqueue({"t": "mouse", "type": "up", "x": x, "y": y, "button": "left"})
                return self.send(f"/up {int(x)},{int(y)}")
            button = "right" if url.path == "/rclick" else "left"
            try:
                count = max(1, min(3, int(query.get("count", ["1"])[0] or 1)))
            except ValueError:
                count = 1
            for state in range(1, count + 1):
                enqueue({"t": "mouse", "type": "down", "x": x, "y": y,
                         "button": button, "count": state})
                enqueue({"t": "mouse", "type": "up", "x": x, "y": y,
                         "button": button, "count": state})
            return self.send(f"{url.path} {int(x)},{int(y)}")
        if url.path == "/type":
            text = query.get("text", [""])[0]
            if text:
                enqueue({"t": "char", "s": text})
            return self.send(f"typed {len(text)}")
        if url.path == "/key":
            name = query.get("name", [""])[0]
            mods = [m for m in query.get("mods", [""])[0].split(",") if m in APPLE_MODIFIERS]
            if mods:
                code = CHAR_CODES.get(name, KEYCODES.get(name))
                if code is None:
                    return self.send("unknown key", status=400)
                enqueue({"t": "key", "code": code, "flags": mods})
                return self.send(f"key {name} with {','.join(mods)}")
            if len(name) == 1:
                enqueue({"t": "char", "s": name})
                return self.send(f"key {name!r}")
            code = KEYCODES.get(name)
            if code is None:
                return self.send("unknown key", status=400)
            enqueue({"t": "key", "code": code})
            return self.send(f"key {name}")
        return self.send("not found", status=404)

    def receive(self, path, length):
        """Stream the request body into path; False when it ends early."""
        left = length
        with open(path, "wb") as out:
            while left > 0:
                chunk = self.rfile.read(min(UPLOAD_CHUNK, left))
                if not chunk:
                    return False
                out.write(chunk)
                left -= len(chunk)
        return True

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not allowed(query.get("t", [""])[0]):
            return self.send("forbidden", status=403)
        if url.path != "/upload":
            return self.send("not found", status=404)
        name = query.get("name", [""])[0]
        if not UPLOAD_NAME.fullmatch(name) or name in (".", ".."):
            return self.send("bad upload name", status=400)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.send("Content-Length required", status=400)
        if not 1 <= length <= UPLOAD_MAX:
            return self.send("upload must be 1 byte to 4 GiB", status=400)
        home = home_dir()
        archive = tempfile.NamedTemporaryFile(prefix="ios-ci-upload-", suffix=".tar.gz",
                                              delete=False).name
        staging = ""
        try:
            if not self.receive(archive, length):
                return self.send("the upload ended early", status=400)
            problem = archive_problem(archive)
            if problem:
                return self.send(problem, status=400)
            # Staging lives beside the target so the move below stays on one volume.
            staging = tempfile.mkdtemp(prefix=".upload-", dir=home)
            extracted = subprocess.run(["tar", "-xzf", archive, "-C", staging],
                                       capture_output=True, text=True, timeout=600)
            if extracted.returncode:
                return self.send("could not extract the archive: "
                                 + extracted.stderr.strip()[:200], status=400)
            files, size = tree_size(staging)
            target = os.path.join(home, name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            elif os.path.exists(target) or os.path.islink(target):
                os.remove(target)
            os.replace(staging, target)
            staging = ""
            hand_over(target)
            return self.send(json.dumps({"path": target, "files": files, "bytes": size}),
                             kind="application/json")
        except (OSError, subprocess.SubprocessError) as error:
            return self.send(f"upload failed: {error}", status=500)
        finally:
            try:
                os.unlink(archive)
            except OSError:
                pass
            if staging:
                shutil.rmtree(staging, ignore_errors=True)


def main():
    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=input_worker, daemon=True).start()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    http.server.ThreadingHTTPServer.daemon_threads = True
    server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"serving on {BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
