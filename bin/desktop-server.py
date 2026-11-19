#!/usr/bin/env python3
"""Serve the console desktop as frames and inject input.

Run in the console session (so clicks and keys reach the GUI):

    launchctl asuser <uid> python3 desktop-server.py <port> <token> <host>

Frames are written by a separate capture loop to FRAME; this process only serves the
newest one. Capturing from here would put this interpreter in the TCC responsibility
chain, and macOS raises a modal consent dialog there that ignores synthetic input.
"""
import http.server
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
FRAME = "/tmp/desktop-frame.png"
LOCK = threading.Lock()
CACHE = [0.0, b""]
KEYCODES = {
    "Enter": 36, "Return": 36, "Tab": 48, "Backspace": 51, "Delete": 117,
    "Escape": 53, " ": 49, "ArrowLeft": 123, "ArrowRight": 124, "ArrowDown": 125,
    "ArrowUp": 126, "Home": 115, "End": 119, "PageUp": 116, "PageDown": 121,
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


def osa(script):
    return run(["osascript", "-e", script])


def frame():
    """Return (mtime, bytes) of the newest frame written by the capture loop."""
    with LOCK:
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


def dimensions(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return 1024, 768
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


PAGE = """<!doctype html><meta charset="utf-8">
<title>ios-ci desktop</title>
<style>
 html,body{margin:0;height:100%;background:#000;overflow:hidden;
   font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#9aa0a6}
 canvas{display:block;width:100vw;height:100vh;touch-action:none;cursor:crosshair}
 #hud{position:fixed;left:10px;bottom:10px;background:#000b;padding:6px 9px;
   border-radius:6px;white-space:pre;pointer-events:none}
</style>
<canvas id=c></canvas><div id=hud>connecting…</div>
<script>
const token = new URLSearchParams(location.search).get('t');
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
const hud = document.getElementById('hud');
let size = [0, 0], box = [0, 0, 1, 1], since = 0, frames = 0, fps = 0, last = null;

const log = m => { hud.textContent = m; };

function fit() {
  const dpr = devicePixelRatio || 1;
  cv.width = Math.round(innerWidth * dpr);
  cv.height = Math.round(innerHeight * dpr);
  const s = Math.min(cv.width / (size[0] || 1), cv.height / (size[1] || 1));
  const w = (size[0] || 1) * s, h = (size[1] || 1) * s;
  box = [(cv.width - w) / 2, (cv.height - h) / 2, w, h];
  draw();
}
function draw() {
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (last) ctx.drawImage(last, box[0], box[1], box[2], box[3]);
}
async function poll() {
  try {
    const r = await fetch('/frame.png?t=' + token + '&since=' + since);
    if (r.status === 200) {
      since = Number(r.headers.get('X-Frame-Time') || 0);
      const bmp = await createImageBitmap(await r.blob());
      if (bmp.width !== size[0] || bmp.height !== size[1]) { size = [bmp.width, bmp.height]; fit(); }
      last = bmp; draw(); frames++;
    } else if (r.status === 503) { log('capture unavailable'); }
  } catch (e) { log('frame error: ' + e.message); }
  setTimeout(poll, 110);
}
setInterval(() => {
  fps = frames; frames = 0;
  if (size[0]) log('desktop ' + size[0] + 'x' + size[1] + '  ' + fps + ' fps' + hint);
}, 1000);
let hint = '';
addEventListener('resize', fit);

function imageXY(e) {
  const dpr = devicePixelRatio || 1;
  return [(e.clientX * dpr - box[0]) / box[2], (e.clientY * dpr - box[1]) / box[3]];
}
async function send(path, params) {
  try {
    const q = new URLSearchParams(params); q.set('t', token);
    const r = await fetch(path + '?' + q);
    hint = '  ' + path + ' ' + (await r.text());
  } catch (err) { hint = '  ' + path + ' failed'; }
}
let down = false, start = [0, 0], dragging = false, pending = null, timer = null;
cv.addEventListener('pointerdown', e => {
  cv.setPointerCapture(e.pointerId);
  start = imageXY(e); down = true; dragging = false;
});
cv.addEventListener('pointermove', e => {
  if (!down) return;
  const [fx, fy] = imageXY(e);
  if (Math.abs(fx - start[0]) * (size[0] || 1) < 3 &&
      Math.abs(fy - start[1]) * (size[1] || 1) < 3) return;
  if (!dragging) { dragging = true; send('/down', {fx: start[0], fy: start[1]}); }
  pending = [fx, fy];
  if (!timer) timer = setTimeout(() => {
    timer = null;
    if (pending) { const p = pending; pending = null; send('/move', {fx: p[0], fy: p[1]}); }
  }, 40);
});
cv.addEventListener('pointerup', e => {
  if (!down) return;
  down = false;
  const [fx, fy] = imageXY(e);
  if (dragging) send('/up', {fx, fy});
  else send('/click', {fx, fy});
  dragging = false;
});
cv.addEventListener('contextmenu', e => {
  e.preventDefault();
  const [fx, fy] = imageXY(e);
  send('/rclick', {fx, fy});
});
cv.addEventListener('wheel', e => {
  e.preventDefault();
  send('/scroll', {dy: Math.sign(e.deltaY) * Math.min(10, Math.abs(e.deltaY))});
}, {passive: false});

let text = '', flushTimer = null;
function flush() {
  if (!text) return;
  const t = text; text = '';
  send('/type', {text: t});
}
addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key.length === 1) { text += e.key; clearTimeout(flushTimer); flushTimer = setTimeout(flush, 160); }
  else if (KEYCODES[e.key] !== undefined) { e.preventDefault(); flush(); send('/key', {name: e.key}); }
  else if (e.key === 'Enter' || e.key === 'Tab' || e.key === 'Backspace' || e.key === 'Escape') {
    e.preventDefault(); flush(); send('/key', {name: e.key});
  }
  if (e.key.length === 1) e.preventDefault();
});
const KEYCODES = {Enter: 1, Tab: 1, Backspace: 1, Escape: 1, ArrowUp: 1, ArrowDown: 1,
  ArrowLeft: 1, ArrowRight: 1, Home: 1, End: 1, PageUp: 1, PageDown: 1};
fit(); poll(); cv.focus();
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

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if query.get("t", [""])[0] != TOKEN:
            return self.send("forbidden", status=403)
        if url.path == "/":
            return self.send(PAGE.encode(), kind="text/html; charset=utf-8")
        if url.path == "/frame.png":
            stamp, data = frame()
            if not data:
                return self.send("capture unavailable", status=503)
            if query.get("since", ["0"])[0] == f"{stamp:.3f}":
                return self.send(b"", status=204)
            return self.send(data, kind="image/png",
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
            code = KEYCODES.get(name)
            if code is None:
                return self.send("unknown key", status=400)
            osa(f'tell application "System Events" to key code {code}')
            return self.send(f"key {name}")
        return self.send("not found", status=404)


def main():
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"serving on {BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
