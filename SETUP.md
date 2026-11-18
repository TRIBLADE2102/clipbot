# Clip bot setup (about 1 hour, once)

What it does, every run: for each streamer you've listed **with permission recorded**, it gets their most-viewed Twitch clips from the last 48h, skips anything already posted, turns each one into a 9:16 Short, and uploads it to YouTube with credit to the streamer and a link to the original clip.

## 1. Get permission (needed for this to last)
Pick 3–5 streamers who **openly allow clipping**. Many run clipper programs on Discord or say so in their Twitch panels. Write down where they gave it in `config.json` → `permission`. Streamers with a blank permission field are skipped.
Without permission you'll get copyright claims, and three strikes deletes the channel.

## 2. Accounts you create yourself
1. **YouTube channel**: a new Google account just for this channel.
2. **Twitch app**: dev.twitch.tv/console → Register Application → copy the Client ID and make a secret.
3. **Google Cloud project**: console.cloud.google.com → new project → enable "YouTube Data API v3" → OAuth consent screen (External, add yourself as a test user) → Credentials → OAuth client ID → "Desktop app" → download the JSON, rename it `client_secret.json`, put it in this folder.

## 3. Install and add keys
```bash
brew install yt-dlp && python3 -m venv .venv && .venv/bin/pip install google-api-python-client google-auth-oauthlib pillow
```
Create `.env` in this folder (never share it):
```
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
```

## 4. First run
```bash
./run.sh --dry-run && tail clipbot.log
```
Watch a few videos in `out/`. If they look right:
```bash
./run.sh
```
A browser opens once so you can sign in to the channel's Google account. After that it runs without you.

## 5. Schedule it (every 6 hours)
`crontab -e`, add:
```
0 */6 * * * /full/path/to/clipbot/run.sh
```
The Mac has to be awake at those times.

## 6. Go public
Uploads go up **private** (`config.json` → `privacy`). YouTube keeps every API upload private until your Google Cloud project passes their audit ("YouTube API Services audit and quota extension" form). Until then, publish them yourself in YouTube Studio once a day. After the audit passes, set `privacy` to `public`.

## 7. Live clipper (GTA 6 launch night)
Start it when a permitted streamer goes live; it stops when the stream ends:
```bash
./live.sh jshock9nine
```
It records the stream, watches chat anonymously, and cuts a Short around every chat spike.
First 4 Shorts upload to YouTube (setting `live_max_uploads`), the rest stay in `out/` for TikTok/IG.
Add `--dry-run` to make Shorts without uploading. Log: `live.log`. The Mac stays awake while it runs.
