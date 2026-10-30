// Online moodboard.
//
// The board lives in the repository (board/board.json + board/shots/) and is
// served by GitHub Pages, so it's the same on every device. Reels are added
// on GitHub's servers: "Add reel" opens a pre-filled GitHub issue, and the
// Moodboard workflow (.github/workflows/moodboard.yml) downloads the reel,
// picks its hero shots with collage/pipeline.js, commits them and redeploys.
// Nothing needs to stay open on the phone while that happens.

import { OUTPUT_NAME, ROWS_PER_IMAGE, cropForAspect, renderCollage, tileAspect } from './layout.js';

// Owner/repo from the Pages address (owner.github.io/repo/...), so a fork works too.
const [OWNER, REPO] = (() => {
  const m = location.hostname.match(/^([^.]+)\.github\.io$/i);
  const repo = location.pathname.split('/').filter(Boolean)[0];
  return m && repo ? [m[1], repo] : ['geosubash-glitch', 'geosity'];
})();
const BOARD_URL = new URL('../board/', location.href);
const REFRESH_MS = 30_000;

const $ = (id) => document.getElementById(id);
let board = [];
let exportUrls = [];
let exportFiles = [];
let refreshTimer = null;

// ---------------------------------------------------------------------------
// GitHub hand-off
// ---------------------------------------------------------------------------

function newIssueUrl(title, body) {
  const u = new URL(`https://github.com/${OWNER}/${REPO}/issues/new`);
  u.searchParams.set('title', title);
  u.searchParams.set('body', body);
  return u.href;
}

function addReelUrl(links) {
  return newIssueUrl('Add reel', `${links.join('\n')}\n\nTap Submit. The board updates in a few minutes; you can close the app.`);
}

function uploadUrl() {
  return newIssueUrl('Add reel', 'Attach the video below (drag it in, or tap the paperclip), then tap Submit.\n\n');
}

function firstLinks(text) {
  return [...new Set((text || '').match(/https?:\/\/[^\s<>"')\]]+/gi) || [])];
}

$('addForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const links = firstLinks($('link').value);
  if (!links.length) {
    notice('Paste a reel link first (Instagram, TikTok or YouTube Shorts).', true);
    $('link').focus();
    return;
  }
  notice('');
  window.open(addReelUrl(links), '_blank', 'noopener');
  $('link').value = '';
  pendingSoon();
});

$('paste').addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    $('link').value = firstLinks(text)[0] || text.trim();
  } catch {
    $('link').focus();
  }
});
if (!navigator.clipboard || !navigator.clipboard.readText) $('paste').hidden = true;

$('upload').href = uploadUrl();

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

function notice(message, isError = false) {
  const el = $('notice');
  el.textContent = message;
  el.className = isError ? 'note err' : 'note';
  el.hidden = !message;
}

// Links and videos shared into the installed app (see sw.js).
function pickUpShared() {
  const params = new URLSearchParams(location.search);
  if (!params.has('link') && !params.has('video')) return;
  history.replaceState(null, '', location.pathname);
  if (params.has('link')) {
    $('link').value = params.get('link');
    notice('Got the link. Tap Add to board to send it.');
  } else {
    notice('To add a video file, tap "Upload a video instead" and attach it on GitHub.');
  }
}

// ---------------------------------------------------------------------------
// Board
// ---------------------------------------------------------------------------

function columnsChoice() {
  const v = document.querySelector('input[name=cols]:checked')?.value;
  return v === '2' || v === '3' ? Number(v) : null;
}

async function loadBoard() {
  // Pages caches files for a few minutes; ask for the newest copy.
  const res = await fetch(new URL(`board.json?t=${Date.now()}`, BOARD_URL), { cache: 'no-store' });
  if (res.status === 404) return [];
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return (data.shots || []).map((s) => ({ ...s, url: new URL(s.file, BOARD_URL).href }));
}

async function refreshBoard() {
  try {
    board = await loadBoard();
  } catch (err) {
    console.error(err);
    notice("Couldn't load the board. Check your connection and pull down to refresh.", true);
    return;
  }
  renderBoard();
}

function renderBoard() {
  const grid = $('board');
  grid.replaceChildren();
  const choice = columnsChoice();
  grid.style.columnCount = choice ? String(choice) : '';
  $('empty').hidden = board.length > 0;
  $('boardTools').hidden = board.length === 0;
  const reels = new Set(board.map((s) => s.reel)).size;
  $('count').textContent = board.length
    ? `${board.length} shot${board.length === 1 ? '' : 's'} from ${reels} reel${reels === 1 ? '' : 's'}`
    : '';

  board.forEach((shot, order) => {
    const [l, t, cw, ch] = cropForAspect(shot, tileAspect(shot, order));
    const tile = document.createElement('button');
    tile.type = 'button';
    tile.className = 'tile';
    tile.style.aspectRatio = `${cw} / ${ch}`;
    tile.setAttribute('aria-label', 'Open shot');
    const img = document.createElement('img');
    img.src = shot.url;
    img.alt = '';
    img.loading = 'lazy';
    // Show exactly the crop: size the frame relative to the crop and shift it.
    img.style.width = `${(shot.w / cw) * 100}%`;
    img.style.height = `${(shot.h / ch) * 100}%`;
    img.style.left = `${(-l / cw) * 100}%`;
    img.style.top = `${(-t / ch) * 100}%`;
    tile.append(img);
    tile.addEventListener('click', () => openLightbox(shot));
    grid.append(tile);
  });
}

document.querySelectorAll('input[name=cols]').forEach((r) => r.addEventListener('change', renderBoard));

let lightboxShot = null;

function openLightbox(shot) {
  lightboxShot = shot;
  $('lbImg').src = shot.url;
  $('lbSource').hidden = !/^https?:/.test(shot.source || '');
  $('lbSource').href = shot.source || '#';
  $('lbRemove').href = newIssueUrl(`Remove shot ${shot.id}`, 'Tap Submit to take this shot off the board.');
  $('lbRemoveReel').href = newIssueUrl(`Remove reel ${shot.reel}`, 'Tap Submit to take every shot from this reel off the board.');
  $('lightbox').hidden = false;
}

function closeLightbox() {
  $('lightbox').hidden = true;
  lightboxShot = null;
}

$('lightbox').addEventListener('click', (e) => { if (e.target === $('lightbox')) closeLightbox(); });
$('lbClose').addEventListener('click', closeLightbox);
for (const id of ['lbRemove', 'lbRemoveReel']) $(id).addEventListener('click', () => { closeLightbox(); pendingSoon(); });

// ---------------------------------------------------------------------------
// Reels GitHub is still working on (open "Add reel" issues)
// ---------------------------------------------------------------------------

async function refreshPending() {
  let issues;
  try {
    const res = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/issues?state=open&creator=${OWNER}&per_page=30`, {
      headers: { Accept: 'application/vnd.github+json' }, cache: 'no-store',
    });
    if (!res.ok) return; // rate limited or offline: just skip the status line
    issues = (await res.json()).filter((i) => !i.pull_request && /^(Add reel|Remove shot|Remove reel)/.test(i.title));
  } catch {
    return;
  }
  const list = $('pending');
  list.replaceChildren();
  for (const issue of issues) {
    const ageMin = (Date.now() - new Date(issue.created_at).getTime()) / 60000;
    // The worker replies when it's done; an open issue with a reply means it needs you.
    const stuck = issue.comments > 0;
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = issue.html_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = stuck ? 'Needs you: open to see why' : ageMin > 15 ? 'Taking longer than usual' : 'Working on it…';
    if (stuck) li.className = 'stuck';
    li.append(`${issue.title.startsWith('Remove reel') ? 'Removing a reel' : issue.title.startsWith('Remove') ? 'Removing a shot' : 'Adding a reel'} · `, a);
    list.append(li);
  }
  $('pendingBox').hidden = issues.length === 0;
  scheduleRefresh(issues.some((i) => i.comments === 0));
}

function scheduleRefresh(active) {
  clearTimeout(refreshTimer);
  if (active) refreshTimer = setTimeout(() => { refreshBoard(); refreshPending(); }, REFRESH_MS);
}

// After sending something to GitHub, check back a little sooner.
function pendingSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { refreshBoard(); refreshPending(); }, 8000);
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { refreshBoard(); refreshPending(); }
});

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

const fetchBlob = async (shot) => (await fetch(shot.url)).blob();

$('export').addEventListener('click', async () => {
  if (!board.length) return;
  const btn = $('export');
  btn.disabled = true;
  btn.textContent = 'Making image…';
  try {
    const columns = Math.min(columnsChoice() || (board.length <= 5 ? 2 : 3), board.length);
    // Phones can't draw one endless picture, so big boards become a few
    // images, split evenly so the last one isn't a lone leftover shot.
    const pageCount = Math.ceil(board.length / (columns * ROWS_PER_IMAGE));
    const perImage = Math.ceil(board.length / pageCount);
    const pages = [];
    for (let i = 0; i < board.length; i += perImage) pages.push(board.slice(i, i + perImage));

    for (const u of exportUrls) URL.revokeObjectURL(u);
    exportUrls = [];
    exportFiles = [];
    const list = $('exports');
    list.replaceChildren();
    for (let p = 0; p < pages.length; p++) {
      const blob = await renderCollage(pages[p], Math.min(columns, pages[p].length), fetchBlob);
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
    notice(`Couldn't make the image: ${err.message || err}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save as image';
  }
});

$('closeExport').addEventListener('click', () => { $('exportView').hidden = true; $('boardView').hidden = false; });
$('shareAll').addEventListener('click', async () => {
  try {
    await navigator.share({ files: exportFiles });
  } catch (err) {
    if (err.name !== 'AbortError') notice("Sharing didn't work here. Use the Save buttons instead.", true);
  }
});

// ---------------------------------------------------------------------------

if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
pickUpShared();
refreshBoard();
refreshPending();
