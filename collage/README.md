# Reel Collage

Turns a short video into one tall Pinterest-style image of its best shots (`moodboard_scroll.jpg`). It's free, runs entirely in the phone's browser, and uploads nothing.

**Use it:** <https://geosubash-glitch.github.io/geosity/collage/>

## How it picks shots

1. Scans the video, finds the cuts, and scores every moment for sharpness, so motion-blurred frames lose.
2. Takes the sharpest frame from the middle of each shot. Transitions happen at the edges of shots, so they're skipped.
3. Runs small on-device models on each candidate:
   - MoveNet for body pose: is the whole body in frame?
   - FaceMesh for eyes and mouth: eyes open, mouth not caught mid-word?
   - An expression model: no grimace?
4. Drops repeats and keeps the best 3–8 shots.
5. Crops each shot around the person so head and feet stay in. It varies the tile heights and packs them into 2–3 columns with 12px dark gaps, picking the arrangement that lets the columns end level.

## Files

- `index.html`, `app.js`: the page and everything it does.
- `sw.js`: works offline after the first visit. It also receives videos shared from the Android gallery once the page has been added to the home screen.
- `vendor/human/`: [Human](https://github.com/vladmandic/human) 3.3.6 (MIT) and the four models it needs, about 10 MB, served from this site.

## Limits

- **Links can't be downloaded.** Fetching videos from a link needs a server, and Instagram blocks browsers from doing it directly. Save or screen-record the reel first. The server version in `../moodboard/` can take links if you ever host it.
- **The models judge visible things only:** sharpness, framing, open eyes and mouth, expression. They can't judge taste the way a person (or Claude) would.
