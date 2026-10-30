// Masonry layout and image export for the moodboard.
// Every shot carries its own frame size (w, h) and subject box (0-1), so crops
// keep the whole person whatever video it came from.

export const OUTPUT_NAME = 'moodboard_scroll.jpg';
const GAP = 12;
const BACKGROUND = '#0e0e10';
const CORNER_RADIUS = 14;
const COLUMN_WIDTH = { 1: 900, 2: 720, 3: 600 };
const TILE_ASPECTS = [0.45, 0.5, 0.5625, 0.6667, 0.75, 0.8];
const SUBJECT_PADDING = 0.08;
const MIN_CROP_SHARE = 0.4;
const MAX_CANVAS_PIXELS = 16_000_000;              // iOS Safari refuses canvases much bigger
export const ROWS_PER_IMAGE = 6;                   // export splits bigger boards across images
const EXACT_LAYOUT_MAX = 8;                        // try every arrangement up to this many tiles
const JPEG_QUALITY = 0.92;

function canvasFor(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  return c;
}


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

export function cropForAspect(shot, aspect) {
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

export function tileAspect(shot, order) {
  // Rotate through the tall shapes that still hold the whole subject, so
  // neighbouring tiles differ in height instead of forming a flat grid.
  const [lo, hi] = aspectRange(shot);
  let fits = TILE_ASPECTS.filter((a) => a >= lo && a <= hi);
  if (!fits.length) return Math.min(Math.max(TILE_ASPECTS[0], lo), hi);
  fits = fits.slice(0, 3);
  return fits.length === 3 ? fits[(order * 2) % 3] : fits[order % fits.length];
}

export function planLayout(shots, columns) {
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

export async function renderCollage(shots, columns, loadBlob) {
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
      const bitmap = await createImageBitmap(await loadBlob(tile.shot));
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
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Couldn't make the image. Try 2 columns."))), 'image/jpeg', JPEG_QUALITY)
  );
}

