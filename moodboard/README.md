# Moodboard

> **Looking for the free online board?** It lives in `../collage/`: <https://geosubash-glitch.github.io/geosity/collage/>. It needs no API key or server of your own, because GitHub processes the reels. This folder is an optional self-hosted server version.

Share a reel from Instagram (or TikTok / YouTube Shorts) and get back one tall, Pinterest-style image: `moodboard_scroll.jpg`. It holds only the hero shots (crisp full-body and full-outfit reveals, best angles, key poses) in a 2- or 3-column masonry layout with 12px dark gaps. No text, no swatches.

How the frames are chosen:
1. Every shot is sampled several times, and only the sharpest frame survives, which cuts out motion blur.
2. Claude (if you give it a key) keeps the clean reveals and drops:
   - awkward expressions
   - transitions
   - cut-off poses
   - repeats

   It also marks where the person or item is. Without a key, frames are picked by sharpness, with repeats removed.
3. Each frame is cropped around the subject, so head and feet stay in and tile heights vary like a real Pinterest board.

- `reel_to_moodboard.py`: the engine. Also works on its own from a terminal.
- `app.py`: the phone web app around it. It shows up in your share sheet.

## Set it up from your phone

You need a free Render account to run the server. A Claude API key is optional.

1. **Deploy.** Open this link: <https://render.com/deploy?repo=https://github.com/geosubash-glitch/geosity>
   - Sign in with GitHub.
   - It asks for `APP_PASSWORD`: pick any password. It keeps strangers off your server.
   - Tap **Apply** and wait for the first build (several minutes).
2. **Optional: let Claude pick the shots.** Create a key at [console.anthropic.com](https://console.anthropic.com) (this needs paid credit). Then add it in Render under *Environment* as `ANTHROPIC_API_KEY`.
3. **Open your app.** Render shows a link like `https://reel-moodboard-xxxx.onrender.com`. Open it and sign in with your password.
4. **Hook up the share button.** In the app, tap **Set up sharing from Instagram** and follow the steps for your phone:
   - Android: install it as an app.
   - iPhone: make a two-step Shortcut.

After that: Instagram → Share → *Share to…* / *More* → **Moodboard**.

## Good to know

- **Free plan sleeps.** If nobody has used it for a while, the first share takes about a minute while the server wakes up.
- **Boards can disappear** when the server restarts or redeploys on the free plan. Tap **Save** to keep the image on your phone. **Share** sends the image itself to Pinterest, Photos or a chat where your phone allows it.
- **Instagram sometimes blocks servers.** If you get a message saying Instagram wants a login, do one of these:
  - Upload the video instead: save it or screen-record it, then tap *Pick a video*.
  - Add your Instagram cookies as an `IG_COOKIES` setting in Render. This is a one-time job that needs a computer: export `cookies.txt` for instagram.com with a browser extension and paste in its contents.
- **Keep it working.** If downloads start failing after an Instagram change, open the service in Render and tap **Manual Deploy**. That pulls in the latest yt-dlp.

## Settings

| Name | Needed | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | no | Claude picks the shots; without it, sharpness does |
| `APP_PASSWORD` | strongly recommended | Password for the app |
| `SECRET_KEY` | set by Render | Signs the login cookie |
| `IG_COOKIES` | no | Instagram `cookies.txt` contents |
| `MOODBOARD_MODEL` | no | Claude model, default `claude-sonnet-5-5` |

## Run on a computer instead

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...            # optional
python app.py                                  # web app on http://localhost:8000
python reel_to_moodboard.py reel.mp4           # or one collage straight from the terminal
python reel_to_moodboard.py reel.mp4 --columns 3 --max-frames 8
python reel_to_moodboard.py reel.mp4 --no-ai   # sharpness only, no API key needed
```
