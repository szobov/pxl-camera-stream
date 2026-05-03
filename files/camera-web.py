#!/usr/bin/env python3
import atexit, json, os, subprocess, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Full, Queue

CAMERA_NAME = "/base/soc@0/cci@ac4a000/i2c-bus@0/camera@1a"
RECORDINGS_DIR = "/var/local/camera-recordings"
FLASH_LED = "/sys/class/leds/white:flash/brightness"
FLASH_MAX = 255
_saved_brightness = 25


# ---------------------------------------------------------------------------
# Flash helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# GStreamer pipeline commands
# ---------------------------------------------------------------------------

def _gst_stream_cmd():
    """Simple MJPEG-to-stdout pipeline for live view only."""
    return [
        "gst-launch-1.0", "-q",
        "libcamerasrc", f"camera-name={CAMERA_NAME}",
        "!", "video/x-raw,width=1280,height=720,framerate=15/1",
        "!", "videoflip", "method=rotate-180",
        "!", "videoconvert",
        "!", "jpegenc", "quality=85",
        "!", "multipartmux", "boundary=frame",
        "!", "fdsink", "fd=1",
    ]


def _gst_record_cmd(filepath):
    """
    Tee pipeline:
      branch 1 → 15 fps MJPEG multipart to stdout  (live view)
      branch 2 → 1 fps MJPEG into an AVI file       (timelapse recording)
    The broadcaster thread always drains stdout so the pipeline never blocks
    even when no HTTP client is connected.
    """
    return [
        "gst-launch-1.0", "-q",
        "libcamerasrc", f"camera-name={CAMERA_NAME}",
        "!", "video/x-raw,width=1280,height=720,framerate=15/1",
        "!", "videoflip", "method=rotate-180",
        "!", "videoconvert",
        "!", "tee", "name=t",
        # -- live branch --
        "t.", "!", "queue",
              "!", "jpegenc", "quality=85",
              "!", "multipartmux", "boundary=frame",
              "!", "fdsink", "fd=1",
        # -- recording branch (1 fps timelapse) --
        "t.", "!", "queue",
              "!", "videorate",
              "!", "video/x-raw,framerate=1/1",
              "!", "jpegenc", "quality=85",
              "!", "avimux",
              "!", "filesink", f"location={filepath}",
    ]


# ---------------------------------------------------------------------------
# Pipeline manager
# ---------------------------------------------------------------------------

class PipelineManager:
    """
    Owns the GStreamer subprocess and fans out its stdout to all subscribed
    HTTP clients via per-client queues.  The broadcast thread always drains
    stdout, so the recording branch never stalls when nobody is watching live.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._clients: set = set()
        self._clients_lock = threading.Lock()
        self._timer = None
        self.mode = None            # "stream" | "record" | None
        self.recording_file = None  # path of in-progress recording
        self.recording_done = None  # path of last completed recording
        self.start_time = None
        self.end_time = None

    # -- public API ----------------------------------------------------------

    def start_stream(self):
        """Start a stream-only pipeline; no-op if already running."""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return
            self._launch(_gst_stream_cmd(), "stream", None)

    def start_record(self, duration_secs):
        """Stop whatever is running and start a recording tee pipeline."""
        with self._lock:
            self._stop_locked()
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fp = os.path.join(RECORDINGS_DIR, f"rec_{ts}.avi")
            self.recording_file = fp
            self.recording_done = None
            self._launch(_gst_record_cmd(fp), "record", duration_secs)

    def stop(self):
        with self._lock:
            self._stop_locked()

    def subscribe(self):
        """Return a Queue that receives MJPEG broadcast chunks."""
        q = Queue(maxsize=200)
        with self._clients_lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._clients_lock:
            self._clients.discard(q)
            no_clients = not self._clients
        # Auto-stop only in stream-only mode; recording outlives viewers.
        if no_clients and self.mode == "stream":
            threading.Thread(target=self.stop, daemon=True).start()

    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def get_status(self):
        now = time.monotonic()
        elapsed = int(now - self.start_time) if self.start_time else 0
        remaining = max(0, int(self.end_time - now)) if self.end_time else None
        return {
            "mode": self.mode,
            "running": self.is_running(),
            "elapsed": elapsed,
            "remaining": remaining,
            "recording_done": (
                os.path.basename(self.recording_done)
                if self.recording_done else None
            ),
            "recordings": self._list_recordings(),
        }

    # -- internals -----------------------------------------------------------

    def _list_recordings(self):
        try:
            result = []
            for name in sorted(os.listdir(RECORDINGS_DIR)):
                if not name.endswith(".avi"):
                    continue
                try:
                    size = os.path.getsize(os.path.join(RECORDINGS_DIR, name))
                    result.append({"name": name, "size": size})
                except OSError:
                    pass
            return result
        except FileNotFoundError:
            return []

    def _launch(self, cmd, mode, duration_secs):
        self.mode = mode
        self.start_time = time.monotonic()
        self.end_time = (self.start_time + duration_secs) if duration_secs else None
        # Fresh client set for this pipeline instance.  The broadcast loop
        # captures this reference so its sentinel can never reach clients
        # that subscribed to a later pipeline.
        with self._clients_lock:
            self._clients = set()
        my_clients = self._clients
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        threading.Thread(
            target=self._broadcast_loop, args=(self._proc, my_clients), daemon=True
        ).start()
        if duration_secs:
            self._timer = threading.Timer(duration_secs, self._on_timer)
            self._timer.daemon = True
            self._timer.start()

    def _on_timer(self):
        with self._lock:
            self._stop_locked()

    def _stop_locked(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._proc:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            if self.mode == "record" and self.recording_file:
                if os.path.exists(self.recording_file):
                    self.recording_done = self.recording_file
                self.recording_file = None
            self._proc = None
        self.mode = None
        self.start_time = None
        self.end_time = None

    def _broadcast_loop(self, proc, clients):
        """Read proc stdout and push to this pipeline's subscriber queues."""
        while proc.poll() is None:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            with self._clients_lock:
                dead = []
                for q in clients:
                    try:
                        q.put_nowait(chunk)
                    except Full:
                        dead.append(q)
                for q in dead:
                    clients.discard(q)
        # Sentinel: only tell THIS pipeline's subscribers the stream is over.
        with self._clients_lock:
            for q in clients:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass


_pipeline = PipelineManager()
atexit.register(_pipeline.stop)
atexit.register(lambda: flash_set(0))


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

HTML = """\
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
    .controls { display: flex; gap: 0.5rem; align-items: center;
                flex-wrap: wrap; justify-content: center; }
    button { padding: 0.6rem 1.4rem; border: none; border-radius: 6px;
             font-size: 1rem; cursor: pointer; }
    #start-btn { background: #2d8a4e; color: #fff; }
    #start-btn:hover:not(:disabled) { background: #36a85f; }
    #start-btn:disabled { background: #444; cursor: default; }
    #stop-btn  { background: #8a2d2d; color: #fff; }
    #stop-btn:hover:not(:disabled)  { background: #a83636; }
    #stop-btn:disabled  { background: #444; cursor: default; }
    .flash-row { display: flex; align-items: center; gap: 0.6rem;
                 font-size: 0.9rem; color: #aaa; }
    #flash-btn { padding: 0.3rem 0.8rem; border: none; border-radius: 4px;
                 font-size: 0.85rem; cursor: pointer;
                 background: #444; color: #aaa; }
    #flash-btn.on { background: #c8a830; color: #111; }
    #flash-slider { width: 120px; accent-color: #c8a830; cursor: pointer; }
    #flash-label  { min-width: 2rem; text-align: right; color: #eee; }
    .divider { width: 100%; max-width: 960px; border: none;
               border-top: 1px solid #2a2a2a; margin: 0.1rem 0; }
    .section { width: 100%; max-width: 960px;
               display: flex; flex-direction: column; gap: 0.5rem; }
    .section-title { font-size: 0.75rem; color: #555;
                     text-transform: uppercase; letter-spacing: 0.07em; }
    .rec-row { display: flex; gap: 0.6rem; align-items: center;
               flex-wrap: wrap; font-size: 0.9rem; color: #aaa; }
    #dur-input { width: 4.5rem; padding: 0.3rem 0.5rem;
                 background: #2a2a2a; border: 1px solid #444;
                 border-radius: 4px; color: #eee; font-size: 0.9rem;
                 text-align: right; }
    #rec-start-btn { background: #2d4e8a; color: #fff;
                     padding: 0.4rem 1rem; font-size: 0.9rem; }
    #rec-start-btn:hover:not(:disabled) { background: #3660aa; }
    #rec-start-btn:disabled { background: #444; cursor: default; }
    #rec-stop-btn  { background: #8a2d2d; color: #fff;
                     padding: 0.4rem 1rem; font-size: 0.9rem; }
    #rec-stop-btn:hover:not(:disabled)  { background: #a83636; }
    #rec-stop-btn:disabled  { background: #444; cursor: default; }
    #rec-status { font-size: 0.85rem; color: #888; min-height: 1.2em; }
    .rec-dot { display: inline-block; width: 8px; height: 8px;
               border-radius: 50%; background: #c03030; margin-right: 5px;
               animation: pulse 1s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.15 } }
    .file-list { display: flex; flex-direction: column; gap: 0.3rem; }
    .file-item a { color: #6ab0f5; font-size: 0.85rem; text-decoration: none; }
    .file-item a:hover { text-decoration: underline; }
    .file-sz { color: #555; font-size: 0.8rem; margin-left: 0.4rem; }
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

  <hr class="divider">

  <div class="section">
    <div class="section-title">Recording</div>
    <div class="rec-row">
      Duration:
      <input type="number" id="dur-input" value="60" min="1" max="1440">
      min
      <button id="rec-start-btn" onclick="startRecording()">&#9210; Rec</button>
      <button id="rec-stop-btn"  onclick="stopRecording()" disabled>&#9632; Stop</button>
    </div>
    <div id="rec-status"></div>
    <div id="rec-files"></div>
  </div>

  <hr class="divider">
  <p id="status"></p>

  <script>
    const STREAM_TIMEOUT_S = 10 * 60;
    let countdownId, deadlineId, flashTimer, statusPollId;

    // ── Init ────────────────────────────────────────────────────────────────
    window.addEventListener('load', async () => {
      try {
        const d = await fetch('/flash/state').then(r => r.json());
        document.getElementById('flash-slider').value = d.brightness;
        document.getElementById('flash-label').textContent = d.brightness;
      } catch (_) {}
      await pollStatus();
      statusPollId = setInterval(pollStatus, 3000);
    });

    // ── Live stream ─────────────────────────────────────────────────────────
    function startStream() {
      const img = document.getElementById('stream');
      const ph  = document.getElementById('placeholder');
      document.getElementById('start-btn').disabled = true;
      document.getElementById('stop-btn').disabled  = false;
      setStatus('Connecting\u2026 (first frame ~10 s)');
      img.onload  = () => { ph.style.display = 'none'; img.style.display = 'block'; setStatus(''); };
      img.onerror = () => { setStatus('Stream ended \u2014 press Start to reconnect'); resetStreamUI(); };
      img.src = '/stream?' + Date.now();
      let rem = STREAM_TIMEOUT_S;
      countdownId = setInterval(() => {
        rem--;
        setStatus('Live \u2014 auto-stop in ' + fmt(rem));
        if (rem <= 0) stopStream();
      }, 1000);
      deadlineId = setTimeout(stopStream, STREAM_TIMEOUT_S * 1000);
    }

    function stopStream() {
      const img = document.getElementById('stream');
      img.src = '';
      img.style.display = 'none';
      document.getElementById('placeholder').style.display = '';
      clearInterval(countdownId); clearTimeout(deadlineId);
      flashOff();
      resetStreamUI();
    }

    function resetStreamUI() {
      document.getElementById('start-btn').disabled = false;
      document.getElementById('stop-btn').disabled  = true;
      if (!document.getElementById('status').textContent.includes('error'))
        setStatus('');
    }

    // ── Flash ───────────────────────────────────────────────────────────────
    function onFlashToggle() {
      const btn = document.getElementById('flash-btn');
      const on  = btn.classList.toggle('on');
      btn.textContent = on ? 'On' : 'Off';
      fetch('/flash/' + (on ? document.getElementById('flash-slider').value : 0));
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

    // ── Recording ───────────────────────────────────────────────────────────
    async function startRecording() {
      const mins = Math.max(1, parseInt(document.getElementById('dur-input').value) || 60);
      // Proactively disconnect live view — recording restarts the pipeline.
      const wasStreaming = !document.getElementById('stop-btn').disabled;
      if (wasStreaming) {
        const img = document.getElementById('stream');
        img.src = '';
        img.style.display = 'none';
        document.getElementById('placeholder').style.display = '';
        clearInterval(countdownId); clearTimeout(deadlineId);
        flashOff(); resetStreamUI();
      }
      document.getElementById('rec-start-btn').disabled = true;
      document.getElementById('rec-stop-btn').disabled  = false;
      await fetch('/record/start?duration=' + (mins * 60));
      setStatus(wasStreaming
        ? 'Recording started \u2014 press Start to watch live'
        : '');
      await pollStatus();
    }

    async function stopRecording() {
      await fetch('/record/stop');
      // Pipeline dies — clean up stream UI too.
      const img = document.getElementById('stream');
      img.src = '';
      img.style.display = 'none';
      document.getElementById('placeholder').style.display = '';
      clearInterval(countdownId); clearTimeout(deadlineId);
      flashOff(); resetStreamUI();
      await pollStatus();
    }

    async function pollStatus() {
      try {
        applyStatus(await fetch('/record/status').then(r => r.json()));
      } catch (_) {}
    }

    function applyStatus(s) {
      const el       = document.getElementById('rec-status');
      const startBtn = document.getElementById('rec-start-btn');
      const stopBtn  = document.getElementById('rec-stop-btn');
      const filesEl  = document.getElementById('rec-files');

      if (s.running && s.mode === 'record') {
        const rem = s.remaining !== null ? ', ' + fmt(s.remaining) + ' remaining' : '';
        el.innerHTML = '<span class="rec-dot"></span>Recording \u2014 '
          + fmt(s.elapsed) + ' elapsed' + rem;
        startBtn.disabled = true;
        stopBtn.disabled  = false;
      } else {
        el.textContent = '';
        startBtn.disabled = false;
        stopBtn.disabled  = true;
      }

      if (s.recordings && s.recordings.length) {
        filesEl.innerHTML = '<div class="file-list">'
          + s.recordings.map(f =>
              '<div class="file-item">'
              + '<a href="/recordings/' + f.name + '" download>' + f.name + '</a>'
              + '<span class="file-sz">' + fmtSize(f.size) + '</span>'
              + '</div>'
            ).join('')
          + '</div>';
      } else {
        filesEl.innerHTML = '';
      }
    }

    // ── Helpers ─────────────────────────────────────────────────────────────
    function setStatus(msg) { document.getElementById('status').textContent = msg; }
    function fmt(s) { return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); }
    function fmtSize(b) {
      if (b < 1024)       return b + ' B';
      if (b < 1048576)    return (b/1024).toFixed(1) + ' KB';
      if (b < 1073741824) return (b/1048576).toFixed(1) + ' MB';
      return (b/1073741824).toFixed(2) + ' GB';
    }
  </script>
</body>
</html>
""".encode('utf-8')


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

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
        path = self.path.split("?")[0]
        query = self.path[len(path) + 1:] if "?" in self.path else ""

        # ── Flash ─────────────────────────────────────────────────────────
        if path == "/flash/state":
            self._json({"brightness": _saved_brightness})
            return

        if path.startswith("/flash/"):
            try:
                val = int(path[len("/flash/"):])
                res = flash_set(val)
                self._json({"brightness": res if res is not None else flash_get()})
            except ValueError:
                self.send_error(400, "brightness must be an integer 0-255")
            return

        # ── Recording control ──────────────────────────────────────────────
        if path == "/record/start":
            try:
                params = dict(
                    p.split("=", 1) for p in query.split("&") if "=" in p
                )
                duration = max(60, int(params.get("duration", 3600)))
            except ValueError:
                duration = 3600
            _pipeline.start_record(duration)
            self._json({"ok": True})
            return

        if path == "/record/stop":
            _pipeline.stop()
            self._json({"ok": True})
            return

        if path == "/record/status":
            self._json(_pipeline.get_status())
            return

        # ── File download ──────────────────────────────────────────────────
        if path.startswith("/recordings/"):
            filename = path[len("/recordings/"):]
            if not filename or "/" in filename or not filename.endswith(".avi"):
                self.send_error(400, "Invalid filename")
                return
            filepath = os.path.join(RECORDINGS_DIR, filename)
            if not os.path.isfile(filepath):
                self.send_error(404, "Not found")
                return
            size = os.path.getsize(filepath)
            self.send_response(200)
            self.send_header("Content-Type", "video/x-msvideo")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            self.end_headers()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        # ── MJPEG live stream ──────────────────────────────────────────────
        if path == "/stream":
            # If no pipeline is running, start a stream-only one.
            # If the recording pipeline is already running, we just subscribe
            # to it — no restart, no warm-up delay.
            _pipeline.start_stream()

            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=--frame"
            )
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()

            q = _pipeline.subscribe()
            try:
                while True:
                    try:
                        chunk = q.get(timeout=30)
                    except Empty:
                        break          # pipeline likely dead/not starting
                    if chunk is None:
                        break          # sentinel: pipeline stopped
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                _pipeline.unsubscribe(q)
            return

        # ── Default: serve page ────────────────────────────────────────────
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.end_headers()
        self.wfile.write(HTML)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("Camera web server on http://0.0.0.0:8080")
    server.serve_forever()
