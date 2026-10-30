// Reel processing: picks the hero shots out of one video.
//
// Used in two places with the same code: the GitHub Actions worker
// (worker.html, driven by tools/moodboard/process.mjs) and any browser that
// imports it. Frames are read with a <video> element and judged with
// on-device models (Human: MoveNet body pose, BlazeFace + FaceMesh for blinks
// and mouths, an expression model). Nothing is sent anywhere.
//
// Picking:
//   1. Scan the clip, find cuts by frame difference, and score every sample's
//      sharpness (Laplacian variance) so motion-blurred frames lose.
//   2. Take the sharpest frame from the middle of each shot (shot edges are
//      where transitions live); long single takes are split into windows.
//   3. Score those candidates: full body in frame, eyes open, mouth not caught
//      mid-word, no grimace. Drop near-duplicates, keep the best 3-8.
//   4. Return each as a JPEG with the box around the person, so later crops
//      keep head and feet.

import { Human } from './vendor/human/human.esm.js';

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
const UNKNOWN_SUBJECT_BOX = [0.15, 0, 0.85, 1];   // no person found: only trim the sides

const NEGATIVE_EXPRESSIONS = new Set(['angry', 'disgust', 'fear', 'sad']);

let report = () => {};
function setStep(text, fraction) {
  report(text, fraction);
}

export function userError(message) {
  const err = new Error(message);
  err.userMessage = message;
  return err;
}

let modelsPromise = null;
const getModels = () => modelsPromise || (modelsPromise = loadModels());

// Returns the hero shots of one video file, best first:
// [{ blob, w, h, box, boxKnown, time, score }]
export async function processVideo(file, onStep = () => {}) {
  report = onStep;
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

    setStep('Saving the shots', 0.94);
    const [w, h] = fitSize(video.videoWidth, video.videoHeight, STORED_SIDE);
    const ctx = canvasFor(w, h).getContext('2d');
    const shots = [];
    for (const hero of heroes) {
      await seek(video, hero.time);
      ctx.drawImage(video, 0, 0, w, h);
      const blob = await new Promise((r) => ctx.canvas.toBlob(r, 'image/jpeg', STORED_QUALITY));
      if (!blob) throw userError("Couldn't save frames from that video.");
      shots.push({ blob, w, h, box: hero.box, boxKnown: hero.boxKnown, time: hero.time, score: hero.score ?? 0 });
    }
    return shots;
  } finally {
    report = () => {};
    if (video) {
      URL.revokeObjectURL(video.src);
      video.remove();
    }
  }
}

export function canvasFor(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  return c;
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
