// Reel Collage - runs entirely in the browser.
//
// Keeps one growing moodboard on the phone. Every reel you add contributes its
// hero shots; the board is saved between visits and can be exported as one
// tall Pinterest-style image (moodboard_scroll.jpg). Nothing is uploaded:
// frames are read with a <video> element, judged with on-device models (Human:
// MoveNet body pose, BlazeFace + FaceMesh for blinks and mouths, an expression
// model), stored in IndexedDB, and stitched on a canvas.
//
// Picking, per reel:
//   1. Scan the clip, find cuts by frame difference, and score every sample's
//      sharpness (Laplacian variance) so motion-blurred frames lose.
//   2. Take the sharpest frame from the middle of each shot (shot edges are
//      where transitions live); long single takes are split into windows.
//   3. Score those candidates: full body in frame, eyes open, mouth not caught
//      mid-word, no grimace. Drop near-duplicates, keep the best 3-8.
//   4. Save each with the box around the person, so crops keep head and feet.

import { Human } from './vendor/human/human.esm.js';

const OUTPUT_NAME = 'moodboard_scroll.jpg';
const MAX_CANDIDATES = 12;
const MAX_HEROES = 8;
const MIN_HEROES = 3;
const MIN_WINDOW_SEC = 0.6;
const BLUR_RATIO = 0.25;           // under 25% of the sharpest frame's score counts as blurry
const DUPLICATE_DIFF = 20;         // coarse-thumbnail difference below which two frames are the same shot
const SHARPNESS_SIDE = 480;        // frames are judged at this size so scores compare fairly
const ANALYSIS_SIDE = 720;         // size handed to the pose/face models
const STORED_SIDE = 1280;          // saved shots keep this much resolution
const STORED_QUALITY = 0.9;

const GAP = 12;
const BACKGROUND = '#0e0e10';
const CORNER_RADIUS = 14;
const COLUMN_WIDTH = { 1: 900, 2: 720, 3: 600 };
const TILE_ASPECTS = [0.45, 0.5, 0.5625, 0.6667, 0.75, 0.8];
const UNKNOWN_SUBJECT_BOX = [0.15, 0, 0.85, 1];   // no person found: only trim the sides
const SUBJECT_PADDING = 0.08;
const MIN_CROP_SHARE = 0.4;
const MAX_CANVAS_PIXELS = 16_000_000;              // iOS Safari refuses canvases much bigger
const ROWS_PER_IMAGE = 6;                          // export splits bigger boards across images
const EXACT_LAYOUT_MAX = 8;                        // try every arrangement up to this many tiles
const JPEG_QUALITY = 0.92;

const NEGATIVE_EXPRESSIONS = new Set(['angry', 'disgust', 'fear', 'sad']);

// ---------------------------------------------------------------------------
// Board storage (IndexedDB, on this phone only)
// ---------------------------------------------------------------------------

const db = (() => {
  let opening = null;
  const open = () => opening || (opening = new Promise((resolve, reject) => {
    const req = indexedDB.open('reel-collage', 1);
    req.onupgradeneeded = () => {
      const d = req.result;
      d.createObjectStore('shots', { keyPath: 'id' });
      d.createObjectStore('reels', { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
  const run = async (store, mode, fn) => {
    const d = await open();
    return new Promise((resolve, reject) => {
      const tx = d.transaction(store, mode);
      const result = fn(tx.objectStore(store));
      tx.oncomplete = () => resolve(result && 'result' in result ? result.result : result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error('Storage is full'));
    });
  };
  return {
    all: (store) => run(store, 'readonly', (s) => s.getAll()),
    put: (store, value) => run(store, 'readwrite', (s) => s.put(value)),
    remove: (store, id) => run(store, 'readwrite', (s) => s.delete(id)),
    clear: async () => { await run('shots', 'readwrite', (s) => s.clear()); await run('reels', 'readwrite', (s) => s.clear()); },
  };
})();

function newId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

// Shots in board order: newest reel first, best shot of each reel first.
async function loadBoard() {
  const shots = await db.all('shots');
  return shots.sort((a, b) => b.reelAddedAt - a.reelAddedAt || a.rank - b.rank);
}

// ---------------------------------------------------------------------------
// Page wiring
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
let busy = false;
let progress = { index: 0, count: 1 };
let exportUrls = [];
let tileUrls = [];

function setStep(text, fraction) {
  const { index, count } = progress;
  $('step').textContent = count > 1 ? `Reel ${index + 1} of ${count}: ${text}` : text;
  if (fraction != null) {
    const overall = (index + Math.min(Math.max(fraction, 0), 1)) / count;
    $('bar').style.width = `${Math.round(overall * 100)}%`;
  }
}

function notice(message, isError = false) {
  const el = $('notice');
  el.textContent = message;
  el.className = isError ? 'note err' : 'note';
  el.hidden = !message;
}

function columnsChoice() {
  const v = document.querySelector('input[name=cols]:checked')?.value;
  return v === '2' || v === '3' ? Number(v) : null;
}

$('file').addEventListener('change', (e) => {
  const files = [...(e.target.files || [])];
  e.target.value = '';
  if (files.length) addReels(files);
});

document.querySelectorAll('input[name=cols]').forEach((r) => r.addEventListener('change', () => renderBoard()));
$('export').addEventListener('click', () => exportBoard());
$('closeExport').addEventListener('click', () => { $('exportView').hidden = true; $('boardView').hidden = false; });
$('shareAll').addEventListener('click', () => shareExports());
$('clear').addEventListener('click', async () => {
  if (!confirm('Remove every shot from your board? This can’t be undone.')) return;
  await db.clear();
  notice('');
  renderBoard();
});
$('lightbox').addEventListener('click', (e) => { if (e.target === $('lightbox')) closeLightbox(); });
$('lbClose').addEventListener('click', () => closeLightbox());

async function pickUpSharedVideos() {
  const params = new URLSearchParams(location.search);
  if (!params.has('shared') && !params.has('link')) return;
  history.replaceState(null, '', location.pathname);
  if (params.has('link')) {
    notice(
      "That was a link, not a video. This free version can't download from Instagram, " +
      'so save or screen-record the reel first, then add that video here.', true
    );
    return;
  }
  try {
    const cache = await caches.open('reel-collage-share');
    const files = [];
    for (const req of await cache.keys()) {
      const res = await cache.match(req);
      const blob = await res.blob();
      const name = decodeURIComponent(res.headers.get('x-name') || 'shared-video');
      files.push(new File([blob], name, { type: blob.type || 'video/mp4' }));
      await cache.delete(req);
    }
    if (!files.length) throw new Error('missing');
    addReels(files);
  } catch {
    notice('The shared video got lost on the way in. Add it again with the button above.', true);
  }
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
renderBoard().then(pickUpSharedVideos);

// ---------------------------------------------------------------------------
// Adding reels
// ---------------------------------------------------------------------------

let modelsPromise = null;
const getModels = () => modelsPromise || (modelsPromise = loadModels());

async function addReels(files) {
  if (busy) { notice('Still working on the last batch. Add these once it finishes.', true); return; }
  busy = true;
  notice('');
  $('work').hidden = false;
  $('addBtn').classList.add('disabled');
  const known = new Set((await db.all('reels')).map((r) => r.fingerprint));
  const problems = [];
  let added = 0, skipped = 0;
  for (let i = 0; i < files.length; i++) {
    progress = { index: i, count: files.length };
    const file = files[i];
    const fingerprint = `${file.name}|${file.size}`;
    if (known.has(fingerprint)) { skipped++; continue; }
    try {
      added += await addReel(file, fingerprint);
      known.add(fingerprint);
      await renderBoard();
    } catch (err) {
      console.error(err);
      problems.push(`${file.name}: ${err.userMessage || err.message || err}`);
    }
  }
  busy = false;
  $('work').hidden = true;
  $('addBtn').classList.remove('disabled');
  keepStorage();
  const parts = [];
  if (added) parts.push(`Added ${added} shot${added === 1 ? '' : 's'} to your board.`);
  if (skipped) parts.push(`${skipped} reel${skipped === 1 ? ' was' : 's were'} already on it.`);
  if (problems.length) parts.push(`Couldn't use:\n${problems.join('\n')}`);
  notice(parts.join(' '), problems.length > 0 && !added);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function addReel(file, fingerprint) {
  setStep('Opening the video', 0.02);
  let video;
  try {
    video = await openVideo(file);
    const human = getModels(); // loads while we scan

    const samples = await scanVideo(video);
    const candidates = pickCandidates(samples, video.duration);

    let heroes;
    const models = await human;
    if (models) {
      heroes = await judgeCandidates(video, candidates, models);
    } else {
      setStep('Picking the sharpest shots', 0.8);
      heroes = pickBySharpness(candidates);
    }
    if (!heroes.length) throw userError("Couldn't find any usable frames in that video.");
    console.info('Picked', JSON.stringify(heroes.map((h) => ({
      t: +h.time.toFixed(2), score: +(h.score ?? 0).toFixed(2), framing: h.framing, blink: h.blink, mouth: h.mouthOpen, box: h.boxKnown,
    }))));

    setStep('Saving to your board', 0.94);
    const reelId = newId();
    const reelAddedAt = Date.now();
    const [w, h] = fitSize(video.videoWidth, video.videoHeight, STORED_SIDE);
    const ctx = canvasFor(w, h).getContext('2d');
    for (let rank = 0; rank < heroes.length; rank++) {
      const hero = heroes[rank];
      await seek(video, hero.time);
      ctx.drawImage(video, 0, 0, w, h);
      const blob = await new Promise((r) => ctx.canvas.toBlob(r, 'image/jpeg', STORED_QUALITY));
      if (!blob) throw userError("Couldn't save frames from that video.");
      await db.put('shots', { id: newId(), reelId, reelAddedAt, rank, blob, w, h, box: hero.box, boxKnown: hero.boxKnown, time: hero.time });
    }
    await db.put('reels', { id: reelId, name: file.name, fingerprint, addedAt: reelAddedAt, count: heroes.length });
    return heroes.length;
  } finally {
    if (video) {
      URL.revokeObjectURL(video.src);
      video.remove();
    }
  }
}

// Ask the browser not to clear the board when space runs low.
async function keepStorage() {
  try { if (navigator.storage && navigator.storage.persist) await navigator.storage.persist(); } catch { /* best effort */ }
}

function userError(message) {
  const err = new Error(message);
  err.userMessage = message;
  return err;
}

// ---------------------------------------------------------------------------
// Video access
// ---------------------------------------------------------------------------

async function openVideo(file) {
  if (file.type && !file.type.startsWith('video/')) {
    throw userError("That file isn't a video. Choose a video from your gallery.");
  }
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.preload = 'auto';
  // Some phones only decode frames for videos that are on the page.
  video.className = 'offscreen';
  document.body.append(video);
  video.src = URL.createObjectURL(file);

  await new Promise((resolve, reject) => {
    video.onloadeddata = resolve;
    video.onerror = () => reject(userError(
      "This phone can't read that video. Try another file, or a screen recording of the reel."
    ));
  });
  // Nudge the decoder awake (needed on some iPhones before seeking works).
  try { await video.play(); } catch { /* muted autoplay can still be refused; seeking works anyway */ }
  video.pause();

  if (!Number.isFinite(video.duration)) {
    // Files from some recorders don't state their length until you seek past the end.
    await new Promise((resolve) => {
      video.ontimeupdate = () => { video.ontimeupdate = null; resolve(); };
      video.currentTime = 1e9;
    });
  }
  if (!video.duration || !video.videoWidth) throw userError("That video seems to be empty.");
  return video;
}

function seek(video, t) {
  const target = Math.min(Math.max(t, 0), Math.max(video.duration - 0.05, 0));
  return new Promise((resolve) => {
    let finished = false;
    const done = () => {
      if (finished) return;
      finished = true;
      video.removeEventListener('seeked', onSeeked);
      resolve();
    };
    const onSeeked = () => {
      // Wait for the new frame to actually be ready to draw where the browser can tell us.
      if ('requestVideoFrameCallback' in video) {
        video.requestVideoFrameCallback(done);
        setTimeout(done, 150);
      } else {
        done();
      }
    };
    video.addEventListener('seeked', onSeeked);
    setTimeout(done, 3000); // never hang on a stubborn decoder
    video.currentTime = target;
  });
}

function canvasFor(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  return c;
}

function fitSize(w, h, longest) {
  const s = Math.min(1, longest / Math.max(w, h));
  return [Math.round(w * s), Math.round(h * s)];
}

// ---------------------------------------------------------------------------
// 1-2. Scan, find cuts, pick sharp candidates
// ---------------------------------------------------------------------------

function grayOf(ctx, w, h) {
  const { data } = ctx.getImageData(0, 0, w, h);
  const g = new Float32Array(w * h);
  for (let i = 0, j = 0; i < g.length; i++, j += 4) g[i] = 0.299 * data[j] + 0.587 * data[j + 1] + 0.114 * data[j + 2];
  return g;
}

function laplacianVariance(g, w, h) {
  let sum = 0, sumSq = 0, n = 0;
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1, i = y * w + 1; x < w - 1; x++, i++) {
      const l = g[i - w] + g[i + w] + g[i - 1] + g[i + 1] - 4 * g[i];
      sum += l; sumSq += l * l; n++;
    }
  }
  const mean = sum / n;
  return sumSq / n - mean * mean;
}

async function scanVideo(video) {
  const duration = video.duration;
  const rate = Math.min(8, Math.max(2, 240 / duration)); // samples per second, ~240 max
  const count = Math.max(4, Math.floor(duration * rate));
  const [sw, sh] = fitSize(video.videoWidth, video.videoHeight, SHARPNESS_SIDE);
  const big = canvasFor(sw, sh).getContext('2d', { willReadFrequently: true });
  const tw = 32, th = Math.max(8, Math.round(32 * sh / sw));
  const small = canvasFor(tw, th).getContext('2d', { willReadFrequently: true });
  // A very coarse copy for spotting repeats: ignores small zooms and pans.
  const cw = 8, ch = Math.max(2, Math.round(8 * sh / sw));
  const tiny = canvasFor(cw, ch).getContext('2d', { willReadFrequently: true });

  const samples = [];
  for (let i = 0; i < count; i++) {
    const t = ((i + 0.5) / count) * duration;
    await seek(video, t);
    big.drawImage(video, 0, 0, sw, sh);
    small.drawImage(big.canvas, 0, 0, tw, th);
    tiny.drawImage(small.canvas, 0, 0, cw, ch);
    samples.push({
      time: t,
      sharpness: laplacianVariance(grayOf(big, sw, sh), sw, sh),
      thumb: grayOf(small, tw, th),
      coarse: grayOf(tiny, cw, ch),
    });
    if (i % 4 === 0) setStep('Finding the sharpest moments', 0.05 + 0.5 * (i / count));
  }
  return samples;
}

function meanAbsDiff(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]);
  return s / a.length;
}

function median(values) {
  const v = [...values].sort((x, y) => x - y);
  return v.length ? v[Math.floor(v.length / 2)] : 0;
}

function pickCandidates(samples, duration) {
  // Cuts: a jump in picture well above the clip's normal frame-to-frame change.
  const diffs = samples.map((s, i) => (i ? meanAbsDiff(s.thumb, samples[i - 1].thumb) : 0));
  const typical = median(diffs.slice(1));
  const threshold = Math.max(18, typical * 4);
  let windows = [];
  let start = 0;
  for (let i = 1; i < samples.length; i++) {
    if (diffs[i] > threshold) { windows.push([start, i]); start = i; }
  }
  windows.push([start, samples.length]);

  // Split long takes so a one-shot dance video still offers several moments.
  const perSec = samples.length / duration;
  const minLen = Math.max(2, Math.ceil(MIN_WINDOW_SEC * perSec));
  while (windows.length < MAX_CANDIDATES) {
    let longest = 0;
    windows.forEach((w, i) => { if (w[1] - w[0] > windows[longest][1] - windows[longest][0]) longest = i; });
    const [a, b] = windows[longest];
    if (b - a < 2 * minLen) break;
    const mid = Math.floor((a + b) / 2);
    windows.splice(longest, 1, [a, mid], [mid, b]);
  }
  if (windows.length > MAX_CANDIDATES) {
    windows = windows.sort((x, y) => (y[1] - y[0]) - (x[1] - x[0])).slice(0, MAX_CANDIDATES).sort((x, y) => x[0] - y[0]);
  }

  const candidates = [];
  for (const [a, b] of windows) {
    // Middle 70% of the shot: the edges are where wipes, fades and whip-pans live.
    let lo = a + Math.floor((b - a) * 0.15);
    let hi = Math.max(lo + 1, b - Math.floor((b - a) * 0.15));
    let best = null;
    for (let i = lo; i < hi; i++) if (!best || samples[i].sharpness > best.sharpness) best = samples[i];
    if (best) candidates.push(best);
  }

  // Drop the blurry ones, but never below a handful.
  const sharpest = Math.max(...candidates.map((c) => c.sharpness), 1);
  const ranked = [...candidates].sort((x, y) => y.sharpness - x.sharpness);
  let kept = ranked.filter((c) => c.sharpness >= sharpest * BLUR_RATIO);
  if (kept.length < MIN_HEROES) kept = ranked.slice(0, MIN_HEROES);
  return kept.sort((x, y) => x.time - y.time);
}

// ---------------------------------------------------------------------------
// 3. Judge poses and faces on the phone
// ---------------------------------------------------------------------------

async function loadModels() {
  try {
    const human = new Human({
      modelBasePath: new URL('./vendor/human/models/', import.meta.url).href,
      debug: false,
      warmup: 'none',
      cacheSensitivity: 0, // every frame is a different photo; never reuse results
      filter: { enabled: false },
      face: {
        enabled: true,
        detector: { rotation: false, maxDetected: 3, minConfidence: 0.3, skipFrames: 0, skipTime: 0 },
        mesh: { enabled: true },
        attention: { enabled: false },
        iris: { enabled: false },
        emotion: { enabled: true, skipFrames: 0, skipTime: 0 },
        description: { enabled: false },
        antispoof: { enabled: false },
        liveness: { enabled: false },
      },
      body: { enabled: true, modelPath: 'movenet-lightning.json', maxDetected: 1, minConfidence: 0.2, skipFrames: 0, skipTime: 0 },
      hand: { enabled: false },
      object: { enabled: false },
      segmentation: { enabled: false },
      gesture: { enabled: true },
    });
    await human.load();
    return human;
  } catch (err) {
    console.warn('Pose/face models unavailable, using sharpness only', err);
    return null;
  }
}

function judgeFrame(result) {
  const out = { framing: 0, box: null, hasFace: false, blink: false, mouthOpen: 0, badExpression: false };

  const body = (result.body || []).reduce((a, b) => (!a || b.score > a.score ? b : a), null);
  if (body && body.score >= 0.25) {
    const kp = {};
    for (const k of body.keypoints) if (k.score >= 0.3) kp[k.part] = k.positionRaw;
    const has = (p) => p in kp;
    const head = has('nose') || has('leftEye') || has('rightEye');
    const feet = has('leftAnkle') || has('rightAnkle');
    const hips = has('leftHip') || has('rightHip');
    out.framing = head && feet ? 1 : hips ? 0.55 : 0.3;

    const pts = Object.values(kp);
    if (pts.length >= 5) {
      let x0 = Math.min(...pts.map((p) => p[0])), x1 = Math.max(...pts.map((p) => p[0]));
      let y0 = Math.min(...pts.map((p) => p[1])), y1 = Math.max(...pts.map((p) => p[1]));
      const w = x1 - x0, h = y1 - y0;
      // Keypoints stop at the eyes and ankles: stretch to cover hair, hands and shoes.
      y0 -= head ? h * 0.12 : 0;
      y1 += feet ? h * 0.07 : h * 0.12;
      x0 -= w * 0.2;
      x1 += w * 0.2;
      out.box = [x0, y0, x1, y1].map((v) => Math.min(Math.max(v, 0), 1));
    }
  }

  const faces = result.face || [];
  let fi = -1;
  faces.forEach((f, i) => { if (fi < 0 || f.score > faces[fi].score) fi = i; });
  if (fi >= 0) {
    const face = faces[fi];
    out.hasFace = true;
    for (const g of result.gesture || []) {
      if (g.face !== fi) continue;
      if (/blink/.test(g.gesture)) out.blink = true;
      const m = /mouth (\d+)% open/.exec(g.gesture);
      if (m) out.mouthOpen = Number(m[1]);
    }
    const top = (face.emotion || []).reduce((a, b) => (!a || b.score > a.score ? b : a), null);
    out.badExpression = !!top && NEGATIVE_EXPRESSIONS.has(top.emotion) && top.score > 0.5;
    if (!out.box) {
      // Only a face: frame head and shoulders.
      const [fx, fy, fw, fh] = face.boxRaw;
      out.box = [fx - fw * 0.8, fy - fh * 0.5, fx + fw * 1.8, fy + fh * 3.2].map((v) => Math.min(Math.max(v, 0), 1));
    }
  }
  if (out.box && (out.box[2] - out.box[0] < 0.05 || out.box[3] - out.box[1] < 0.05)) out.box = null;
  return out;
}

async function judgeCandidates(video, candidates, human) {
  const [aw, ah] = fitSize(video.videoWidth, video.videoHeight, ANALYSIS_SIDE);
  const ctx = canvasFor(aw, ah).getContext('2d', { willReadFrequently: true });
  const sharpest = Math.max(...candidates.map((c) => c.sharpness), 1);

  const judged = [];
  for (let i = 0; i < candidates.length; i++) {
    setStep(`Checking poses and faces (${i + 1} of ${candidates.length})`, 0.58 + 0.32 * (i / candidates.length));
    const c = candidates[i];
    await seek(video, c.time);
    ctx.drawImage(video, 0, 0, aw, ah);
    let verdict;
    try {
      verdict = judgeFrame(await human.detect(ctx.canvas));
    } catch (err) {
      console.warn('detect failed', err);
      verdict = judgeFrame({});
    }
    let score = 0.45 * (c.sharpness / sharpest) + 0.45 * verdict.framing + (verdict.hasFace ? 0.1 : 0);
    if (verdict.blink) score -= 0.35;
    if (verdict.mouthOpen > 35) score -= 0.2;
    if (verdict.badExpression) score -= 0.15;
    judged.push({ ...c, ...verdict, score });
  }
  return chooseHeroes(judged);
}

function chooseHeroes(judged) {
  const ranked = [...judged].sort((a, b) => b.score - a.score);
  const best = ranked[0]?.score ?? 0;
  const heroes = [];
  for (const c of ranked) {
    if (heroes.length >= MAX_HEROES) break;
    if (heroes.length >= MIN_HEROES && c.score < best * 0.5) break;
    // Same shot again (in testing, repeats measured 0-13 and different shots 70+).
    if (heroes.some((h) => meanAbsDiff(h.coarse, c.coarse) < DUPLICATE_DIFF)) continue;
    heroes.push({ ...c, boxKnown: !!c.box, box: c.box || UNKNOWN_SUBJECT_BOX });
  }
  return heroes;
}

function pickBySharpness(candidates) {
  return chooseHeroes(candidates.map((c) => ({ ...c, box: null, score: c.sharpness })));
}


// ---------------------------------------------------------------------------
// 4. Subject-aware crops and masonry layout
// Every shot carries its own frame size (w, h) and subject box (0-1).
// ---------------------------------------------------------------------------

function paddedSubject(shot) {
  const W = shot.w, H = shot.h;
  const [bx0, by0, bx1, by1] = shot.box;
  let bw = (bx1 - bx0) * W, bh = (by1 - by0) * H;
  const cx = ((bx0 + bx1) / 2) * W, cy = ((by0 + by1) / 2) * H;
  if (shot.boxKnown) { bw *= 1 + 2 * SUBJECT_PADDING; bh *= 1 + 2 * SUBJECT_PADDING; }
  bh = Math.max(bh, MIN_CROP_SHARE * H); // never crop so tight the tile turns soft
  bw = Math.min(bw, W);
  bh = Math.min(bh, H);
  const x0 = Math.min(Math.max(cx - bw / 2, 0), W - bw);
  const y0 = Math.min(Math.max(cy - bh / 2, 0), H - bh);
  return { x0, y0, x1: x0 + bw, y1: y0 + bh };
}

function aspectRange(shot) {
  const s = paddedSubject(shot);
  return [(s.x1 - s.x0) / shot.h, shot.w / (s.y1 - s.y0)];
}

function cropForAspect(shot, aspect) {
  const W = shot.w, H = shot.h;
  const s = paddedSubject(shot);
  const [lo, hi] = aspectRange(shot);
  aspect = Math.min(Math.max(aspect, lo), hi);
  const bw = s.x1 - s.x0, bh = s.y1 - s.y0;
  let ch = Math.max(bh, bw / aspect);
  let cw = ch * aspect;
  cw = Math.min(cw, W); ch = Math.min(ch, H);
  const cx = (s.x0 + s.x1) / 2, cy = (s.y0 + s.y1) / 2;
  const left = Math.min(Math.max(cx - cw / 2, 0), W - cw);
  const top = Math.min(Math.max(cy - ch / 2, 0), H - ch);
  return [left, top, cw, ch];
}

function tileAspect(shot, order) {
  // Rotate through the tall shapes that still hold the whole subject, so
  // neighbouring tiles differ in height instead of forming a flat grid.
  const [lo, hi] = aspectRange(shot);
  let fits = TILE_ASPECTS.filter((a) => a >= lo && a <= hi);
  if (!fits.length) return Math.min(Math.max(TILE_ASPECTS[0], lo), hi);
  fits = fits.slice(0, 3);
  return fits.length === 3 ? fits[(order * 2) % 3] : fits[order % fits.length];
}

function planLayout(shots, columns) {
  const colW = COLUMN_WIDTH[columns];
  // Each tile has a natural height (from its rotated shape) and a range it can
  // stretch or shrink to while still holding the whole subject.
  const tiles = shots.map((shot, order) => {
    const aspect = tileAspect(shot, order);
    const [lo, hi] = aspectRange(shot);
    const hMin = colW / Math.min(hi, 1.25);
    const hMax = colW / Math.max(lo, 0.4);
    const h = Math.min(Math.max(Math.round(colW / aspect), hMin), hMax);
    return { shot, h, hMin, hMax };
  });
  const n = tiles.length;
  const sum = (col, key) => col.reduce((s, i) => s + tiles[i][key] + GAP, 0);

  // Deal tiles into columns. Up to EXACT_LAYOUT_MAX tiles, every arrangement is
  // tried. Bigger boards place the rest shortest-column-first and only try
  // every arrangement of the last few, which is what decides the bottom edge.
  // The winner lets all columns end at the same height, keeps the best-ranked
  // shots near the top, and reshapes tiles the least.
  const tail = Math.min(n, n <= EXACT_LAYOUT_MAX ? n : 6);
  const base = Array.from({ length: columns }, () => []);
  for (let i = 0; i < n - tail; i++) base.reduce((a, b) => (sum(b, 'h') < sum(a, 'h') ? b : a)).push(i);

  let best = null;
  const assign = new Array(tail).fill(0);
  const evaluate = () => {
    const cols = base.map((c) => [...c]);
    assign.forEach((c, k) => cols[c].push(n - tail + k));
    if (cols.some((c) => !c.length)) return;
    const lowT = Math.max(...cols.map((c) => sum(c, 'hMin')));
    const highT = Math.min(...cols.map((c) => sum(c, 'hMax')));
    const noms = cols.map((c) => sum(c, 'h'));
    const mean = noms.reduce((a, b) => a + b, 0) / columns;
    const t = Math.min(Math.max(mean, lowT), Math.max(highT, lowT));
    let rowCost = 0;
    cols.forEach((c) => c.forEach((tileIdx, row) => { rowCost += (n - tileIdx) * row; }));
    const cost = [Math.round(Math.max(0, lowT - highT)), rowCost, noms.reduce((s, v) => s + Math.abs(v - t), 0)];
    if (!best || cost[0] < best.cost[0] || (cost[0] === best.cost[0] && (cost[1] < best.cost[1] || (cost[1] === best.cost[1] && cost[2] < best.cost[2])))) {
      best = { cost, cols, t };
    }
  };
  const search = (k) => {
    if (k === tail) { evaluate(); return; }
    // On a fresh board the best shot starts the first column; columns are otherwise interchangeable.
    const limit = k === 0 && tail === n ? 1 : columns;
    for (let c = 0; c < limit; c++) { assign[k] = c; search(k + 1); }
  };
  search(0);
  const picked = best.cols;
  const target = best.t;

  // Reshape tiles (last in each column first) so every column ends at the target.
  const cols = picked.map((col) => col.map((i) => ({ ...tiles[i] })));
  for (const col of cols) {
    let delta = target - col.reduce((s, t) => s + t.h + GAP, 0);
    for (let i = col.length - 1; i >= 0 && Math.abs(delta) >= 1; i--) {
      const tile = col[i];
      const newH = Math.min(Math.max(tile.h + delta, tile.hMin), tile.hMax);
      delta -= newH - tile.h;
      tile.h = Math.round(newH);
    }
    for (const tile of col) tile.aspect = colW / tile.h;
  }
  return { cols, colW };
}

function roundedRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

async function renderCollage(shots, columns) {
  const { cols, colW } = planLayout(shots, columns);
  const width = cols.length * colW + (cols.length + 1) * GAP;
  const height = Math.max(...cols.map((c) => c.reduce((s, t) => s + t.h + GAP, 0))) + GAP;
  // Very tall images get scaled down to stay inside what phones can draw.
  const scale = Math.min(1, Math.sqrt(MAX_CANVAS_PIXELS / (width * height)));

  const canvas = canvasFor(width * scale, height * scale);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.fillStyle = BACKGROUND;
  ctx.fillRect(0, 0, width, height);

  for (let c = 0; c < cols.length; c++) {
    let y = GAP;
    const x = GAP + c * (colW + GAP);
    for (const tile of cols[c]) {
      const bitmap = await createImageBitmap(tile.shot.blob);
      // Stored frames may be smaller than the original; crop in their own pixels.
      const [sx, sy, sw, sh] = cropForAspect({ ...tile.shot, w: bitmap.width, h: bitmap.height }, tile.aspect);
      ctx.save();
      roundedRect(ctx, x, y, colW, tile.h, CORNER_RADIUS);
      ctx.clip();
      ctx.drawImage(bitmap, sx, sy, sw, sh, x, y, colW, tile.h);
      ctx.restore();
      bitmap.close && bitmap.close();
      y += tile.h + GAP;
    }
  }
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(userError("Couldn't make the image. Try 2 columns."))), 'image/jpeg', JPEG_QUALITY)
  );
}

// ---------------------------------------------------------------------------
// Board view
// ---------------------------------------------------------------------------

async function renderBoard() {
  let shots;
  try {
    shots = await loadBoard();
  } catch (err) {
    console.error(err);
    notice("This browser won't let the page save anything, so the board can't be kept. Try Chrome or Safari outside private mode.", true);
    return;
  }
  for (const u of tileUrls) URL.revokeObjectURL(u);
  tileUrls = [];

  const grid = $('board');
  grid.replaceChildren();
  const choice = columnsChoice();
  grid.style.columnCount = choice ? String(choice) : '';
  $('empty').hidden = shots.length > 0;
  $('boardTools').hidden = shots.length === 0;
  const reelCount = new Set(shots.map((s) => s.reelId)).size;
  $('count').textContent = shots.length
    ? `${shots.length} shot${shots.length === 1 ? '' : 's'} from ${reelCount} reel${reelCount === 1 ? '' : 's'}`
    : '';

  shots.forEach((shot, order) => {
    const aspect = tileAspect(shot, order);
    const [l, t, cw, ch] = cropForAspect(shot, aspect);
    const url = URL.createObjectURL(shot.blob);
    tileUrls.push(url);
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'tile';
    tile.style.aspectRatio = `${cw} / ${ch}`;
    tile.setAttribute('aria-label', 'Open shot');
    const img = document.createElement('img');
    img.src = url;
    img.alt = '';
    img.loading = 'lazy';
    // Show exactly the crop: size the frame relative to the crop and shift it.
    img.style.width = `${(shot.w / cw) * 100}%`;
    img.style.height = `${(shot.h / ch) * 100}%`;
    img.style.left = `${(-l / cw) * 100}%`;
    img.style.top = `${(-t / ch) * 100}%`;
    tile.append(img);
    tile.addEventListener('click', () => openLightbox(shot, url));
    grid.append(tile);
  });
  showStorageHint(shots.length);
}

function showStorageHint(count) {
  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
  const installed = window.navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  $('iosHint').hidden = !(isIOS && !installed && count > 0);
}

let lightboxShot = null;

function openLightbox(shot, url) {
  lightboxShot = shot;
  $('lbImg').src = url;
  $('lightbox').hidden = false;
}

function closeLightbox() {
  $('lightbox').hidden = true;
  lightboxShot = null;
}

$('lbRemove').addEventListener('click', async () => {
  if (!lightboxShot) return;
  const shot = lightboxShot;
  closeLightbox();
  await db.remove('shots', shot.id);
  // Forget the reel too once its last shot is gone, so it can be added again.
  const rest = (await db.all('shots')).filter((s) => s.reelId === shot.reelId);
  if (!rest.length) await db.remove('reels', shot.reelId);
  renderBoard();
});

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

let exportFiles = [];

async function exportBoard() {
  const shots = await loadBoard();
  if (!shots.length) return;
  const btn = $('export');
  btn.disabled = true;
  btn.textContent = 'Making image…';
  try {
    const columns = Math.min(columnsChoice() || (shots.length <= 5 ? 2 : 3), shots.length);
    // Phones can't draw one endless picture, so big boards become a few images.
    // Split evenly so the last part isn't a lone leftover shot.
    const pageCount = Math.ceil(shots.length / (columns * ROWS_PER_IMAGE));
    const perImage = Math.ceil(shots.length / pageCount);
    const pages = [];
    for (let i = 0; i < shots.length; i += perImage) pages.push(shots.slice(i, i + perImage));

    for (const u of exportUrls) URL.revokeObjectURL(u);
    exportUrls = [];
    exportFiles = [];
    const list = $('exports');
    list.replaceChildren();
    for (let p = 0; p < pages.length; p++) {
      const blob = await renderCollage(pages[p], Math.min(columns, pages[p].length));
      const name = pages.length === 1 ? OUTPUT_NAME : OUTPUT_NAME.replace('.jpg', `_${p + 1}.jpg`);
      exportFiles.push(new File([blob], name, { type: 'image/jpeg' }));
      const url = URL.createObjectURL(blob);
      exportUrls.push(url);
      const item = document.createElement('div');
      item.className = 'export';
      const save = document.createElement('a');
      save.className = 'btn';
      save.href = url;
      save.download = name;
      save.textContent = pages.length === 1 ? 'Save image' : `Save part ${p + 1} of ${pages.length}`;
      const img = document.createElement('img');
      img.src = url;
      img.alt = `Moodboard${pages.length > 1 ? `, part ${p + 1}` : ''}`;
      item.append(save, img);
      list.append(item);
    }
    $('shareAll').hidden = !(navigator.canShare && navigator.canShare({ files: exportFiles }));
    $('boardView').hidden = true;
    $('exportView').hidden = false;
    window.scrollTo(0, 0);
  } catch (err) {
    console.error(err);
    notice(err.userMessage || `Couldn't make the image: ${err.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save as image';
  }
}

async function shareExports() {
  try {
    await navigator.share({ files: exportFiles });
  } catch (err) {
    if (err.name !== 'AbortError') notice("Sharing didn't work here. Use the Save buttons instead.", true);
  }
}
