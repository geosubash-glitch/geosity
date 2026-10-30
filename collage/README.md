# Moodboard

An online moodboard that grows with every reel you add. It's free to run, needs no API key, and you can close your phone while it works.

**Open it:** <https://geosubash-glitch.github.io/geosity/collage/>

## Using it

1. In Instagram, tap **Share** on a reel, then **Copy link**. On Android, once the page is on your home screen, you can share the link straight to **Moodboard** instead.
2. In the app, paste it and tap **Add to board**. GitHub opens with an "Add reel" issue already filled in; tap **Submit**.
3. Close the app if you like. GitHub's servers process the reel, and its best shots appear on the board a few minutes later. The app shows reels still in progress, and any that need you.
4. **Save as image** exports the board as `moodboard_scroll.jpg`. A big board is split evenly across a few images, because phones can't draw one endless picture.
5. Tap a shot to see the original reel, remove the shot, or remove the whole reel.

**If a link won't download** (Instagram often refuses servers that aren't logged in), the worker replies on the issue. Reply with the video attached, or use **Upload a video instead**. Optional: store your Instagram `cookies.txt` contents as a repository secret named `IG_COOKIES`, and logged-in downloads will work.

## How it works

- `index.html`, `app.js`: the board page. It reads `../board/board.json`, lays the shots out with `layout.js`, and hands new reels to GitHub as pre-filled issues.
- `.github/workflows/moodboard.yml` runs on your "Add reel" / "Remove shot" / "Remove reel" issues and comments. Anyone else's are ignored. It runs `tools/moodboard/process.mjs`, which:
  1. downloads the reel (yt-dlp, or the attached file) and converts it to WebM;
  2. opens `worker.html` in a headless browser, which runs `pipeline.js`:
     - finds the cuts;
     - keeps the sharpest mid-shot frame of each shot;
     - checks each frame with on-device models (Human 3.3.6, MIT, vendored): full body in frame, eyes open, mouth not caught mid-word, no grimace;
     - drops repeats and keeps the best 3–8;
  3. writes the shots to `board/`, commits, and redeploys GitHub Pages.
- **Layout:** crops keep the whole person, tile heights stagger, and the columns are arranged so they end level.

## Good to know

- **The board is public**, like the rest of this site, and so are the "Add reel" issues with their links.
- **Each reel adds about 1 MB to the repository.** GitHub Pages sites are limited to 1 GB, which is several hundred reels.
- **Speed:** processing runs on the free GitHub runner's processor, so a 30-second reel takes a few minutes from Submit to showing up.
