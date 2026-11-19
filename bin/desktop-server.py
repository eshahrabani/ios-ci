#!/usr/bin/env python3
"""Serve the console desktop as live frames and inject input.

Run in the console session (so clicks and keys reach the GUI):

    launchctl asuser <uid> python3 desktop-server.py <port> <token> <host>

Frames are written by a separate capture process (bin/screen-stream.swift) to FRAME;
this process only serves the newest one. Capturing from here would put this interpreter
in the TCC responsibility chain, and macOS raises a modal consent dialog there that
ignores synthetic input.

The page holds one multipart/x-mixed-replace connection and the browser renders frames
as they arrive: no polling latency, and a slow client simply sees fewer frames because
the writer blocks (the next write always sends the newest frame).
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

PORT = int(sys.argv[1])
TOKEN = sys.argv[2]
BIND = sys.argv[3] if len(sys.argv) > 3 else "0.0.0.0"
FRAME = "/tmp/desktop-frame.jpg"
BEAT = FRAME + ".beat"                  # touched every second by the capture process

LOCK = threading.Lock()
COND = threading.Condition(LOCK)
CACHE = [0.0, b""]                      # mtime, bytes of the newest frame
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


def osa(script):
    return run(["osascript", "-e", script])


def dimensions(data):
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
            now = time.time()
            STATS["frames"] += 1
            STATS["recent"] = [t for t in STATS["recent"] if now - t <= 2.0] + [now]
            DIMS[:] = list(dimensions(data))
            COND.notify_all()


PAGE = """<!doctype html><meta charset="utf-8">
<title>ios-ci desktop</title>
<style>
 html,body{margin:0;height:100%;background:#000;overflow:hidden;
   font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#9aa0a6}
 #screen{position:fixed;inset:0;width:100vw;height:100vh;object-fit:contain;
   background:#000;cursor:crosshair;touch-action:none;user-select:none;
   -webkit-user-select:none;-webkit-user-drag:none}
 #hud{position:fixed;left:10px;bottom:10px;background:#000b;padding:6px 9px;
   border-radius:6px;white-space:pre;pointer-events:none}
</style>
<img id=screen alt="" draggable=false><div id=hud>connecting…</div>
<script>
const token = new URLSearchParams(location.search).get('t');
const img = document.getElementById('screen');
const hud = document.getElementById('hud');
const KEYNAMES = new Set(['Enter','Tab','Backspace','Delete','Escape','ArrowUp',
  'ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown',
  'F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12']);
const MODIFIER_KEYS = new Set(['Shift','Control','Alt','Meta','CapsLock','OS','AltGraph']);
const PUNCTUATION = {Minus:'-',Equal:'=',BracketLeft:'[',BracketRight:']',Backslash:'\\\\',
  Semicolon:';',Quote:"'",Comma:',',Period:'.',Slash:'/',Backquote:'`',Space:' '};
let hint = '';

function log(message) { hud.textContent = message; }
function connect() {
  img.src = '/stream.mjpg?t=' + token + '&n=' + Math.random().toString(36).slice(2);
}
img.addEventListener('error', () => { log('stream lost; reconnecting'); setTimeout(connect, 1500); });
connect();

function contentBox() {
  const nw = img.naturalWidth, nh = img.naturalHeight;
  if (!nw || !nh) return null;
  const ew = img.clientWidth || innerWidth, eh = img.clientHeight || innerHeight;
  const scale = Math.min(ew / nw, eh / nh);
  return [(ew - nw * scale) / 2, (eh - nh * scale) / 2, nw * scale, nh * scale];
}
function imageXY(e) {
  const box = contentBox();
  if (!box) return null;
  return [(e.clientX - box[0]) / box[2], (e.clientY - box[1]) / box[3]];
}
async function send(path, params) {
  try {
    const query = new URLSearchParams(params);
    query.set('t', token);
    const r = await fetch(path + '?' + query);
    hint = '  ' + (await r.text());
  } catch (err) { hint = '  ' + path + ' failed'; }
}

let down = false, start = [0, 0], dragging = false, pending = null, timer = null;
img.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;          // right button belongs to contextmenu alone
  const point = imageXY(e);
  if (!point) return;
  img.setPointerCapture(e.pointerId);
  start = point; down = true; dragging = false;
  e.preventDefault();
});
img.addEventListener('pointermove', e => {
  if (!down) return;
  const point = imageXY(e);
  if (!point) return;
  const [fx, fy] = point;
  if (!dragging && Math.abs(fx - start[0]) * img.naturalWidth < 3 &&
      Math.abs(fy - start[1]) * img.naturalHeight < 3) return;
  if (!dragging) { dragging = true; send('/down', {fx: start[0], fy: start[1]}); }
  pending = [fx, fy];
  if (!timer) timer = setTimeout(() => {
    timer = null;
    if (pending) { const p = pending; pending = null; send('/move', {fx: p[0], fy: p[1]}); }
  }, 40);
});
img.addEventListener('pointerup', e => {
  if (e.button !== 0 || !down) return;
  down = false;
  const point = imageXY(e);
  if (!point) return;
  if (dragging) send('/up', {fx: point[0], fy: point[1]});
  else send('/click', {fx: point[0], fy: point[1]});
  dragging = false;
});
img.addEventListener('pointercancel', () => { down = false; dragging = false; });
img.addEventListener('dragstart', e => e.preventDefault());
img.addEventListener('contextmenu', e => {
  e.preventDefault();
  const point = imageXY(e);
  if (point) send('/rclick', {fx: point[0], fy: point[1]});
});
img.addEventListener('wheel', e => {
  e.preventDefault();
  send('/scroll', {dy: Math.sign(e.deltaY) * Math.min(10, Math.abs(e.deltaY))});
}, {passive: false});

let text = '', flushTimer = null;
function flush() {
  if (!text) return;
  const t = text; text = '';
  send('/type', {text: t});
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
      send('/key', {name, mods: held});
    }
    return;
  }
  if (e.key.length === 1) {
    text += e.key;
    clearTimeout(flushTimer);
    flushTimer = setTimeout(flush, 160);
    e.preventDefault();
  } else if (KEYNAMES.has(e.key)) {
    e.preventDefault(); flush(); send('/key', {name: e.key});
  }
});
addEventListener('blur', flush);

let statsFailures = 0;
setInterval(async () => {
  try {
    const r = await fetch('/stats?t=' + token, {cache: 'no-store'});
    const s = await r.json();
    statsFailures = 0;
    const size = s.width ? s.width + 'x' + s.height : 'no frame';
    const stalled = s.alive === false;
    log('desktop ' + size + '  capture ' + s.fps + ' fps' +
        (stalled ? '  STALLED (capture stopped)' : '') + hint);
  } catch (err) {
    statsFailures += 1;
    log('stats error: ' + err.message + hint);
    if (statsFailures >= 3) { statsFailures = 0; connect(); }
  }
}, 1000);
</script>"""


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

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if query.get("t", [""])[0] != TOKEN:
            return self.send("forbidden", status=403)
        if url.path == "/":
            return self.send(PAGE.encode(), kind="text/html; charset=utf-8")
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
                payload = {"stamp": round(stamp, 3), "frames": STATS["frames"],
                           "fps": round(len(recent) / 2.0, 1),
                           "width": DIMS[0], "height": DIMS[1], "alive": alive,
                           "age": round(now - stamp, 1) if stamp and data else None}
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
        if url.path in ("/click", "/rclick", "/down", "/move", "/up", "/scroll"):
            width, height = dimensions(frame()[1])

            def at():
                fx = max(0.0, min(1.0, float(query.get("fx", ["0.5"])[0])))
                fy = max(0.0, min(1.0, float(query.get("fy", ["0.5"])[0])))
                return int(fx * width), int(fy * height)

            if url.path == "/scroll":
                dy = int(float(query.get("dy", ["0"])[0]))
                code = "121" if dy > 0 else "116"
                for _ in range(min(5, max(1, abs(dy) // 3 or 1))):
                    osa(f'tell application "System Events" to key code {code}')
                return self.send(f"scroll {dy}")
            x, y = at()
            if not CLICLICK:
                return self.send("cliclick missing", status=503)
            if url.path == "/click":
                run([CLICLICK, f"c:{x},{y}"])
            elif url.path == "/rclick":
                run([CLICLICK, f"rc:{x},{y}"])
            elif url.path == "/down":
                run([CLICLICK, f"dd:{x},{y}"])
            elif url.path == "/move":
                run([CLICLICK, f"dm:{x},{y}"])
            else:
                run([CLICLICK, f"du:{x},{y}"])
            return self.send(f"{url.path} {x},{y}")
        if url.path == "/type":
            text = query.get("text", [""])[0]
            if text:
                escaped = text.replace("\\", "\\\\").replace('"', '\\"')
                osa(f'tell application "System Events" to keystroke "{escaped}"')
            return self.send(f"typed {len(text)}")
        if url.path == "/key":
            name = query.get("name", [""])[0]
            mods = [m for m in query.get("mods", [""])[0].split(",") if m in APPLE_MODIFIERS]
            using = ""
            if mods:
                using = " using {" + ", ".join(APPLE_MODIFIERS[m] for m in mods) + "}"
            if len(name) == 1:
                escaped = name.replace("\\", "\\\\").replace('"', '\\"')
                osa(f'tell application "System Events" to keystroke "{escaped}"{using}')
                return self.send(f"key {name!r}{using}")
            code = KEYCODES.get(name)
            if code is None:
                return self.send("unknown key", status=400)
            osa(f'tell application "System Events" to key code {code}{using}')
            return self.send(f"key {name}{using}")
        return self.send("not found", status=404)


def main():
    threading.Thread(target=watch, daemon=True).start()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    http.server.ThreadingHTTPServer.daemon_threads = True
    server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"serving on {BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
