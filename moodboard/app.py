#!/usr/bin/env python3
# app.py - phone-friendly web app around reel_to_moodboard.py
#
# Share a Reel from Instagram (or TikTok / YouTube Shorts) straight into this
# app and get a hero-shot collage back. Android: install the site as an app and it
# appears in the share sheet. iPhone: a two-action Shortcut does the same.
# The /setup page walks through both.
#
# Run locally:
#   pip install -r requirements.txt
#   export ANTHROPIC_API_KEY=sk-ant-...   # optional; without it shots are picked by sharpness
#   python app.py                      # http://localhost:8000
#
# Environment:
#   ANTHROPIC_API_KEY  optional - Claude picks the shots; without it they're picked by sharpness (free)
#   APP_PASSWORD       recommended - anyone with the link could otherwise spend your API credits
#   SECRET_KEY         signs the login cookie (set any long random string)
#   IG_COOKIES         optional - Instagram cookies.txt contents, for reels Instagram won't serve anonymously
#   MOODBOARD_MODEL    optional - Claude model ID (default: claude-sonnet-5-5)
#   DATA_DIR           optional - where boards are stored (default: ./data)

import hmac
import html
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import reel_to_moodboard as engine

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data")).resolve()
BOARDS_DIR = DATA_DIR / "boards"
UPLOADS_DIR = DATA_DIR / "uploads"
for d in (BOARDS_DIR, UPLOADS_DIR):
    d.mkdir(parents=True, exist_ok=True)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MODEL = os.environ.get("MOODBOARD_MODEL", engine.DEFAULT_MODEL)
USE_AI = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
LINK_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

app = Flask(__name__, static_folder=None)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    PERMANENT_SESSION_LIFETIME=timedelta(days=400),
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("COOKIE_SECURE")),
    MAX_CONTENT_LENGTH=300 * 1024 * 1024,
)

# Instagram cookies arrive as an env var; yt-dlp wants a file.
COOKIE_FILE = None
if os.environ.get("IG_COOKIES", "").strip():
    _fd, COOKIE_FILE = tempfile.mkstemp(prefix="ig_cookies_", suffix=".txt")
    with os.fdopen(_fd, "w") as fh:
        fh.write(os.environ["IG_COOKIES"].strip() + "\n")

# In-memory job table. One worker process (see Dockerfile), so this is shared.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
# Small servers choke on several videos at once; run them one after another.
WORK_SLOT = threading.Semaphore(int(os.environ.get("MAX_PARALLEL_JOBS", "1")))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

OPEN_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js"}


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return None
    if request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    if session.get("ok"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="login required"), 401
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


def safe_next(target: str | None) -> str:
    # Only allow same-site relative paths.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/"


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    nxt = safe_next(request.values.get("next"))
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), APP_PASSWORD.encode()):
            session.permanent = True
            session["ok"] = True
            return redirect(nxt)
        error = '<p class="err">That password didn\'t match.</p>'
    body = f"""
    <h1>Moodboard</h1>
    <p class="muted">Enter the password you set when deploying. You'll stay signed in on this phone.</p>
    {error}
    <form method="post" class="stack">
      <input type="hidden" name="next" value="{html.escape(nxt)}">
      <input type="password" name="password" placeholder="Password" autocomplete="current-password" required autofocus>
      <button type="submit">Sign in</button>
    </form>"""
    return page("Sign in", body)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def new_id() -> str:
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


def set_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        JOBS[job_id].update(fields)


def run_job(job_id: str, source: str, label: str, upload_path: Path | None) -> None:
    def report(step: str) -> None:
        set_job(job_id, step=step)

    try:
        set_job(job_id, step="Waiting for a free slot")
        with WORK_SLOT:
            set_job(job_id, status="running", step="Starting")
            collage = engine.create_moodboard(source, MODEL, use_ai=USE_AI, cookie_file=COOKIE_FILE, progress=report)
        save_board(job_id, collage, label)
        set_job(job_id, status="done", step="Done")
    except engine.MoodboardError as exc:
        set_job(job_id, status="error", error=str(exc))
    except Exception as exc:  # keep the worker alive and tell the user something useful
        app.logger.exception("Job %s crashed", job_id)
        set_job(job_id, status="error", error=f"Something went wrong on the server: {exc}")
    finally:
        if upload_path:
            upload_path.unlink(missing_ok=True)


def start_job(source: str, label: str, upload_path: Path | None = None) -> str:
    job_id = new_id()
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "step": "Queued", "error": "", "label": label, "created": time.time()}
        # Forget finished jobs after a day so memory stays flat.
        cutoff = time.time() - 86400
        for jid in [j for j, v in JOBS.items() if v["created"] < cutoff and v["status"] in ("done", "error")]:
            del JOBS[jid]
    threading.Thread(target=run_job, args=(job_id, source, label, upload_path), daemon=True).start()
    return job_id


# ---------------------------------------------------------------------------
# Board storage
# ---------------------------------------------------------------------------


def board_dir(board_id: str) -> Path:
    if not ID_RE.match(board_id):
        abort(404)
    return BOARDS_DIR / board_id


def save_board(board_id: str, collage, label: str) -> None:
    from PIL import Image

    d = BOARDS_DIR / board_id
    d.mkdir(parents=True, exist_ok=True)
    engine.save_jpeg(collage, d / engine.OUTPUT_NAME)
    # Gallery thumbnail: the top of the strip at 3:4, where the best shots sit.
    width = 480
    thumb = collage.resize((width, round(collage.height * width / collage.width)), Image.LANCZOS)
    thumb.crop((0, 0, width, min(thumb.height, width * 4 // 3))).save(d / "thumb.jpg", "JPEG", quality=82)
    meta = {"source": label, "created": time.time(), "width": collage.width, "height": collage.height}
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def load_board(board_id: str) -> dict:
    d = board_dir(board_id)
    if not (d / "meta.json").exists() or not (d / engine.OUTPUT_NAME).exists():
        abort(404)
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


def list_boards() -> list[dict]:
    out = []
    for d in BOARDS_DIR.iterdir():
        meta = d / "meta.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": d.name, "created": data["created"]})
    return sorted(out, key=lambda b: b["created"], reverse=True)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

BASE_CSS = """
:root { --bg:#0e0e10; --panel:#18181b; --line:#2a2a2f; --text:#ececef; --muted:#9a9aa3; --accent:#e8e3d9; }
* { box-sizing:border-box; }
[hidden] { display:none !important; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--text);
  font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif; }
.wrap { max-width:720px; margin:0 auto; padding:28px 16px 64px; }
h1 { font-size:30px; line-height:1.1; letter-spacing:-.02em; margin:0 0 8px; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:32px 0 12px; }
p { margin:0 0 12px; }
a { color:var(--text); }
.muted { color:var(--muted); }
.err { color:#ff9b8f; white-space:pre-wrap; overflow-wrap:anywhere; }
.stack { display:flex; flex-direction:column; gap:10px; }
.row { display:flex; gap:8px; }
.row > input { flex:1; min-width:0; }
input[type=url], input[type=password], input[type=text] {
  font:inherit; color:var(--text); background:var(--panel); border:1px solid var(--line);
  border-radius:12px; padding:13px 14px; width:100%; }
input:focus { outline:2px solid var(--accent); outline-offset:1px; }
button, .btn { font-family:inherit; font-size:15px; font-weight:600; line-height:1; border:0; border-radius:12px; padding:14px 18px;
  background:var(--accent); color:#111; cursor:pointer; text-decoration:none; display:inline-block; text-align:center; }
button.ghost, .btn.ghost { background:var(--panel); color:var(--text); border:1px solid var(--line); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px; }
.file { display:block; border:1px dashed var(--line); border-radius:12px; padding:18px; text-align:center;
  color:var(--muted); cursor:pointer; }
.file input { display:none; }
.boards { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
@media (min-width:560px) { .boards { grid-template-columns:repeat(3,minmax(0,1fr)); } }
.bcard { display:block; text-decoration:none; background:var(--panel); border-radius:12px; overflow:hidden; }
.bcard img { display:block; width:100%; aspect-ratio:3/4; object-fit:cover; object-position:top; background:#222; }
.nav { display:flex; gap:8px; align-items:center; margin-bottom:16px; }
.nav .grow { flex:1; }
.nav a, .nav button { font-size:13px; padding:9px 14px; border-radius:999px; background:var(--panel);
  color:var(--text); border:1px solid var(--line); text-decoration:none; }
.collage { display:block; width:100%; height:auto; border-radius:12px; }
ol.steps { padding-left:20px; margin:0; }
ol.steps li { margin:0 0 10px; }
code { background:#232328; padding:2px 6px; border-radius:6px; font-size:14px; overflow-wrap:anywhere; }
.spinner { width:28px; height:28px; border:3px solid var(--line); border-top-color:var(--accent);
  border-radius:50%; animation:spin .9s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.status { display:flex; align-items:center; gap:14px; margin:20px 0; }
.tabs { display:flex; gap:8px; margin-bottom:12px; }
.tabs button { flex:1; }
.tabs button[aria-pressed=false] { background:var(--panel); color:var(--text); border:1px solid var(--line); }
"""


def page(title: str, body: str, head_extra: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e0e10">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Moodboard">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/static/icon-192.png">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<title>{html.escape(title)}</title>
<style>{BASE_CSS}</style>
{head_extra}
</head>
<body>
<div class="wrap">
{body}
</div>
<script>if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>
</body>
</html>"""


@app.route("/")
def home():
    notice = ""
    if request.args.get("nolink"):
        notice = '<p class="err">There was no link in what you shared. Copy the reel link and paste it below.</p>'
    cards = ""
    for b in list_boards():
        cards += (
            f'<a class="bcard" href="/b/{b["id"]}" aria-label="Open board">'
            f'<img src="/b/{b["id"]}/thumb.jpg" alt="" loading="lazy"></a>'
        )
    boards_html = f'<div class="boards">{cards}</div>' if cards else '<p class="muted">No boards yet. Your first one will show up here.</p>'
    body = f"""
    <h1>Moodboard</h1>
    <p class="muted">Share a reel here, or paste its link.</p>
    {notice}
    <form method="post" action="/make" class="stack" style="margin-top:20px">
      <div class="row">
        <input type="url" name="url" id="url" placeholder="https://www.instagram.com/reel/…" inputmode="url" autocomplete="off">
        <button type="button" class="ghost" id="paste">Paste</button>
      </div>
      <button type="submit">Make moodboard</button>
    </form>
    <form method="post" action="/make" enctype="multipart/form-data" id="upform" style="margin-top:12px">
      <label class="file">Or pick a video from your phone
        <input type="file" name="video" accept="video/*" id="video">
      </label>
    </form>
    <p style="margin-top:16px"><a href="/setup">Set up sharing from Instagram</a></p>
    <h2>Your boards</h2>
    {boards_html}
    <script>
      const p = document.getElementById('paste');
      if (!navigator.clipboard || !navigator.clipboard.readText) p.style.display = 'none';
      p.addEventListener('click', async () => {{
        try {{
          const t = await navigator.clipboard.readText();
          const m = t.match(/https?:\\/\\/\\S+/);
          document.getElementById('url').value = m ? m[0] : t.trim();
        }} catch (e) {{ document.getElementById('url').focus(); }}
      }});
      document.getElementById('video').addEventListener('change', e => {{
        if (e.target.files.length) {{
          e.target.closest('label').firstChild.textContent = 'Uploading ' + e.target.files[0].name + '…';
          document.getElementById('upform').submit();
        }}
      }});
    </script>"""
    return page("Moodboard", body)


def first_link(*values: str | None) -> str | None:
    for v in values:
        m = LINK_RE.search(v or "")
        if m:
            return m.group(0).rstrip(".,;:!?)]}")
    return None


@app.route("/share", methods=["GET", "POST"])
def share():
    # Web Share Target (Android) and the iOS Shortcut both land here.
    link = first_link(request.values.get("url"), request.values.get("text"), request.values.get("title"))
    if not link:
        return redirect("/?nolink=1")
    return redirect(url_for("job_page", job_id=start_job(link, link)))


@app.route("/make", methods=["POST"])
def make():
    upload = request.files.get("video")
    if upload and upload.filename:
        ext = Path(upload.filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            ext = ".mp4"
        path = UPLOADS_DIR / f"{new_id()}{ext}"
        upload.save(path)
        return redirect(url_for("job_page", job_id=start_job(str(path), upload.filename, upload_path=path)))
    link = first_link(request.form.get("url"))
    if not link:
        return redirect("/?nolink=1")
    return redirect(url_for("job_page", job_id=start_job(link, link)))


@app.route("/job/<job_id>")
def job_page(job_id):
    if job_id not in JOBS:
        if (board_dir(job_id) / "board.json").exists():
            return redirect(url_for("board_page", board_id=job_id))
        body = '<h1>Not found</h1><p class="muted">This job is gone, probably because the server restarted. Share the reel again.</p><p><a class="btn" href="/">Back</a></p>'
        return page("Not found", body), 404
    label = html.escape(JOBS[job_id]["label"])
    body = f"""
    <p><a href="/" class="muted">← Boards</a></p>
    <h1>Making your board</h1>
    <p class="muted" style="overflow-wrap:anywhere">{label}</p>
    <div class="status" id="status"><div class="spinner"></div><span id="step">Starting</span></div>
    <p class="muted" id="hint">This usually takes 20-60 seconds. You can leave this page; the board will be in your list when it's ready.</p>
    <div id="fail" hidden>
      <p class="err" id="error"></p>
      <p class="row"><a class="btn" href="/">Try another</a></p>
    </div>
    <script>
      async function poll() {{
        try {{
          const r = await fetch('/api/job/{job_id}', {{cache: 'no-store'}});
          if (r.status === 404) {{ location.reload(); return; }}
          const j = await r.json();
          if (j.status === 'done') {{ location.replace('/b/{job_id}'); return; }}
          if (j.status === 'error') {{
            document.getElementById('status').hidden = true;
            document.querySelector('h1').textContent = "Couldn't make this one";
            document.getElementById('hint').hidden = true;
            document.getElementById('error').textContent = j.error;
            document.getElementById('fail').hidden = false;
            return;
          }}
          document.getElementById('step').textContent = j.step;
        }} catch (e) {{}}
        setTimeout(poll, 1500);
      }}
      poll();
    </script>"""
    return page("Making your board", body)


@app.route("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="not found"), 404
    return jsonify(status=job["status"], step=job["step"], error=job["error"])


@app.route("/b/<board_id>")
def board_page(board_id):
    data = load_board(board_id)
    img = f"/b/{board_id}/{engine.OUTPUT_NAME}"
    body = f"""
    <nav class="nav">
      <a href="/">← Boards</a><span class="grow"></span>
      <button type="button" id="sharebtn" hidden>Share</button>
      <a href="/b/{board_id}/download">Save</a>
      <form method="post" action="/b/{board_id}/delete" onsubmit="return confirm('Delete this board?')" style="margin:0">
        <button type="submit">Delete</button>
      </form>
    </nav>
    <img class="collage" src="{img}" width="{data['width']}" height="{data['height']}" alt="Moodboard collage">
    <script>
      const b = document.getElementById('sharebtn');
      if (navigator.share) {{
        b.hidden = false;
        b.onclick = async () => {{
          // Share the picture itself where the phone allows it (straight to
          // Pinterest, Photos, a chat...), otherwise share the link.
          try {{
            const blob = await (await fetch('{img}')).blob();
            const file = new File([blob], '{engine.OUTPUT_NAME}', {{type: 'image/jpeg'}});
            if (navigator.canShare && navigator.canShare({{files: [file]}})) {{
              await navigator.share({{files: [file]}});
              return;
            }}
          }} catch (e) {{ if (e.name === 'AbortError') return; }}
          navigator.share({{url: location.href}}).catch(() => {{}});
        }};
      }}
    </script>"""
    return page("Moodboard", body)


@app.route(f"/b/<board_id>/{engine.OUTPUT_NAME}")
def board_image(board_id):
    load_board(board_id)
    return send_from_directory(board_dir(board_id), engine.OUTPUT_NAME, max_age=86400)


@app.route("/b/<board_id>/download")
def board_download(board_id):
    load_board(board_id)
    return send_from_directory(board_dir(board_id), engine.OUTPUT_NAME, as_attachment=True,
                               download_name=f"moodboard_scroll_{board_id}.jpg")


@app.route("/b/<board_id>/thumb.jpg")
def board_thumb(board_id):
    d = board_dir(board_id)
    if not (d / "thumb.jpg").exists():
        abort(404)
    return send_from_directory(d, "thumb.jpg", max_age=86400)


@app.route("/b/<board_id>/delete", methods=["POST"])
def board_delete(board_id):
    d = board_dir(board_id)
    if d.exists():
        shutil.rmtree(d)
    return redirect("/")


@app.route("/setup")
def setup():
    base = request.host_url.rstrip("/")
    shortcut_url = f"{base}/share?url="
    body = f"""
    <p><a href="/" class="muted">← Boards</a></p>
    <h1>Share from Instagram</h1>
    <p class="muted">Do this once. After that, tap Share on any reel and send it here.</p>
    <div class="tabs">
      <button type="button" data-tab="android" aria-pressed="false">Android</button>
      <button type="button" data-tab="ios" aria-pressed="false">iPhone</button>
    </div>

    <div class="card" id="android" hidden>
      <ol class="steps">
        <li>Open <code>{html.escape(base)}</code> in <strong>Chrome</strong> and sign in.</li>
        <li>Tap <strong>⋮</strong> (top right), then <strong>Add to Home screen</strong>, then <strong>Install</strong>.</li>
        <li>In Instagram, open a reel, tap <strong>Share</strong> (paper plane), then <strong>Share to…</strong> or <strong>More</strong>.</li>
        <li>Pick <strong>Moodboard</strong>. Your board opens when it's ready.</li>
      </ol>
      <p class="muted" style="margin-top:12px">If Moodboard isn't in the list, open the installed app once, then try again.</p>
    </div>

    <div class="card" id="ios" hidden>
      <p>iPhone doesn't let web apps join the share sheet directly, so a tiny Shortcut does it.</p>
      <ol class="steps">
        <li>Open this page in <strong>Safari</strong> and sign in, so Safari remembers you.</li>
        <li>Open the <strong>Shortcuts</strong> app, tap <strong>+</strong>, and name it <strong>Moodboard</strong>.</li>
        <li>Add the action <strong>URL Encode</strong>. It fills in <em>Shortcut Input</em> on its own.</li>
        <li>Add the action <strong>Text</strong> and type this, then insert the <em>URL Encoded Text</em> variable right after it:
          <p class="row" style="margin:8px 0"><input type="text" readonly value="{html.escape(shortcut_url)}" id="sc"><button type="button" class="ghost" id="copysc">Copy</button></p></li>
        <li>Add the action <strong>Open URLs</strong>.</li>
        <li>Tap the <strong>ⓘ</strong> at the bottom, turn on <strong>Show in Share Sheet</strong>, and set it to receive <strong>URLs</strong> and <strong>Text</strong>.</li>
        <li>In Instagram, tap <strong>Share</strong> on a reel, then <strong>Share to…</strong> or <strong>More</strong>, then <strong>Moodboard</strong>.</li>
      </ol>
      <p class="muted" style="margin-top:12px">Quicker option without a Shortcut: tap <strong>Copy link</strong> in Instagram, open this app, and tap <strong>Paste</strong>.
      To keep it on your home screen: Safari share button → <strong>Add to Home Screen</strong>.</p>
    </div>

    <h2>If Instagram won't let a reel through</h2>
    <p class="muted">Instagram sometimes blocks downloads from servers. When that happens you'll see a message saying so. Two ways around it:</p>
    <ol class="steps muted">
      <li>Save or screen-record the reel, then use <strong>Pick a video</strong> on the home page.</li>
      <li>Give the server your Instagram cookies (one-time, needs a computer): export <code>cookies.txt</code> for instagram.com with a browser extension, then paste its contents into the <code>IG_COOKIES</code> setting on your host.</li>
    </ol>
    <script>
      const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
      function show(tab) {{
        for (const b of document.querySelectorAll('[data-tab]')) {{
          const on = b.dataset.tab === tab;
          b.setAttribute('aria-pressed', on);
          document.getElementById(b.dataset.tab).hidden = !on;
        }}
      }}
      document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => show(b.dataset.tab));
      show(isIOS ? 'ios' : 'android');
      document.getElementById('copysc').onclick = async (e) => {{
        const v = document.getElementById('sc').value;
        try {{ await navigator.clipboard.writeText(v); e.target.textContent = 'Copied'; }}
        catch (_) {{ document.getElementById('sc').select(); }}
      }};
    </script>"""
    return page("Set up sharing", body)


# ---------------------------------------------------------------------------
# PWA plumbing
# ---------------------------------------------------------------------------


@app.route("/manifest.webmanifest")
def manifest():
    data = {
        "id": "/",
        "name": "Moodboard",
        "short_name": "Moodboard",
        "description": "Turn reels into moodboards.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0e0e10",
        "theme_color": "#0e0e10",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "share_target": {
            "action": "/share",
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
    }
    return Response(json.dumps(data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    # Network-only: boards are made on the server, so there's nothing useful to cache.
    # The fetch handler exists so browsers treat the site as installable.
    js = "self.addEventListener('install', () => self.skipWaiting());\n" \
         "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));\n" \
         "self.addEventListener('fetch', () => {});\n"
    return Response(js, mimetype="text/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(HERE / "static", name, max_age=604800)


@app.route("/healthz")
def healthz():
    return "ok"


@app.errorhandler(413)
def too_big(_):
    return page("Too big", '<h1>That video is too big</h1><p class="muted">Keep uploads under 300 MB.</p><p><a class="btn" href="/">Back</a></p>'), 413


if not APP_PASSWORD:
    app.logger.warning("APP_PASSWORD is not set: anyone with the URL can use your API credits.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)
