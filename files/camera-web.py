#!/usr/bin/env python3
import atexit
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAMERA_NAME = "/base/soc@0/cci@ac4a000/i2c-bus@0/camera@1a"
STREAM_TIMEOUT = 10 * 60  # seconds before auto-stop

FLASH_LED = "/sys/class/leds/white:flash/brightness"
FLASH_MAX = 255
_saved_brightness = 25  # default; updated whenever flash is set to a non-zero value

GST_CMD = [
    "gst-launch-1.0", "-q",
    "libcamerasrc", f"camera-name={CAMERA_NAME}",
    "!", "video/x-raw,width=1280,height=720,framerate=15/1",
    "!", "videoflip", "method=rotate-180",
    "!", "videoconvert",
    "!", "jpegenc", "quality=85",
    "!", "multipartmux", "boundary=frame",
    "!", "fdsink", "fd=1",
]


def flash_set(brightness):
    global _saved_brightness
    brightness = max(0, min(FLASH_MAX, int(brightness)))
    if brightness > 0:
        _saved_brightness = brightness
    try:
        with open(FLASH_LED, "w") as f:
            f.write(str(brightness))
        return brightness
    except OSError:
        return None


def flash_get():
    try:
        with open(FLASH_LED) as f:
            return int(f.read().strip())
    except OSError:
        return 0


atexit.register(lambda: flash_set(0))

HTML = b"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>3D Printer Camera</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #1a1a1a; color: #eee; font-family: sans-serif;
           display: flex; flex-direction: column; align-items: center;
           min-height: 100vh; padding: 1rem; gap: 0.75rem; }
    h1 { margin-top: 1rem; font-size: 1.2rem; color: #aaa; }
    #frame { width: 100%; max-width: 960px; aspect-ratio: 16/9;
             background: #000; border-radius: 8px; display: flex;
             align-items: center; justify-content: center; overflow: hidden; }
    #stream { width: 100%; display: none; }
    #placeholder { color: #444; font-size: 1rem; }
    .controls { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap;
                justify-content: center; }
    button { padding: 0.6rem 1.4rem; border: none; border-radius: 6px;
             font-size: 1rem; cursor: pointer; }
    #start-btn { background: #2d8a4e; color: #fff; }
    #start-btn:hover { background: #36a85f; }
    #start-btn:disabled { background: #444; cursor: default; }
    #stop-btn { background: #8a2d2d; color: #fff; }
    #stop-btn:hover { background: #a83636; }
    #stop-btn:disabled { background: #444; cursor: default; }
    .flash-row { display: flex; align-items: center; gap: 0.6rem;
                 font-size: 0.9rem; color: #aaa; }
    #flash-btn { padding: 0.3rem 0.8rem; border: none; border-radius: 4px;
                 font-size: 0.85rem; cursor: pointer;
                 background: #444; color: #aaa; }
    #flash-btn.on { background: #c8a830; color: #111; }
    #flash-slider { width: 120px; accent-color: #c8a830; cursor: pointer; }
    #flash-label { min-width: 2rem; text-align: right; color: #eee; }
    #status { font-size: 0.85rem; color: #888; min-height: 1.2em; }
  </style>
</head>
<body>
  <h1>3D Printer Camera</h1>
  <div id="frame">
    <img id="stream" alt="camera stream">
    <span id="placeholder">Press Start to view the camera</span>
  </div>
  <div class="controls">
    <button id="start-btn" onclick="startStream()">&#9654; Start</button>
    <button id="stop-btn"  onclick="stopStream()" disabled>&#9632; Stop</button>
    <div class="flash-row">
      &#9888; Flash:
      <button id="flash-btn" onclick="onFlashToggle()">Off</button>
      <input type="range" id="flash-slider" min="1" max="255" value="25"
             oninput="onFlashSlider(this.value)">
      <span id="flash-label">25</span>
    </div>
  </div>
  <p id="status"></p>
  <script>
    const TIMEOUT_S = 10 * 60;
    let countdownId, deadlineId, flashTimer;

    window.addEventListener('load', async () => {
      try {
        const data = await fetch('/flash/state').then(r => r.json());
        document.getElementById('flash-slider').value = data.brightness;
        document.getElementById('flash-label').textContent = data.brightness;
      } catch (e) {}
    });

    function startStream() {
      const img = document.getElementById('stream');
      const ph  = document.getElementById('placeholder');
      document.getElementById('start-btn').disabled = true;
      document.getElementById('stop-btn').disabled  = false;
      setStatus('Connecting\u2026 (first frame ~10 s)');

      img.onload  = () => { ph.style.display = 'none'; img.style.display = 'block'; };
      img.onerror = () => { setStatus('Stream error \u2014 press Start to retry'); resetUI(); };
      img.src = '/stream?' + Date.now();

      let remaining = TIMEOUT_S;
      countdownId = setInterval(() => {
        remaining--;
        setStatus('Live \u2014 auto-stop in ' + fmt(remaining));
        if (remaining <= 0) stopStream();
      }, 1000);
      deadlineId = setTimeout(stopStream, TIMEOUT_S * 1000);
    }

    function stopStream() {
      const img = document.getElementById('stream');
      img.src = '';
      img.style.display = 'none';
      document.getElementById('placeholder').style.display = '';
      clearInterval(countdownId);
      clearTimeout(deadlineId);
      flashOff();
      resetUI();
    }

    function resetUI() {
      document.getElementById('start-btn').disabled = false;
      document.getElementById('stop-btn').disabled  = true;
      if (!document.getElementById('status').textContent.includes('error'))
        setStatus('');
    }

    function onFlashToggle() {
      const btn = document.getElementById('flash-btn');
      const isOn = btn.classList.toggle('on');
      btn.textContent = isOn ? 'On' : 'Off';
      const val = document.getElementById('flash-slider').value;
      fetch('/flash/' + (isOn ? val : 0));
    }

    function flashOff() {
      const btn = document.getElementById('flash-btn');
      if (btn.classList.contains('on')) {
        btn.classList.remove('on');
        btn.textContent = 'Off';
        fetch('/flash/0');
      }
    }

    function onFlashSlider(val) {
      val = parseInt(val);
      document.getElementById('flash-label').textContent = val;
      if (!document.getElementById('flash-btn').classList.contains('on')) return;
      clearTimeout(flashTimer);
      flashTimer = setTimeout(() => fetch('/flash/' + val), 120);
    }

    function setStatus(msg) { document.getElementById('status').textContent = msg; }
    function fmt(s) { return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); }
  </script>
</body>
</html>
"""

_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/flash/state":
            self._json({"brightness": _saved_brightness})
            return

        if self.path.startswith("/flash/"):
            try:
                brightness = int(self.path[len("/flash/"):])
                result = flash_set(brightness)
                self._json({"brightness": result if result is not None else flash_get()})
            except ValueError:
                self.send_error(400, "brightness must be an integer 0-255")
            return

        if not self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(HTML)))
            self.end_headers()
            self.wfile.write(HTML)
            return

        if not _lock.acquire(blocking=False):
            self.send_error(503, "Camera already in use")
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=--frame")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()

        proc = subprocess.Popen(GST_CMD, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + STREAM_TIMEOUT
        try:
            while time.monotonic() < deadline:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            _lock.release()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("Camera web server on http://0.0.0.0:8080")
    server.serve_forever()
