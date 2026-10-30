#!/usr/bin/env node
// Moodboard worker. Runs on GitHub Actions (.github/workflows/moodboard.yml)
// when the repo owner opens an "Add reel", "Remove shot" or "Remove reel" issue.
//
//   node process.mjs add      reel links (or attached videos) from $TEXT
//   node process.mjs remove   the shot or reel named in $TITLE ("Remove shot <id>" / "Remove reel <id>")
//   node process.mjs add --file clip.webm [--file ...]   local test, no download
//
// For each reel: download it (yt-dlp, or straight from a GitHub attachment),
// convert it to WebM (Playwright's Chromium can't play H.264), run
// collage/pipeline.js on it in a headless browser, and add the shots to
// board/ (board.json + shots/*.jpg). The workflow commits board/ and
// redeploys GitHub Pages; collage/ shows the board.
//
// Writes a Markdown summary to $SUMMARY_FILE for the issue comment. Exit code
// 0 when the board changed, 1 when nothing could be added or removed.

import { chromium } from 'playwright';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs/promises';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

const run = promisify(execFile);
const REPO_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const BOARD_DIR = process.env.BOARD_DIR ? path.resolve(process.env.BOARD_DIR) : path.join(REPO_ROOT, 'board'); // override for tests
const BOARD_JSON = path.join(BOARD_DIR, 'board.json');
const VIDEO_EXT = /\.(mp4|mov|m4v|webm|mkv|3gp)(\?|$)/i;
const LINK_RE = /https?:\/\/[^\s<>"')\]]+/gi;
const MAX_REELS_PER_REQUEST = 10;

const summary = [];
const say = (line) => { summary.push(line); console.log(line); };

async function main() {
  const [mode, ...rest] = process.argv.slice(2);
  const board = await loadBoard();
  let changed = false;
  if (mode === 'remove') {
    changed = await removeShots(board, process.env.TITLE || '');
  } else if (mode === 'add') {
    const files = [];
    for (let i = 0; i < rest.length; i++) if (rest[i] === '--file') files.push(rest[++i]);
    changed = await addReels(board, files);
  } else {
    say('Unknown request.');
  }
  if (changed) await saveBoard(board);
  if (process.env.SUMMARY_FILE) await fs.writeFile(process.env.SUMMARY_FILE, summary.join('\n') + '\n');
  process.exitCode = changed ? 0 : 1;
}

// ---------------------------------------------------------------------------
// Board file
// ---------------------------------------------------------------------------

async function loadBoard() {
  try {
    return JSON.parse(await fs.readFile(BOARD_JSON, 'utf8'));
  } catch {
    return { shots: [] };
  }
}

async function saveBoard(board) {
  board.updated = new Date().toISOString();
  await fs.mkdir(BOARD_DIR, { recursive: true });
  await fs.writeFile(BOARD_JSON, JSON.stringify(board, null, 1) + '\n');
}

const sameSource = (a, b) => normaliseLink(a) === normaliseLink(b);

function normaliseLink(link) {
  try {
    const u = new URL(link);
    return `${u.hostname.replace(/^www\./, '')}${u.pathname.replace(/\/+$/, '')}`;
  } catch {
    return link;
  }
}

async function removeShots(board, title) {
  // "Remove shot <id>" takes one shot off; "Remove reel <id>" takes every shot from that reel.
  const m = /^Remove (shot|reel)\s+(\S+)/i.exec(title.trim());
  const [kind, id] = m ? [m[1].toLowerCase(), m[2]] : ['shot', ''];
  const gone = board.shots.filter((s) => (kind === 'reel' ? s.reel === id : s.id === id));
  if (!gone.length) {
    say(`Couldn't find that ${kind} on the board. It may already be gone.`);
    return false;
  }
  board.shots = board.shots.filter((s) => !gone.includes(s));
  for (const s of gone) await fs.rm(path.join(BOARD_DIR, s.file), { force: true });
  say(kind === 'reel' ? `Removed that reel (${gone.length} shot${gone.length === 1 ? '' : 's'}) from the board.` : 'Removed that shot from the board.');
  return true;
}

// ---------------------------------------------------------------------------
// Adding reels
// ---------------------------------------------------------------------------

async function addReels(board, localFiles) {
  const links = [...new Set((process.env.TEXT || '').match(LINK_RE) || [])]
    .map((l) => l.replace(/[.,;:!?]+$/, ''))
    .slice(0, MAX_REELS_PER_REQUEST);
  const sources = localFiles.length ? localFiles.map((f) => ({ file: path.resolve(f), label: path.basename(f) }))
    : links.map((link) => ({ link, label: link }));
  if (!sources.length) {
    say('There was no link in that. Paste a reel link, or attach the video in a comment here.');
    return false;
  }

  const work = await fs.mkdtemp(path.join(os.tmpdir(), 'moodboard-'));
  const browser = await openWorker();
  let added = 0;
  try {
    for (const [i, src] of sources.entries()) {
      if (src.link && board.shots.some((s) => s.source && sameSource(s.source, src.link))) {
        say(`- ${src.label}: already on the board.`);
        continue;
      }
      try {
        const original = src.file || await download(src.link, path.join(work, `in-${i}`));
        const webm = path.join(work, `reel-${i}.webm`);
        await toWebm(original, webm);
        const shots = await browser.process(webm);
        const reelId = `${process.env.ISSUE_NUMBER || 'local'}-${Date.now().toString(36)}`;
        const addedAt = new Date().toISOString();
        await fs.mkdir(path.join(BOARD_DIR, 'shots'), { recursive: true });
        const fresh = [];
        for (const [rank, s] of shots.entries()) {
          const id = `${reelId}-${rank}`;
          const file = `shots/${id}.jpg`;
          await fs.writeFile(path.join(BOARD_DIR, file), Buffer.from(s.jpeg, 'base64'));
          fresh.push({
            id, file, w: s.w, h: s.h,
            box: s.box.map((v) => Math.round(v * 1000) / 1000), boxKnown: s.boxKnown,
            reel: reelId, rank, source: src.link || src.label, issue: Number(process.env.ISSUE_NUMBER) || null, addedAt,
          });
        }
        board.shots = [...fresh, ...board.shots]; // newest reel on top
        added += fresh.length;
        say(`- ${src.label}: added ${fresh.length} shot${fresh.length === 1 ? '' : 's'}.`);
      } catch (err) {
        console.error(err);
        say(`- ${src.label}: ${err.userMessage || err.message || err}`);
      }
    }
  } finally {
    await browser.close();
    await fs.rm(work, { recursive: true, force: true });
  }
  if (added) say(`\nThe board updates in a minute or two.`);
  return added > 0;
}

function friendly(message) {
  const err = new Error(message);
  err.userMessage = message;
  return err;
}

async function download(link, base) {
  const host = new URL(link).hostname;
  const isGitHub = /(^|\.)github\.com$|githubusercontent\.com$/.test(host);
  if (isGitHub || VIDEO_EXT.test(link)) {
    // Attachments and direct video files: plain download.
    const headers = isGitHub && process.env.GITHUB_TOKEN ? { Authorization: `Bearer ${process.env.GITHUB_TOKEN}` } : {};
    const res = await fetch(link, { headers, redirect: 'follow' });
    if (!res.ok) throw friendly(`couldn't download that file (HTTP ${res.status}).`);
    const out = `${base}.video`;
    await fs.writeFile(out, Buffer.from(await res.arrayBuffer()));
    return out;
  }
  const args = [
    '-f', 'bv*[height<=1920][ext=mp4]/b[height<=1920][ext=mp4]/bv*/b',
    '-o', `${base}.%(ext)s`, '--no-playlist', '--quiet', '--no-warnings', '--socket-timeout', '30',
  ];
  if (process.env.IG_COOKIES && process.env.IG_COOKIES.trim()) {
    const cookies = path.join(path.dirname(base), 'cookies.txt');
    await fs.writeFile(cookies, process.env.IG_COOKIES.trim() + '\n');
    args.push('--cookies', cookies);
  }
  try {
    await run('yt-dlp', [...args, link], { timeout: 180_000 });
  } catch (err) {
    const text = String(err.stderr || err.message).replace(/\x1b\[[0-9;]*m/g, '').replace(/^ERROR: /gm, '').trim();
    if (/login|cookies|rate.?limit|not available|private|sign in|confirm you/i.test(text)) {
      throw friendly(
        "the site wouldn't hand the video to GitHub's servers (it wants a login). " +
        'Reply here with the video attached (drag it into a comment), and it will be added from that.'
      );
    }
    throw friendly(`couldn't download it: ${text.split('\n').pop()}`);
  }
  const dir = path.dirname(base);
  const name = (await fs.readdir(dir)).find((f) => f.startsWith(path.basename(base) + '.'));
  if (!name) throw friendly('the download finished but no video came out.');
  return path.join(dir, name);
}

async function toWebm(input, output) {
  try {
    await run('ffmpeg', [
      '-y', '-loglevel', 'error', '-i', input, '-an',
      '-vf', 'scale=w=1280:h=1280:force_original_aspect_ratio=decrease:force_divisible_by=2',
      '-c:v', 'libvpx-vp9', '-b:v', '3M', '-deadline', 'realtime', '-cpu-used', '8', output,
    ], { timeout: 600_000 });
  } catch (err) {
    throw friendly(`that file doesn't look like a video ffmpeg can read (${String(err.stderr || err.message).trim().split('\n').pop()}).`);
  }
}

// ---------------------------------------------------------------------------
// Headless browser running collage/pipeline.js
// ---------------------------------------------------------------------------

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.bin': 'application/octet-stream', '.css': 'text/css' };

async function openWorker() {
  const collage = path.join(REPO_ROOT, 'collage');
  const server = http.createServer(async (req, res) => {
    const file = path.normalize(path.join(collage, decodeURIComponent(new URL(req.url, 'http://x').pathname)));
    if (!file.startsWith(collage + path.sep)) { res.writeHead(403).end(); return; }
    try {
      const body = await fs.readFile(file);
      res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' }).end(body);
    } catch {
      res.writeHead(404).end();
    }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on('console', (m) => { if (/^\[step\]|^Picked/.test(m.text())) console.log(m.text()); });
  page.on('pageerror', (e) => console.error('[page]', e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/worker.html`);
  await page.waitForFunction(() => window.workerReady === true);

  return {
    async process(video) {
      await page.setInputFiles('#f', video);
      const out = await page.evaluate(() => window.processPicked());
      if (out.error) throw friendly(out.error);
      return out.shots;
    },
    async close() {
      await browser.close();
      server.close();
    },
  };
}

main().catch((err) => {
  console.error(err);
  summary.push(`Something went wrong on the server: ${err.message || err}`);
  if (process.env.SUMMARY_FILE) fs.writeFile(process.env.SUMMARY_FILE, summary.join('\n') + '\n').finally(() => process.exit(1));
  else process.exit(1);
});
