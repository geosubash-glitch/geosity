#!/usr/bin/env python3
# reel_to_moodboard.py
#
# Pulls the hero shots out of a short vertical video (Instagram Reel, TikTok,
# YouTube Short, or a local file) and stitches them into one tall,
# Pinterest-style masonry image: moodboard_scroll.jpg. No text, no swatches,
# just the best full-body / full-item frames.
#
# How frames are chosen:
#   1. PySceneDetect finds the shot cuts. Long single shots are split so a
#      one-take dance reel still gets several candidates.
#   2. Within each shot, several frames are sampled and the sharpest one
#      (highest Laplacian variance) is kept, which throws out motion blur.
#   3. Claude looks at the candidates, keeps the clean full-body/full-item
#      reveals and best angles, drops awkward expressions, transitions and
#      near-duplicates, ranks them, and marks where the subject is.
#   4. Each hero frame is cropped around the subject, so tile heights vary
#      naturally, then packed into a 2- or 3-column masonry strip.
#
# Install:
#   pip install anthropic opencv-python scenedetect yt-dlp numpy pillow
#
# Run:
#   export ANTHROPIC_API_KEY=sk-ant-...
#   python reel_to_moodboard.py path/to/reel.mp4
#   python reel_to_moodboard.py "https://www.instagram.com/reel/XXXXXXXX/"
#   python reel_to_moodboard.py clip.mp4 --columns 3 --max-frames 8 --out boards/
#   python reel_to_moodboard.py clip.mp4 --no-ai      # sharpness only, no API call
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
import importlib
import os
import re
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

DEFAULT_MODEL = "claude-sonnet-5-5"
OUTPUT_NAME = "moodboard_scroll.jpg"

MAX_CANDIDATES = 12      # frames shown to Claude
SAMPLES_PER_SHOT = 5     # frames tried per shot; the sharpest one survives
MIN_WINDOW_SEC = 0.6     # don't split shots shorter than this
DEFAULT_MAX_HEROES = 8
MIN_HEROES = 3
ANALYSIS_SIDE = 1024     # longest side of frames sent to Claude
# A candidate counts as blurry when its sharpness is below this share of the
# sharpest candidate. MIN_HEROES still wins, so short clips never run dry.
BLUR_RATIO = 0.25

# Collage look
GAP = 12                          # px between tiles and around the edge
BACKGROUND = (14, 14, 16)         # near-black, like a dark Pinterest feed
CORNER_RADIUS = 14
COLUMN_WIDTH = {2: 720, 3: 600}   # px; 2 cols -> 1476px wide, 3 cols -> 1848px wide
# Tile shapes (width / height). Each tile takes one that still holds the
# whole subject, rotating through them so neighbouring tiles differ in height.
TILE_ASPECTS = (0.45, 0.5, 0.5625, 0.6667, 0.75, 0.8)
# Without a subject box (--no-ai), assume the subject is in the middle 70% of
# the width and only ever trim the sides, never head or feet.
UNKNOWN_SUBJECT_BOX = (0.15, 0.0, 0.85, 1.0)
SUBJECT_PADDING = 0.08            # breathing room around the subject, share of its size
MIN_CROP_SHARE = 0.4              # crops never go below 40% of the frame height
JPEG_QUALITY = 92

# Models that accept the server-side refusal fallback parameter.
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

REQUIRED_MODULES = {
    # import name: pip package
    "cv2": "opencv-python",
    "numpy": "numpy",
    "scenedetect": "scenedetect",
    "PIL": "pillow",
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


def check_dependencies(need_ytdlp: bool, need_anthropic: bool) -> None:
    modules = dict(REQUIRED_MODULES)
    if need_ytdlp:
        modules["yt_dlp"] = "yt-dlp"
    if need_anthropic:
        modules["anthropic"] = "anthropic"
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
        # Best video-only MP4 first: no audio needed, and it avoids an ffmpeg
        # merge. The collage is built from these pixels, so take full quality.
        "format": "bv*[ext=mp4][height<=1920]/b[ext=mp4][height<=1920]/bv*[ext=mp4]/b[ext=mp4]/bv*/b",
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
# 2. Shot detection and sharp-frame picking
# ---------------------------------------------------------------------------


def shot_windows(video_path: str, total_frames: int, fps: float) -> list[tuple[int, int]]:
    """Frame ranges to pull one candidate from each: detected shots, with long
    shots split so a single-take video still yields several candidates."""
    from scenedetect import ContentDetector, detect

    log("Detecting scene cuts")
    try:
        scenes = detect(video_path, ContentDetector(threshold=27.0))
    except Exception as exc:  # scenedetect raises several backend-specific errors
        log(f"Scene detection failed ({exc}); sampling evenly instead")
        scenes = []
    def frame_no(tc) -> int:
        # scenedetect 0.7 renamed get_frames() to the frame_num property.
        return tc.frame_num if hasattr(tc, "frame_num") else tc.get_frames()

    windows = [(frame_no(s), frame_no(e)) for s, e in scenes if frame_no(e) > frame_no(s)]
    if not windows:
        windows = [(0, total_frames)]
    log(f"Found {len(scenes) or 1} shot(s)")

    min_len = max(2, int(MIN_WINDOW_SEC * fps))
    while len(windows) < MAX_CANDIDATES:
        longest = max(range(len(windows)), key=lambda i: windows[i][1] - windows[i][0])
        s, e = windows[longest]
        if e - s < 2 * min_len:
            break
        mid = (s + e) // 2
        windows[longest : longest + 1] = [(s, mid), (mid, e)]

    if len(windows) > MAX_CANDIDATES:
        # More cuts than we can show: keep the longest shots (they're the ones
        # that hold a pose), then put them back in timeline order.
        windows = sorted(sorted(windows, key=lambda w: w[1] - w[0], reverse=True)[:MAX_CANDIDATES])
    return windows


def resize_to_max_side(frame, max_side: int):
    import cv2

    h, w = frame.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return frame
    scale = max_side / longest
    return cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)


def sharpness(frame) -> float:
    """Laplacian variance, measured at a fixed size so every frame is judged alike."""
    import cv2

    gray = cv2.cvtColor(resize_to_max_side(frame, 720), cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def extract_candidates(video_path: str) -> list[dict]:
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        fail("OpenCV could not open the video (unsupported codec or corrupt file).")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if total <= 0:
        cap.release()
        fail("The video reports zero frames. Is it a valid video file?")

    windows = shot_windows(video_path, total, fps)
    log("Hunting for the sharpest frame in each shot")
    candidates = []
    for start, end in windows:
        # Sample the middle 70% of the shot: the edges are where cuts,
        # wipes and whip-pans live.
        lo = start + (end - start) * 0.15
        hi = start + (end - start) * 0.85
        count = min(SAMPLES_PER_SHOT, max(1, end - start))
        picks = sorted({int(lo + (hi - lo) * (i + 0.5) / count) for i in range(count)})
        best = None
        for idx in picks:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(idx, total - 1))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            score = sharpness(frame)
            if best is None or score > best["sharpness"]:
                best = {"frame_index": idx, "time": idx / fps, "image": frame, "sharpness": score}
        if best:
            candidates.append(best)
    cap.release()

    if not candidates:
        fail("Could not read any frames from the video.")
    log(f"Kept {len(candidates)} candidate frame(s)")
    return candidates


def drop_blurry(candidates: list[dict]) -> list[dict]:
    ranked = sorted(candidates, key=lambda c: c["sharpness"], reverse=True)
    best = ranked[0]["sharpness"] or 1.0
    kept = [c for c in ranked if c["sharpness"] >= best * BLUR_RATIO]
    if len(kept) < MIN_HEROES:
        kept = ranked[: min(MIN_HEROES, len(ranked))]
    if len(kept) < len(candidates):
        log(f"Dropped {len(candidates) - len(kept)} blurry frame(s)")
    # Back to timeline order so Claude reads the clip in sequence.
    return sorted(kept, key=lambda c: c["frame_index"])


def encode_jpeg_b64(frame, quality: int = 88) -> str:
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        fail("JPEG encoding failed.")
    return base64.b64encode(buf.tobytes()).decode("ascii")


# ---------------------------------------------------------------------------
# 3. Hero selection (Claude vision, or sharpness alone with --no-ai)
# ---------------------------------------------------------------------------

HERO_TOOL = {
    "name": "select_hero_frames",
    "description": "Record the hero frames chosen for the collage, best first. Call this exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["hero_frames"],
        "properties": {
            "hero_frames": {
                "type": "array",
                "description": "Chosen frames, best first.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["frame", "subject_box"],
                    "properties": {
                        "frame": {"type": "integer", "description": "Frame number as labelled."},
                        "subject_box": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "[x_min, y_min, x_max, y_max] on a 0-1000 scale of the frame's width and height, "
                                "tightly around the whole person head to toe (hair, hands and shoes included) "
                                "or around the whole featured item."
                            ),
                        },
                    },
                },
            }
        },
    },
}

SYSTEM_PROMPT = """You are a fashion photo editor pulling hero shots out of a short-form video for a visual moodboard.

You will see numbered frames from one Reel / TikTok / Short. Pick the frames worth printing and call the select_hero_frames tool once. Do not answer in plain text.

Keep frames that:
- show the full outfit or the full item clearly: try-on reveals, full-body poses, the best angle of a look;
- are crisp, with the subject in focus;
- catch a natural, composed expression and a deliberate pose.

Drop frames that have:
- motion blur, or a subject caught mid-movement;
- eyes mid-blink, a mouth mid-word, or an awkward, unflattering expression;
- transitions: wipes, fades, whip pans, cover cards, text-heavy frames;
- the subject cut off at the head or feet, or blocked by hands, phone or objects;
- the same outfit and pose as a better frame you already chose.

Order the frames best first. Aim for {target} frames; return fewer only if the clip doesn't have that many good ones, and never more than {max_heroes}.

For each frame, give subject_box around the person (head to toe) or the featured item, on a 0-1000 scale. The collage is cropped from this box, so include the whole subject and nothing you'd want cut."""


def ask_claude_for_heroes(frames: list[dict], model: str, max_heroes: int) -> list[dict]:
    import anthropic

    content = []
    for i, f in enumerate(frames):
        small = resize_to_max_side(f["image"], ANALYSIS_SIDE)
        content.append({"type": "text", "text": f"Frame {i} (t={f['time']:.1f}s)"})
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": encode_jpeg_b64(small)}}
        )
    content.append(
        {
            "type": "text",
            "text": f"Frame numbers run 0 to {len(frames) - 1}. Choose the hero frames with the select_hero_frames tool.",
        }
    )

    target = min(max_heroes, max(MIN_HEROES, len(frames) // 2 + 1))
    request = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT.format(target=target, max_heroes=max_heroes),
        tools=[HERO_TOOL],
        # Current models reject forced tool_choice; the strict schema plus the
        # prompt keeps the output on the tool, and we verify below.
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": content}],
    )

    client = anthropic.Anthropic()
    log("Asking Claude to pick the hero shots")
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
        if block.type == "tool_use" and block.name == "select_hero_frames":
            return clean_selection(block.input.get("hero_frames") or [], frames, max_heroes)
    if response.stop_reason == "max_tokens":
        fail("The model ran out of output tokens before choosing frames.")
    fail("The model answered without choosing frames. Try running again.")


def clean_selection(picks: list, frames: list[dict], max_heroes: int) -> list[dict]:
    """Validate Claude's picks: real frame numbers, no repeats, sane boxes."""
    heroes, seen = [], set()
    for p in picks:
        idx = p.get("frame") if isinstance(p, dict) else None
        if not isinstance(idx, int) or not 0 <= idx < len(frames) or idx in seen:
            continue
        seen.add(idx)
        box = normalise_box(p.get("subject_box"))
        heroes.append({**frames[idx], "box": box or UNKNOWN_SUBJECT_BOX, "box_known": box is not None})
        if len(heroes) == max_heroes:
            break
    if not heroes:
        log("Claude didn't return usable frames; falling back to the sharpest ones")
        return pick_by_sharpness(frames, max_heroes)
    log(f"Claude picked {len(heroes)} hero frame(s)")
    return heroes


def normalise_box(box) -> tuple[float, float, float, float] | None:
    """0-1000 ints -> 0-1 floats, or None if the box is unusable."""
    if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) for v in box):
        return None
    x0, y0, x1, y1 = (min(max(v / 1000.0, 0.0), 1.0) for v in box)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 < 0.05 or y1 - y0 < 0.05:
        return None
    return (x0, y0, x1, y1)


def pick_by_sharpness(frames: list[dict], max_heroes: int) -> list[dict]:
    """No-AI selection: sharpest first, skipping frames that look like a repeat."""
    import cv2
    import numpy as np

    def thumb(img):
        return cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (32, 56), interpolation=cv2.INTER_AREA).astype(np.float32)

    heroes, thumbs = [], []
    for f in sorted(frames, key=lambda c: c["sharpness"], reverse=True):
        t = thumb(f["image"])
        if any(float(np.mean(np.abs(t - other))) < 6.0 for other in thumbs):
            continue
        heroes.append({**f, "box": UNKNOWN_SUBJECT_BOX, "box_known": False})
        thumbs.append(t)
        if len(heroes) == max_heroes:
            break
    log(f"Picked {len(heroes)} frame(s) by sharpness")
    return heroes


# ---------------------------------------------------------------------------
# 4. Subject-aware cropping and masonry layout (Pillow)
# ---------------------------------------------------------------------------


def padded_subject(hero: dict) -> tuple[int, int, int, int, int, int]:
    """Subject box in pixels with breathing room, plus frame size: (x0, y0, x1, y1, W, H)."""
    H, W = hero["image"].shape[:2]
    bx0, by0, bx1, by1 = hero["box"]
    bw, bh = (bx1 - bx0) * W, (by1 - by0) * H
    cx, cy = (bx0 + bx1) / 2 * W, (by0 + by1) / 2 * H
    if hero.get("box_known", True):
        bw *= 1 + 2 * SUBJECT_PADDING
        bh *= 1 + 2 * SUBJECT_PADDING
    # Keep enough pixels that tiles never turn soft from upscaling.
    bh = max(bh, MIN_CROP_SHARE * H)
    bw = min(bw, W)
    bh = min(bh, H)
    x0 = min(max(cx - bw / 2, 0), W - bw)
    y0 = min(max(cy - bh / 2, 0), H - bh)
    return int(x0), int(y0), int(x0 + bw), int(y0 + bh), W, H


def aspect_range(hero: dict) -> tuple[float, float]:
    """Narrowest and widest tile shapes that still hold the whole subject."""
    x0, y0, x1, y1, W, H = padded_subject(hero)
    return (x1 - x0) / H, W / (y1 - y0)


def crop_for_aspect(hero: dict, aspect: float) -> tuple[int, int, int, int]:
    """Smallest crop of the given width/height ratio that contains the padded subject."""
    x0, y0, x1, y1, W, H = padded_subject(hero)
    lo, hi = aspect_range(hero)
    aspect = min(max(aspect, lo), hi)
    bw, bh = x1 - x0, y1 - y0
    ch = max(bh, bw / aspect)
    cw = ch * aspect
    cw, ch = min(cw, W), min(ch, H)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = min(max(cx - cw / 2, 0), W - cw)
    top = min(max(cy - ch / 2, 0), H - ch)
    return int(round(left)), int(round(top)), int(round(left + cw)), int(round(top + ch))


def tile_aspect(hero: dict, order: int) -> float:
    """Tile shape for the order-th tile: one of the standard shapes that holds
    the whole subject, rotating so neighbouring tiles get different heights.
    A head-to-toe pose only allows tall shapes; a close-up allows squarer ones."""
    lo, hi = aspect_range(hero)
    fits = [a for a in TILE_ASPECTS if lo <= a <= hi]
    if not fits:
        # The subject doesn't fit any standard shape; use the closest one it allows.
        return min(max(TILE_ASPECTS[0], lo), hi)
    # Stay among the three tallest that fit: they suit poses and keep the strip vertical.
    fits = fits[:3]
    return fits[(order * 2) % len(fits)] if len(fits) == 3 else fits[order % len(fits)]


def plan_layout(heroes: list[dict], columns: int) -> list[list[dict]]:
    """Greedy masonry: each tile drops into the currently shortest column, then
    the last tile of each column is reshaped so the columns end nearly level."""
    col_w = COLUMN_WIDTH[columns]
    cols: list[list[dict]] = [[] for _ in range(columns)]

    def height(col):
        return sum(t["h"] + GAP for t in col)

    for order, hero in enumerate(heroes):
        aspect = tile_aspect(hero, order)
        tile = {"hero": hero, "aspect": aspect, "h": round(col_w / aspect)}
        min(cols, key=height).append(tile)

    filled = [c for c in cols if c]
    if len(filled) > 1:
        # Even out the bottoms: spread each column's shortfall or overshoot
        # over its tiles (last first), keeping every crop around the subject.
        target = sum(height(c) for c in filled) / len(filled)
        for col in filled:
            delta = target - height(col)
            for tile in reversed(col):
                if abs(delta) < 1:
                    break
                lo, hi = aspect_range(tile["hero"])
                lo, hi = max(lo, 0.4), min(hi, 1.25)
                h_min, h_max = col_w / hi, col_w / lo
                new_h = min(max(tile["h"] + delta, h_min), h_max)
                delta -= new_h - tile["h"]
                tile["h"] = round(new_h)
                tile["aspect"] = col_w / tile["h"]
    return cols


def rounded_mask(size: tuple[int, int], radius: int):
    from PIL import Image, ImageDraw

    # Draw at 4x and shrink for smooth corners.
    big = Image.new("L", (size[0] * 4, size[1] * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=radius * 4, fill=255)
    return big.resize(size, Image.LANCZOS)


def render_collage(heroes: list[dict], columns: int):
    import cv2
    from PIL import Image

    cols = plan_layout(heroes, columns)
    col_w = COLUMN_WIDTH[columns]
    width = columns * col_w + (columns + 1) * GAP
    height = max(sum(t["h"] + GAP for t in col) for col in cols) + GAP
    canvas = Image.new("RGB", (width, height), BACKGROUND)

    for c, col in enumerate(cols):
        x, y = GAP + c * (col_w + GAP), GAP
        for tile in col:
            l, t, r, b = crop_for_aspect(tile["hero"], tile["aspect"])
            rgb = cv2.cvtColor(tile["hero"]["image"][t:b, l:r], cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb).resize((col_w, tile["h"]), Image.LANCZOS)
            canvas.paste(img, (x, y), rounded_mask(img.size, CORNER_RADIUS))
            y += tile["h"] + GAP
    return canvas


def choose_columns(count: int) -> int:
    return 2 if count <= 5 else 3


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def create_moodboard(
    source: str,
    model: str = DEFAULT_MODEL,
    use_ai: bool = True,
    max_heroes: int = DEFAULT_MAX_HEROES,
    columns: int | None = None,
    cookies_browser: str | None = None,
    cookie_file: str | None = None,
    progress=None,
):
    """Run the whole pipeline and return the collage as a PIL image.

    progress, if given, is called with short status strings as work proceeds.
    Raises MoodboardError for anything the user should see.
    """
    _progress.reporter = progress
    try:
        with tempfile.TemporaryDirectory(prefix="reel2mood_") as workdir:
            video_path = resolve_source(source, workdir, cookies_browser, cookie_file)
            candidates = extract_candidates(video_path)

        frames = drop_blurry(candidates)
        if use_ai:
            heroes = ask_claude_for_heroes(frames, model, max_heroes)
        else:
            heroes = pick_by_sharpness(frames, max_heroes)

        cols = columns or choose_columns(len(heroes))
        log(f"Stitching {len(heroes)} frame(s) into {cols} columns")
        return render_collage(heroes, cols)
    finally:
        _progress.reporter = None


def save_jpeg(image, path) -> None:
    image.save(path, "JPEG", quality=JPEG_QUALITY, subsampling=0, optimize=True, progressive=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Turn a short video into a Pinterest-style hero-shot collage.")
    p.add_argument("source", help="Local video path or a Reel / TikTok / YouTube Short URL")
    p.add_argument("--out", default=".", help=f"Directory for {OUTPUT_NAME} (default: current directory)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model ID (default: {DEFAULT_MODEL})")
    p.add_argument("--no-ai", action="store_true", help="Pick frames by sharpness alone; no API call")
    p.add_argument("--max-frames", type=int, default=DEFAULT_MAX_HEROES, help=f"Most hero frames to use (default: {DEFAULT_MAX_HEROES})")
    p.add_argument("--columns", type=int, choices=(2, 3), help="Masonry columns (default: 2 for up to 5 frames, else 3)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="Pass browser cookies to yt-dlp (chrome, firefox, ...)")
    p.add_argument("--cookies", metavar="FILE", help="Netscape-format cookies.txt for yt-dlp")
    p.add_argument("--no-open", action="store_true", help="Don't open the collage when done")
    args = p.parse_args()
    if args.max_frames < 1:
        p.error("--max-frames must be at least 1")
    return args


def main() -> None:
    args = parse_args()
    check_dependencies(need_ytdlp=bool(URL_RE.match(args.source)), need_anthropic=not args.no_ai)

    if not args.no_ai and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log("ANTHROPIC_API_KEY is not set; relying on any saved Anthropic login profile")

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / OUTPUT_NAME

    collage = create_moodboard(
        args.source,
        model=args.model,
        use_ai=not args.no_ai,
        max_heroes=args.max_frames,
        columns=args.columns,
        cookies_browser=args.cookies_from_browser,
        cookie_file=args.cookies,
    )
    save_jpeg(collage, out_path)
    log(f"Collage written to {out_path.resolve()} ({collage.width}x{collage.height})")

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
