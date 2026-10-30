# Moodboard

Share a reel from Instagram (or TikTok / YouTube Shorts) and get a moodboard back: the sharpest frames, a colour palette, style tags and a short description of the vibe.

- `reel_to_moodboard.py`: the engine. Also works on its own from a terminal.
- `app.py`: the phone web app around it. It shows up in your share sheet.

## Set it up from your phone

You need two things: a Claude API key, and a free Render account to run the server.

1. **Get an API key.** Go to [console.anthropic.com](https://console.anthropic.com), open *API Keys*, and create one. Add a few dollars of credit under *Billing*. Copy the key (it starts with `sk-ant-`).
2. **Deploy.** Open this link: <https://render.com/deploy?repo=https://github.com/geosubash-glitch/geosity>
   - Sign in with GitHub.
   - It asks for two values:
     - `ANTHROPIC_API_KEY`: paste your key.
     - `APP_PASSWORD`: pick any password. It stops strangers from using your credits.
   - Tap **Apply** and wait for the first build (several minutes).
3. **Open your app.** Render shows a link like `https://reel-moodboard-xxxx.onrender.com`. Open it and sign in with your password.
4. **Hook up the share button.** In the app, tap **Set up sharing from Instagram** and follow the steps for your phone:
   - Android: install it as an app.
   - iPhone: make a two-step Shortcut.

After that: Instagram → Share → *Share to…* / *More* → **Moodboard**.

## Good to know

- **Free plan sleeps.** If nobody has used it for a while, the first share takes about a minute while the server wakes up.
- **Boards can disappear** when the server restarts or redeploys on the free plan. Tap **Save** on a board to keep a copy on your phone.
- **Instagram sometimes blocks servers.** If you get a message saying Instagram wants a login, do one of these:
  - Upload the video instead: save it or screen-record it, then tap *Pick a video*.
  - Add your Instagram cookies as an `IG_COOKIES` setting in Render. This is a one-time job that needs a computer: export `cookies.txt` for instagram.com with a browser extension and paste in its contents.
- **Keep it working.** If downloads start failing after an Instagram change, open the service in Render and tap **Manual Deploy**. That pulls in the latest yt-dlp.

## Settings

| Name | Needed | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Claude API key |
| `APP_PASSWORD` | strongly recommended | Password for the app |
| `SECRET_KEY` | set by Render | Signs the login cookie |
| `IG_COOKIES` | no | Instagram `cookies.txt` contents |
| `MOODBOARD_MODEL` | no | Claude model, default `claude-sonnet-5-5` |

## Run on a computer instead

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python app.py                                  # web app on http://localhost:8000
python reel_to_moodboard.py reel.mp4           # or one board straight from the terminal
```
