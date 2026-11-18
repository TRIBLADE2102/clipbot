"""Clip bot: top Twitch clips -> vertical Shorts -> YouTube upload.

Usage:
  python3 clipbot.py              # full run (uploads)
  python3 clipbot.py --dry-run    # everything except upload
  python3 clipbot.py --selftest   # check clip-picking logic, no network
Needs env vars TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (put them in .env; run.sh loads it).
"""
import json, os, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
POSTED = HERE / "posted.json"   # clip ids already uploaded, so nothing is posted twice
OUT = HERE / "out"


def http_json(url, data=None, headers=None):
    req = Request(url, data=data, headers=headers or {})
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def twitch_headers():
    cid, secret = os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"]
    tok = http_json("https://id.twitch.tv/oauth2/token", data=urlencode(
        {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}).encode())
    return {"Client-Id": cid, "Authorization": f"Bearer {tok['access_token']}"}


def fetch_clips(h, login, hours):
    users = http_json(f"https://api.twitch.tv/helix/users?login={login}", headers=h)["data"]
    if not users:
        print(f"  ! no Twitch user '{login}'"); return []
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = urlencode({"broadcaster_id": users[0]["id"], "started_at": since, "first": 50})
    return http_json(f"https://api.twitch.tv/helix/clips?{q}", headers=h)["data"]


def game_id(h, name):
    if not name:
        return None
    found = http_json(f"https://api.twitch.tv/helix/games?{urlencode({'name': name})}", headers=h)["data"]
    if not found:
        print(f"  ! Twitch has no game named '{name}' yet, no game preference this run")
    return found[0]["id"] if found else None


def pick_clips(clips, posted, min_views, n, prefer_game=None):
    fresh = [c for c in clips if c["id"] not in posted and c["view_count"] >= min_views]
    # preferred game first (e.g. GTA 6 at launch), then most viewed
    return sorted(fresh, key=lambda c: (c.get("game_id") == prefer_game, c["view_count"]), reverse=True)[:n]


FONT = next(f for f in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",  # Mac
                       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if os.path.exists(f))  # cloud (Linux)
_whisper = None


def text_png(text, path, size=64, height=160, stroke=5):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (1080, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    while size > 28:  # shrink until it fits the frame width
        font = ImageFont.truetype(FONT, size)
        if d.textlength(text, font=font) <= 1000:
            break
        size -= 4
    d.text((540, height // 2), text, font=font, fill="white", anchor="mm", stroke_width=stroke, stroke_fill="black")
    img.save(path)


def transcribe(video):
    """[(word, start, end)] for the clip's speech; [] if speech-to-text isn't available."""
    global _whisper
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        return []
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    _whisper = _whisper or WhisperModel("base.en", device="cpu", compute_type="int8")
    segs, _ = _whisper.transcribe(np.frombuffer(pcm, np.float32), word_timestamps=True, vad_filter=True)
    return [(w.word.strip(), w.start, w.end) for s in segs for w in s.words]


def caption_chunks(words, max_words=3, max_gap=0.6):
    """Group words into short on-screen captions: [(TEXT, start, end)]."""
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w[1] - cur[-1][2] > max_gap):
            chunks.append(cur); cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return [(re.sub(r"[,.]", "", " ".join(x[0] for x in c)).upper(), c[0][1], c[-1][2]) for c in chunks]


def make_title(clip, words):
    """Keep a real viewer title; replace junk ('1', stream titles with !commands) with a quote from the clip."""
    t = clip["title"].strip()
    clean = " ".join(re.sub(r"[^\w\s'?!]", "", re.sub(r"!\w+", "", t)).split())  # drop !commands and emojis
    if len(clean.split()) >= 3 and len(clean) <= 70 and not re.search(r"![a-z]", t):
        return clean
    filler = r"^(?:(?:yo|bro|ok|okay|all right|alright|like|um+|uh+|so|and|but|oh|man|dude|guys|wait|yeah)\b[\s,]*)+"
    sents = [re.sub(filler, "", s, flags=re.I).strip(" ,") for s in re.split(r"(?<=[.?!])\s+", " ".join(w[0] for w in words))]
    good = [s for s in sents if 3 <= len(s.split()) <= 10]  # complete, punchy lines only
    if good:  # prefer questions/exclamations, then lines close to 7 words
        best = max(good, key=lambda s: (s[-1] in "?!", -abs(len(s.split()) - 7)))
        return '"' + (best[0].upper() + best[1:]).rstrip(",.") + '"'
    long = next((s for s in sents if s), "")
    return ('"' + " ".join(long.split()[:8]) + '…"') if long else (clean or "Insane moment")


TITLE_PROMPT = ("You write YouTube Shorts titles for gaming livestream clips. Reply with only the title: at most 60 "
                "characters, no hashtags, no quotation marks, at most one emoji. Make it a curiosity hook about what "
                "happens in the clip. Use only facts from the clip title and transcript. The transcript is automatic "
                "and may mishear names; the streamer's correct name is given. Write in English.")


def ai_title(clip, words):
    """Claude-written title, or None (no key, refusal, or API error) so the caller falls back to make_title."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    transcript = " ".join(w[0] for w in words)[:3000]
    try:
        r = anthropic.Anthropic().beta.messages.create(
            model="claude-opus-5-5", max_tokens=1024, output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",  # retries on another model if declined
            system=TITLE_PROMPT,
            messages=[{"role": "user", "content": f"Streamer: {clip['broadcaster_name']}\n"
                       f"Viewer's clip title: {clip['title']}\nTranscript: {transcript or '(no speech)'}"}])
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        print(f"  ! AI title failed ({e.__class__.__name__}), using fallback")
        return None
    if r.stop_reason == "refusal":
        return None
    text = next((b.text for b in r.content if b.type == "text"), "").strip().strip('"').replace("#", "")
    return text[:70] or None


def make_short(clip, s):
    """Download the clip and render a 9:16 Short. Returns (path, title)."""
    tmp = OUT / f"{clip['id']}_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    raw, final = tmp / "raw.mp4", OUT / f"{clip['id']}.mp4"
    if clip.get("file"):  # live clipper already cut it locally
        shutil.copy(clip["file"], raw)
    else:
        subprocess.run(["yt-dlp", "-q", "-o", str(raw), clip["url"]], check=True)
    words = transcribe(raw)
    if s.get("facecam"):  # split: streamer's camera big on top, gameplay below
        x, y, w, h = s["facecam"]
        vf = (f"[0:v]crop={w}:{h}:{x}:{y},scale=1080:640:force_original_aspect_ratio=increase,crop=1080:640[cam];"
              "[0:v]crop=ih*1080/1280:ih,scale=1080:1280[game];[cam][game]vstack[v0]")
        banner_y, banner_size, cap_y, wm_y = 0, 40, 530, 1690
    else:  # blurred background, full clip centred
        vf = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
              "[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
        banner_y, banner_size, cap_y, wm_y = 430, 64, 1280, 1600
    text_png(f"{CONFIG['disclosure']} · {clip['broadcaster_name']} · twitch.tv/{clip['broadcaster_login']}",
             tmp / "banner.png", size=banner_size, height=110 if s.get("facecam") else 160)
    inputs, cur, n = ["-i", str(raw), "-i", str(tmp / "banner.png")], "v0", 1
    vf += f";[{cur}][1:v]overlay=0:{banner_y}[v1]"; cur = "v1"
    if s.get("watermark"):  # streamer's required watermark
        n += 1; inputs += ["-i", str(HERE / s["watermark"])]
        vf += f";[{n}:v]scale=640:-1[wm];[{cur}][wm]overlay=(W-w)/2:{wm_y}[v{n}]"; cur = f"v{n}"
    for i, (text, start, end) in enumerate(caption_chunks(words)):
        png = tmp / f"cap{i}.png"
        text_png(text, png, size=88, height=220, stroke=9)
        n += 1; inputs += ["-i", str(png)]
        vf += f";[{cur}][{n}:v]overlay=0:{cap_y}:enable='between(t,{start:.2f},{end:.2f})'[v{n}]"; cur = f"v{n}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", vf, "-map", f"[{cur}]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", str(final)], check=True)
    shutil.rmtree(tmp)
    return final, ai_title(clip, words) or make_title(clip, words)


def youtube():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request as GReq
    from googleapiclient.discovery import build
    scopes, tok = ["https://www.googleapis.com/auth/youtube.upload"], HERE / "token.json"
    creds = Credentials.from_authorized_user_file(tok, scopes) if tok.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GReq())
    elif not creds or not creds.valid:
        if (HERE / "client_secret.json").exists():
            flow = InstalledAppFlow.from_client_secrets_file(HERE / "client_secret.json", scopes)
        else:  # keys from .env (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)
            flow = InstalledAppFlow.from_client_config({"installed": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"]}}, scopes)
        creds = flow.run_local_server(port=0)
    tok.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(yt, path, clip, hashtag="", title_text=None):
    from googleapiclient.http import MediaFileUpload
    who = clip["broadcaster_name"]
    tags = f"{hashtag} #shorts".strip()  # campaign rules require the streamer hashtag in the caption
    title = f"{title_text or clip['title'].strip()} | {who}"[:99 - len(tags)] + " " + tags
    desc = (f"{hashtag}\nStreamer: https://twitch.tv/{clip['broadcaster_login']}\nOriginal clip: {clip['url']}\n"
            f"Clipped by {clip['creator_name']} on Twitch. All credit to {who}.")
    body = {"snippet": {"title": title, "description": desc, "categoryId": "20", "tags": [who, "twitch", "clips"]},
            "status": {"privacyStatus": CONFIG["privacy"], "selfDeclaredMadeForKids": False}}
    res = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(path), resumable=True)).execute()
    return res["id"]


def selftest():
    c = lambda i, v: {"id": i, "view_count": v}
    got = pick_clips([c("a", 50), c("b", 900), c("c", 400), c("d", 2000)], {"d"}, 300, 2)
    assert [x["id"] for x in got] == ["b", "c"], got
    assert pick_clips([], set(), 0, 3) == []
    g = lambda i, v, gid: {"id": i, "view_count": v, "game_id": gid}
    got = pick_clips([g("x", 9000, "1"), g("y", 400, "gta"), g("z", 800, "gta")], set(), 300, 3, "gta")
    assert [x["id"] for x in got] == ["z", "y", "x"], got
    w = [("yo,", 0.0, 0.3), ("this", 0.4, 0.6), ("is", 0.6, 0.7), ("crazy.", 0.7, 1.0), ("wait", 2.0, 2.2)]
    assert caption_chunks(w) == [("YO THIS IS", 0.0, 0.7), ("CRAZY", 0.7, 1.0), ("WAIT", 2.0, 2.2)], caption_chunks(w)
    assert make_title({"title": "wirdo dying to clix"}, w) == "wirdo dying to clix"
    assert make_title({"title": "1"}, w) == '"This is crazy"', make_title({"title": "1"}, w)
    q = [(x, 0, 0) for x in "Yo, yo, all right, I'm gonna give you a PC. Can you get this kid's address?".split()]
    assert make_title({"title": "pc winner"}, q) == '"Can you get this kid\'s address?"', make_title({"title": "pc winner"}, q)
    assert make_title({"title": "SAN DIEGO DAY 2 !discord !clips"}, []) == "SAN DIEGO DAY 2"
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    dry = "--dry-run" in sys.argv
    posted = set(json.loads(POSTED.read_text())) if POSTED.exists() else set()
    h, yt = twitch_headers(), None
    prefer = game_id(h, CONFIG.get("prefer_game", ""))
    budget = CONFIG["clips_per_run"]
    for s in CONFIG["streamers"]:
        if not s.get("permission", "").strip() or s["permission"].startswith("Leave blank"):
            print(f"skip {s['twitch_login']}: no clipping permission recorded"); continue
        for clip in pick_clips(fetch_clips(h, s["twitch_login"], CONFIG["lookback_hours"]), posted, CONFIG["min_views"], budget, prefer):
            clip["broadcaster_login"] = s["twitch_login"]  # Helix clips don't return the login
            print(f"{s['twitch_login']}: {clip['title']!r} ({clip['view_count']} views)")
            path, title = make_short(clip, s)
            print(f"  title: {title}")
            budget -= 1
            if dry:
                print(f"  dry run, saved {path}"); continue
            yt = yt or youtube()
            print(f"  uploaded https://youtu.be/{upload(yt, path, clip, s.get('hashtag', ''), title)}")
            posted.add(clip["id"]); POSTED.write_text(json.dumps(sorted(posted)))
            path.unlink()
        if budget <= 0:
            break


if __name__ == "__main__":
    main()
