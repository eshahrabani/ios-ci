#!/usr/bin/env python3
"""Serve the console desktop as PNG frames and inject input.

Screen Sharing on macOS 26 refuses to capture (its ARDAgent dies with
"Unable to capture screen"), but `screencapture` and `System Events` both work, so
this serves the former and drives the latter. Run it in the console session:

    launchctl asuser <uid> python3 desktop_server.py <port> <token>
"""
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

PORT = int(sys.argv[1])
TOKEN = sys.argv[2]
FRAME = "/tmp/desktop-frame.png"
LOCK = threading.Lock()
LAST = [0.0, b""]
KEYCODES = {
    "Return": 36, "Enter": 36, "Tab": 48, "Backspace": 51, "Delete": 117,
    "Escape": 53, " ": 49, "ArrowLeft": 123, "ArrowRight": 124,
    "ArrowDown": 125, "ArrowUp": 126, "Home": 115, "End": 119,
}

PAGE = b"""<!doctype html><meta charset=utf-8>
<title>ios-ci desktop</title>
<style>
 html,body{margin:0;height:100%;background:#111;overflow:hidden}
 img{width:100vw;height:100vh;object-fit:contain;display:block;cursor:crosshair}
 #hud{position:fixed;left:8px;bottom:8px;color:#888;font:12px monospace}
</style>
<img id=i alt="desktop"><div id=hud></div>
<script>
const token = new URLSearchParams(location.search).get('t');
const img = document.getElementById('i'), hud = document.getElementById('hud');
let box = null;
async function frame() {
  try {
    const r = await fetch('/frame.png?t=' + token + '&_=' + Date.now());
    if (!r.ok) throw new Error(r.status);
    img.src = URL.createObjectURL(await r.blob());
    img.onload = () => { box = {w: img.naturalWidth, h: img.naturalHeight}; hud.textContent = box.w + 'x' + box.h; };
  } catch (e) { hud.textContent = 'frame error ' + e.message; }
  setTimeout(frame, 900);
}
frame();
img.addEventListener('click', async e => {
  const r = img.getBoundingClientRect();
  const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
  hud.textContent = 'click ' + fx.toFixed(3) + ',' + fy.toFixed(3);
  await fetch('/click?fx=' + fx + '&fy=' + fy + '&t=' + token);
});
let typed = '';
async function flush() { if (!typed) return; const t = typed; typed = '';
  await fetch('/type?text=' + encodeURIComponent(t) + '&t=' + token); }
document.addEventListener('keydown', async e => {
  if (e.key.length === 1 && !e.metaKey && !e.ctrlKey) { typed += e.key; setTimeout(flush, 250); }
  else { await flush(); await fetch('/key?name=' + encodeURIComponent(e.key) + '&t=' + token); }
  e.preventDefault();
});
</script>"""


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(cmd, 1, "", str(error))


def frame():
    with LOCK:
        now = time.time()
        if now - LAST[0] < 0.5 and LAST[1]:
            return LAST[1]
        run(["screencapture", "-x", "-t", "png", FRAME])
        try:
            LAST[1] = open(FRAME, "rb").read()
        except OSError:
            LAST[1] = LAST[1] or b""
        LAST[0] = now
        return LAST[1]


def screen_size():
    data = frame()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        return 1024, 768
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def osa(script):
    return run(["osascript", "-e", script])


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def authorised(self, query):
        return query.get("t", [""])[0] == TOKEN

    def send(self, body, kind="text/html; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self.authorised(query):
            return self.send(b"forbidden", status=403)
        if url.path == "/":
            return self.send(PAGE, kind="text/html; charset=utf-8")
        if url.path == "/frame.png":
            data = frame()
            if not data:
                return self.send(b"capture failed", status=503)
            return self.send(data, kind="image/png")
        if url.path == "/click":
            width, height = screen_size()
            fx = float(query.get("fx", ["0.5"])[0])
            fy = float(query.get("fy", ["0.5"])[0])
            x, y = int(fx * width), int(fy * height)
            osa(f'tell application "System Events" to click at {{{x}, {y}}}')
            return self.send(f"clicked {x},{y}".encode(), kind="text/plain")
        if url.path == "/type":
            text = query.get("text", [""])[0]
            if text:
                escaped = text.replace("\\", "\\\\").replace('"', '\\"')
                osa(f'tell application "System Events" to keystroke "{escaped}"')
            return self.send(b"ok", kind="text/plain")
        if url.path == "/key":
            name = query.get("name", [""])[0]
            code = KEYCODES.get(name)
            if code is None:
                return self.send(b"unknown key", status=400)
            osa(f'tell application "System Events" to key code {code}')
            return self.send(b"ok", kind="text/plain")
        if url.path == "/size":
            width, height = screen_size()
            return self.send(f"{width}x{height}".encode(), kind="text/plain")
        return self.send(b"not found", status=404)


def main():
    address = sys.argv[3] if len(sys.argv) > 3 else "0.0.0.0"
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer((address, PORT), Handler)
    print(f"serving on {address}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
