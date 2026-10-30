#!/usr/bin/env python3
# reel_to_moodboard.py
#
# Turns a short vertical video (Instagram Reel, TikTok, YouTube Short, or a
# local MP4) into a self-contained HTML moodboard: sharp keyframes, a colour
# palette, aesthetic tags and a short vibe description written by Claude.
#
# Install:
#   pip install anthropic opencv-python scenedetect yt-dlp numpy
#
# Run:
#   export ANTHROPIC_API_KEY=sk-ant-...
#   python reel_to_moodboard.py path/to/reel.mp4
#   python reel_to_moodboard.py "https://www.instagram.com/reel/XXXXXXXX/"
#   python reel_to_moodboard.py clip.mp4 --out boards/ --no-open
#
# Notes:
#   - claude-3-5-sonnet-20241022 has been retired by Anthropic, so the default
#     model is the current Sonnet (claude-sonnet-5-5). Use --model to change it.
#   - Downloading from Instagram/TikTok can require cookies for private or
#     age-gated posts; pass --cookies-from-browser chrome (or firefox, etc.),
#     or --cookies cookies.txt (Netscape format).
#   - app.py in this folder wraps this script in a phone-friendly web app.

import argparse
import base64
import html
import importlib
import os
import re
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_CANDIDATES = 8
MIN_KEEP = 4
MAX_KEEP = 6
MAX_SIDE = 1024
JPEG_QUALITY = 88
# A frame is treated as blurry when its sharpness is below this share of the
# sharpest candidate. MIN_KEEP still wins, so short clips never run dry.
BLUR_RATIO = 0.25

CATEGORIES = [
    "Fashion Try-On Haul",
    "Dance Reel",
    "Thirst Trap",
    "Fitness",
    "Lifestyle",
    "Beauty",
]

# Models that accept the server-side refusal fallback parameter.
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

REQUIRED_MODULES = {
    # import name: pip package
    "cv2": "opencv-python",
    "numpy": "numpy",
    "scenedetect": "scenedetect",
    "anthropic": "anthropic",
}


class MoodboardError(Exception):
    """A problem worth showing to the user as-is (bad input, download or API failure)."""


# Per-thread progress callback, so the web app can show live status per job.
_progress = threading.local()


def fail(message: str) -> None:
    raise MoodboardError(message)


def log(message: str) -> None:
    print(f"[reel2mood] {message}", flush=True)
    reporter = getattr(_progress, "reporter", None)
    if reporter:
        reporter(message)


def check_dependencies(need_ytdlp: bool) -> None:
    modules = dict(REQUIRED_MODULES)
    if need_ytdlp:
        modules["yt_dlp"] = "yt-dlp"
    missing = []
    for module, package in modules.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if missing:
        fail(
            "Missing Python packages: "
            + ", ".join(missing)
            + "\nInstall them with:\n  pip install "
            + " ".join(missing)
        )


# ---------------------------------------------------------------------------
# 1. Ingestion
# ---------------------------------------------------------------------------


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def explain_download_error(exc: Exception) -> str:
    text = ANSI_RE.sub("", str(exc)).replace("ERROR: ", "").strip()
    lowered = text.lower()
    if any(k in lowered for k in ("login", "cookies", "rate-limit", "rate limit", "not available", "private")):
        return (
            "Instagram wouldn't hand over this video without a logged-in session. "
            "Add Instagram cookies (see the setup notes) or upload the video file instead.\n\n"
            f"Details: {text}"
        )
    if "unsupported url" in lowered:
        return f"That link isn't a video page yt-dlp knows how to read.\n\nDetails: {text}"
    return f"Couldn't download the video.\n\nDetails: {text}"


def download_video(url: str, workdir: str, cookies_browser: str | None = None, cookie_file: str | None = None) -> str:
    import yt_dlp

    log("Downloading the video")
    opts = {
        # Video-only MP4 first: no audio needed, and it avoids an ffmpeg merge.
        # 1280px tall is plenty for 1024px frames and keeps downloads small.
        "format": "bv*[ext=mp4][height<=1280]/b[ext=mp4][height<=1280]/bv*[ext=mp4]/b[ext=mp4]/bv*/b",
        "outtmpl": os.path.join(workdir, "reel.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    if cookie_file:
        opts["cookiefile"] = cookie_file
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("_type") == "playlist":
                # Carousel posts: take the first video entry.
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    fail("That post has no video in it.")
                info = entries[0]
            downloads = info.get("requested_downloads") or []
            path = downloads[0].get("filepath") if downloads else ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as exc:
        fail(explain_download_error(exc))
    if not path or not os.path.isfile(path):
        fail("yt-dlp reported success but no video file was written.")
    log("Video downloaded")
    return path


def resolve_source(source: str, workdir: str, cookies_browser: str | None = None, cookie_file: str | None = None) -> str:
    if URL_RE.match(source):
        return download_video(source, workdir, cookies_browser, cookie_file)
    path = Path(source).expanduser()
    if not path.exists():
        fail(f"Video file not found: {path}")
    if not path.is_file():
        fail(f"Not a file: {path}")
    return str(path.resolve())


# ---------------------------------------------------------------------------
# 2. Shot detection and frame extraction
# ---------------------------------------------------------------------------


def detect_shot_frames(video_path: str, total_frames: int) -> list[int]:
    """Return one frame index per detected shot (the middle of each shot)."""
    from scenedetect import ContentDetector, detect

    log("Detecting scene cuts")
    try:
        scenes = detect(video_path, ContentDetector(threshold=27.0))
    except Exception as exc:  # scenedetect raises several backend-specific errors
        log(f"Scene detection failed ({exc}); falling back to even sampling")
        scenes = []

    picks = []
    for start, end in scenes:
        s, e = start.get_frames(), end.get_frames()
        if e > s:
            picks.append((s + e) // 2)
    log(f"Found {len(scenes)} shot(s)")

    # Single-shot clips (very common for dance reels) give us too few frames,
    # so top up with evenly spaced samples across the whole clip.
    if len(picks) < MAX_CANDIDATES and total_frames > 0:
        needed = MAX_CANDIDATES - len(picks)
        step = total_frames / (needed + 1)
        extra = [int(step * (i + 1)) for i in range(needed)]
        # Skip samples that sit right next to an existing pick.
        gap = max(1, total_frames // (MAX_CANDIDATES * 2))
        for idx in extra:
            if all(abs(idx - p) > gap for p in picks):
                picks.append(idx)

    picks = sorted(set(picks))
    if len(picks) > MAX_CANDIDATES:
        # Spread the cap evenly over the shots rather than taking the first N.
        step = len(picks) / MAX_CANDIDATES
        picks = [picks[int(i * step)] for i in range(MAX_CANDIDATES)]
    return picks


def resize_to_max_side(frame, max_side: int = MAX_SIDE):
    import cv2

    h, w = frame.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return frame
    scale = max_side / longest
    return cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def sharpness(frame) -> float:
    import cv2

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def extract_candidates(video_path: str) -> list[dict]:
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        fail(f"OpenCV could not open the video (unsupported codec or corrupt file): {video_path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if total <= 0:
        cap.release()
        fail("The video reports zero frames. Is it a valid video file?")

    indices = detect_shot_frames(video_path, total)
    candidates = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        frame = resize_to_max_side(frame)
        candidates.append({"frame_index": idx, "time": idx / fps, "image": frame, "sharpness": sharpness(frame)})
    cap.release()

    if not candidates:
        fail("Could not read any frames from the video.")
    log(f"Extracted {len(candidates)} candidate frame(s)")
    return candidates


def keep_sharpest(candidates: list[dict]) -> list[dict]:
    ranked = sorted(candidates, key=lambda c: c["sharpness"], reverse=True)
    best = ranked[0]["sharpness"] or 1.0
    kept = [c for c in ranked[:MAX_KEEP] if c["sharpness"] >= best * BLUR_RATIO]
    if len(kept) < MIN_KEEP:
        kept = ranked[: min(MIN_KEEP, len(ranked))]
    dropped = len(candidates) - len(kept)
    log(f"Kept {len(kept)} sharp frame(s), dropped {dropped} blurry/extra")
    # Back to timeline order so the model reads the clip in sequence.
    return sorted(kept, key=lambda c: c["frame_index"])


def encode_jpeg_b64(frame) -> str:
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        fail("JPEG encoding failed.")
    return base64.b64encode(buf.tobytes()).decode("ascii")


# ---------------------------------------------------------------------------
# 3. Claude vision analysis
# ---------------------------------------------------------------------------

MOODBOARD_TOOL = {
    "name": "generate_moodboard",
    "description": (
        "Record the moodboard for a short-form video. Call this exactly once, "
        "after looking at every frame."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "primary_category",
            "sub_style",
            "selected_frame_indices",
            "color_palette",
            "tags",
            "vibe_description",
        ],
        "properties": {
            "title": {
                "type": "string",
                "description": "A short, evocative board title, 2-5 words.",
            },
            "primary_category": {"type": "string", "enum": CATEGORIES},
            "sub_style": {
                "type": "string",
                "description": (
                    "Specific style / demographic vibe, e.g. 'Desi Traditional / Festal', "
                    "'Blonde / Boho Chic', 'Y2K Streetwear', 'Minimalist Gym', 'Athleisure'."
                ),
            },
            "selected_frame_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "3 or 4 frame numbers showing the cleanest, most representative poses or outfits.",
            },
            "color_palette": {
                "type": "array",
                "description": "Exactly 4 dominant colours, most prominent first.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["hex", "name"],
                    "properties": {
                        "hex": {"type": "string", "description": "Colour as #RRGGBB."},
                        "name": {"type": "string", "description": "Short colour name, e.g. 'Marigold'."},
                    },
                },
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exactly 5 short aesthetic keywords.",
            },
            "vibe_description": {
                "type": "string",
                "description": "Exactly two sentences describing the overall vibe.",
            },
        },
    },
}

SYSTEM_PROMPT = """You are a fashion and social-media trend analyst building moodboards from short-form video.

You will see numbered keyframes from one Reel / TikTok / Short. Study all of them, then call the generate_moodboard tool once. Do not answer in plain text.

Guidance:
- primary_category: choose the single best fit from the allowed list.
- sub_style: be specific about the aesthetic and cultural styling you can see (garments, fabrics, jewellery, setting, grooming). Avoid vague labels like "casual" or "trendy".
- selected_frame_indices: pick 3 or 4 frame numbers that are in focus, well framed, and together show the range of looks or poses. Prefer distinct outfits or poses over near-duplicates.
- color_palette: 4 colours that actually dominate the clothing, styling and set, not generic black/white unless they truly dominate.
- tags: 5 lowercase aesthetic keywords.
- vibe_description: two plain sentences a stylist would write. Describe what is visible; do not guess anyone's identity."""


def analyse_frames(frames: list[dict], model: str) -> dict:
    import anthropic

    content = []
    for i, f in enumerate(frames):
        content.append({"type": "text", "text": f"Frame {i} (t={f['time']:.1f}s)"})
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f["b64"]}}
        )
    content.append(
        {
            "type": "text",
            "text": f"Frame numbers run 0 to {len(frames) - 1}. Build the moodboard with the generate_moodboard tool.",
        }
    )

    request = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=[MOODBOARD_TOOL],
        # Current models reject forced tool_choice; the strict schema plus the
        # prompt keeps the output on the tool, and we verify below.
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": content}],
    )

    client = anthropic.Anthropic()
    log(f"Sending {len(frames)} frame(s) to {model}")
    try:
        if model.startswith(FALLBACK_MODELS):
            # On a safety-classifier decline, let the API re-run the request on
            # Anthropic's recommended fallback model instead of failing.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **request
            )
        else:
            response = client.messages.create(**request)
    except anthropic.AuthenticationError:
        fail("Anthropic rejected the API key. Set ANTHROPIC_API_KEY to a valid key.")
    except anthropic.NotFoundError:
        fail(f"Model '{model}' was not found or is retired. Try {DEFAULT_MODEL}.")
    except anthropic.RateLimitError:
        fail("Rate limited by the Anthropic API. Wait a minute and try again.")
    except anthropic.APIStatusError as exc:
        fail(f"Anthropic API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError as exc:
        fail(f"Could not reach the Anthropic API: {exc}")

    if response.stop_reason == "refusal":
        fail("The model declined to analyse this video.")
    for block in response.content:
        if block.type == "tool_use" and block.name == "generate_moodboard":
            return dict(block.input)
    if response.stop_reason == "max_tokens":
        fail("The model ran out of output tokens before finishing the moodboard.")
    fail("The model answered without calling generate_moodboard. Try running again.")


# ---------------------------------------------------------------------------
# Result clean-up (the schema is strict, but counts and hex formats aren't)
# ---------------------------------------------------------------------------


def dominant_colours(frames: list[dict], k: int = 4) -> list[str]:
    """K-means palette from the frames, used only if the model's colours are unusable."""
    import cv2
    import numpy as np

    pixels = []
    for f in frames:
        small = cv2.resize(f["image"], (64, 64), interpolation=cv2.INTER_AREA)
        pixels.append(small.reshape(-1, 3))
    data = np.float32(np.vstack(pixels))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, centers = cv2.kmeans(data, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.flatten(), minlength=k)
    order = np.argsort(-counts)
    out = []
    for i in order:
        b, g, r = (int(v) for v in centers[i])
        out.append(f"#{r:02X}{g:02X}{b:02X}")
    return out


def clean_result(raw: dict, frames: list[dict]) -> dict:
    n = len(frames)

    indices = []
    for i in raw.get("selected_frame_indices") or []:
        if isinstance(i, int) and 0 <= i < n and i not in indices:
            indices.append(i)
    indices = indices[:4]
    if len(indices) < min(3, n):
        by_sharpness = sorted(range(n), key=lambda j: frames[j]["sharpness"], reverse=True)
        for j in by_sharpness:
            if j not in indices:
                indices.append(j)
            if len(indices) >= min(3, n):
                break

    palette = []
    for c in raw.get("color_palette") or []:
        m = HEX_RE.match(str(c.get("hex", "")).strip())
        if m:
            palette.append({"hex": "#" + m.group(1).upper(), "name": str(c.get("name", "")).strip()})
    palette = palette[:4]
    if len(palette) < 4:
        have = {p["hex"] for p in palette}
        for hx in dominant_colours(frames):
            if len(palette) >= 4:
                break
            if hx not in have:
                palette.append({"hex": hx, "name": ""})

    tags = [str(t).strip().lstrip("#") for t in raw.get("tags") or [] if str(t).strip()][:5]

    category = raw.get("primary_category")
    if category not in CATEGORIES:
        category = "Lifestyle"

    return {
        "title": str(raw.get("title") or "Untitled Moodboard").strip(),
        "primary_category": category,
        "sub_style": str(raw.get("sub_style") or "").strip(),
        "selected": [frames[i] for i in indices],
        "palette": palette,
        "tags": tags,
        "vibe": str(raw.get("vibe_description") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 4. HTML moodboard
# ---------------------------------------------------------------------------


def text_colour_for(hex_colour: str) -> str:
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111111" if luminance > 150 else "#F5F5F5"


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #0e0e10;
    --panel: #18181b;
    --line: #2a2a2f;
    --text: #ececef;
    --muted: #9a9aa3;
    --accent: {accent};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
  }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 40px 20px 64px; }}
  header {{ border-bottom: 1px solid var(--line); padding-bottom: 24px; margin-bottom: 28px; }}
  h1 {{ font-size: clamp(28px, 5vw, 44px); line-height: 1.1; margin: 0 0 14px; letter-spacing: -0.02em; }}
  .badges {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .badge {{
    font-size: 12px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
    padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted);
  }}
  .badge.primary {{ background: var(--accent); border-color: var(--accent); color: {accent_text}; }}
  .layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 28px; align-items: start; }}
  @media (max-width: 860px) {{ .layout {{ grid-template-columns: 1fr; }} }}
  .grid {{ column-count: 2; column-gap: 14px; }}
  @media (min-width: 1100px) {{ .grid {{ column-count: 3; }} }}
  @media (max-width: 520px) {{ .grid {{ column-gap: 10px; }} .tile {{ margin-bottom: 10px; border-radius: 10px; }} }}
  .tile {{
    break-inside: avoid; margin: 0 0 14px; border-radius: 14px; overflow: hidden;
    background: var(--panel); position: relative;
  }}
  .tile img {{ display: block; width: 100%; height: auto; transition: transform .35s ease; cursor: zoom-in; }}
  @media (hover: hover) {{ .tile:hover img {{ transform: scale(1.03); }} }}
  .tile span {{
    position: absolute; left: 10px; bottom: 10px; font-size: 11px; padding: 3px 8px;
    border-radius: 6px; background: rgba(0,0,0,.55); color: #fff;
  }}
  aside section {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 18px; margin-bottom: 16px; }}
  aside h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 12px; }}
  .swatches {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .swatch {{
    border-radius: 10px; height: 84px; padding: 10px; display: flex; flex-direction: column;
    justify-content: flex-end; cursor: pointer; border: 1px solid rgba(255,255,255,.06);
    font: 600 13px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
  }}
  .swatch small {{ font-size: 12px; font-weight: 400; opacity: .8; }}
  .tags {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .tag {{ font-size: 13px; padding: 5px 10px; border-radius: 8px; background: #232328; color: var(--text); }}
  .vibe {{ margin: 0; color: #cfcfd6; }}
  .lightbox {{
    position: fixed; inset: 0; background: rgba(0,0,0,.88); display: none;
    align-items: center; justify-content: center; padding: 24px; z-index: 10; cursor: zoom-out;
  }}
  .lightbox.open {{ display: flex; }}
  .lightbox img {{ max-width: 100%; max-height: 100%; border-radius: 12px; }}
  .toast {{
    position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #fff; color: #111;
    padding: 8px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none;
  }}
  .toast.show {{ opacity: 1; }}
  footer {{ margin-top: 32px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
  footer a {{ color: var(--muted); }}
  .nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }}
  .nav a, .nav button {{
    font-family: inherit; font-size: 13px; font-weight: 600; line-height: 1; color: var(--text); background: var(--panel);
    border: 1px solid var(--line); border-radius: 999px; padding: 9px 14px; text-decoration: none; cursor: pointer;
  }}
  .nav .grow {{ flex: 1; }}
</style>
</head>
<body>
<div class="wrap">
{nav}
  <header>
    <h1>{title}</h1>
    <div class="badges">
      <span class="badge primary">{category}</span>
      {sub_style_badge}
    </div>
  </header>
  <div class="layout">
    <main class="grid">
{tiles}
    </main>
    <aside>
      <section>
        <h2>Palette</h2>
        <div class="swatches">
{swatches}
        </div>
      </section>
      <section>
        <h2>Tags</h2>
        <div class="tags">{tags}</div>
      </section>
      <section>
        <h2>Vibe</h2>
        <p class="vibe">{vibe}</p>
      </section>
    </aside>
  </div>
  <footer>Made from {source}</footer>
</div>
<div class="lightbox" id="lightbox"><img alt=""></div>
<div class="toast" id="toast"></div>
<script>
  const box = document.getElementById('lightbox');
  document.querySelectorAll('.tile img').forEach(img => {{
    img.addEventListener('click', () => {{ box.querySelector('img').src = img.src; box.classList.add('open'); }});
  }});
  box.addEventListener('click', () => box.classList.remove('open'));
  document.addEventListener('keydown', e => {{ if (e.key === 'Escape') box.classList.remove('open'); }});
  const toast = document.getElementById('toast');
  document.querySelectorAll('.swatch').forEach(s => {{
    s.addEventListener('click', () => {{
      const hex = s.dataset.hex;
      const done = () => {{ toast.textContent = hex + ' copied'; toast.classList.add('show'); setTimeout(() => toast.classList.remove('show'), 1200); }};
      if (navigator.clipboard) navigator.clipboard.writeText(hex).then(done, done); else done();
    }});
  }});
</script>
</body>
</html>
"""


def build_html(board: dict, source: str, nav_html: str = "") -> str:
    """Render the moodboard page. nav_html is trusted markup the web app adds on top."""
    esc = html.escape
    tiles = "\n".join(
        f'      <figure class="tile"><img src="data:image/jpeg;base64,{f["b64"]}" '
        f'alt="Frame at {f["time"]:.1f}s"><span>{f["time"]:.1f}s</span></figure>'
        for f in board["selected"]
    )
    swatches = "\n".join(
        f'          <div class="swatch" data-hex="{p["hex"]}" title="Click to copy" '
        f'style="background:{p["hex"]};color:{text_colour_for(p["hex"])}">'
        f'{esc(p["name"]) + "<br>" if p["name"] else ""}<small>{p["hex"]}</small></div>'
        for p in board["palette"]
    )
    tags = "".join(f'<span class="tag">#{esc(t)}</span>' for t in board["tags"])
    sub_style_badge = f'<span class="badge">{esc(board["sub_style"])}</span>' if board["sub_style"] else ""
    accent = board["palette"][0]["hex"]
    return PAGE_TEMPLATE.format(
        title=esc(board["title"]),
        accent=accent,
        accent_text=text_colour_for(accent),
        category=esc(board["primary_category"]),
        sub_style_badge=sub_style_badge,
        tiles=tiles,
        swatches=swatches,
        tags=tags,
        vibe=esc(board["vibe"]),
        source=f'<a href="{esc(source)}">{esc(source)}</a>' if URL_RE.match(source) else esc(source),
        nav=nav_html,
    )


def create_moodboard(
    source: str,
    model: str = DEFAULT_MODEL,
    cookies_browser: str | None = None,
    cookie_file: str | None = None,
    progress=None,
) -> dict:
    """Run the whole pipeline. Returns the cleaned board dict (frames include "b64").

    progress, if given, is called with short status strings as work proceeds.
    Raises MoodboardError for anything the user should see.
    """
    _progress.reporter = progress
    try:
        with tempfile.TemporaryDirectory(prefix="reel2mood_") as workdir:
            video_path = resolve_source(source, workdir, cookies_browser, cookie_file)
            candidates = extract_candidates(video_path)

        log("Picking the sharpest frames")
        frames = keep_sharpest(candidates)
        for f in frames:
            f["b64"] = encode_jpeg_b64(f["image"])

        log("Asking Claude to read the look")
        raw = analyse_frames(frames, model)
        board = clean_result(raw, frames)
        log("Building the board")
        return board
    finally:
        _progress.reporter = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Turn a short video Reel into an HTML moodboard.")
    p.add_argument("source", help="Local video path or a Reel / TikTok / YouTube Short URL")
    p.add_argument("--out", default=".", help="Directory for moodboard.html (default: current directory)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model ID (default: {DEFAULT_MODEL})")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="Pass browser cookies to yt-dlp (chrome, firefox, ...)")
    p.add_argument("--cookies", metavar="FILE", help="Netscape-format cookies.txt for yt-dlp")
    p.add_argument("--no-open", action="store_true", help="Don't open the moodboard in a browser")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    check_dependencies(need_ytdlp=bool(URL_RE.match(args.source)))

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log("ANTHROPIC_API_KEY is not set; relying on any saved Anthropic login profile")

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "moodboard.html"

    board = create_moodboard(args.source, args.model, args.cookies_from_browser, args.cookies)

    out_path.write_text(build_html(board, args.source), encoding="utf-8")
    log(f"{board['primary_category']} / {board['sub_style']}")
    log(f"Moodboard written to {out_path.resolve()}")

    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    try:
        main()
    except MoodboardError as exc:
        print(f"\n[error] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[error] Interrupted.", file=sys.stderr)
        sys.exit(130)
