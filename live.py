"""Live clipper: records a Twitch stream while it's live, watches chat, and cuts a Short
around every chat spike (chat floods = something big just happened).

Usage:  ./live.sh <twitch_login> [--dry-run]
Stops by itself when the stream ends. Ctrl+C to stop early.
"""
import socket, statistics, subprocess, sys, threading, time
from collections import deque
from pathlib import Path

import clipbot as cb

WINDOW = 10          # seconds per chat-count bucket
SEG = 5              # seconds per recorded segment
BEFORE, AFTER = 14, 8   # short build-up so the payoff comes early; viewers swipe away from slow starts


def is_spike(count, history):
    """A bucket is a spike if chat is well above its recent normal level."""
    if len(history) < 6:  # need ~1 minute of baseline first
        return False
    base = statistics.median(history)
    return count >= max(10, base * 4, base + 15)


def chat_counter(login, counts, stop):
    """Anonymous Twitch chat (no account, no token): counts messages per WINDOW."""
    while not stop.is_set():
        try:
            s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
            s.sendall(f"NICK justinfan{int(time.time()) % 100000}\r\nJOIN #{login}\r\n".encode())
            buf = b""
            while not stop.is_set():
                data = s.recv(4096)
                if not data:
                    break
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for line in lines:
                    if line.startswith(b"PING"):
                        s.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif b" PRIVMSG " in line:
                        counts[int(time.time() // WINDOW)] = counts.get(int(time.time() // WINDOW), 0) + 1
        except OSError as e:
            print(f"  chat reconnect ({e})", flush=True); time.sleep(5)


def cut(seg_dir, start, end, out):
    """Join the recorded segments covering [start, end] (epoch seconds) into one mp4."""
    segs = sorted(p for p in seg_dir.glob("seg_*.ts") if start - SEG <= int(p.stem[4:]) <= end)
    if not segs:
        return False
    lst = seg_dir / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segs))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", str(out)], check=True)
    return True


def selftest():
    calm = deque([3, 4, 2, 5, 3, 4], maxlen=30)
    assert not is_spike(18, calm) and is_spike(25, calm)
    assert not is_spike(50, deque([3, 4])), "no spikes before a baseline exists"
    busy = deque([40, 45, 38, 50, 42, 44], maxlen=30)
    assert not is_spike(120, busy) and is_spike(180, busy)
    print("live selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    login = sys.argv[1].lower()
    dry = "--dry-run" in sys.argv
    s = next((x for x in cb.CONFIG["streamers"] if x["twitch_login"] == login), None)
    if not s or not s.get("permission", "").strip():
        sys.exit(f"{login} has no clipping permission recorded in config.json")
    max_up = cb.CONFIG.get("live_max_uploads", 4)  # YouTube allows ~6 uploads/day; the rest stay in out/ for TikTok/IG
    seg_dir = cb.OUT / f"live_{login}"
    seg_dir.mkdir(parents=True, exist_ok=True)

    rec = subprocess.Popen(
        f"streamlink --twitch-disable-ads -O twitch.tv/{login} best | "
        f"ffmpeg -loglevel error -i pipe: -c copy -f segment -segment_time {SEG} -reset_timestamps 1 "
        f"-strftime 1 '{seg_dir}/seg_%s.ts'", shell=True)
    counts, stop = {}, threading.Event()
    threading.Thread(target=chat_counter, args=(login, counts, stop), daemon=True).start()
    print(f"recording {login}; watching chat", flush=True)

    history, last_clip, uploaded, yt, n = deque(maxlen=30), 0, 0, None, 0
    cur = int(time.time() // WINDOW)
    try:
        while rec.poll() is None:
            time.sleep(1)
            now = int(time.time() // WINDOW)
            if now == cur:
                continue
            c = counts.pop(cur, 0)
            spike_at = cur * WINDOW
            if is_spike(c, history) and spike_at - last_clip > 60:
                base = statistics.median(history)
                strong = c >= max(25, base * 8)  # only the biggest moments get uploaded; the rest are kept as downloads
                print(f"spike: {c} msgs/{WINDOW}s (normal {base:.0f}){' STRONG' if strong else ''}", flush=True)
                time.sleep(AFTER + SEG + 2)  # let the aftermath get recorded
                n += 1
                raw = seg_dir / f"cut_{spike_at}.mp4"
                if cut(seg_dir, spike_at - BEFORE, spike_at + WINDOW + AFTER, raw):
                    last_clip = spike_at
                    clip = {"id": f"live_{login}_{spike_at}", "file": raw, "title": "", "broadcaster_name": login,
                            "broadcaster_login": login, "url": f"https://twitch.tv/{login}", "creator_name": "clipbot (live)"}
                    try:
                        path, title = cb.make_short(clip, s)
                        print(f"  short: {path}\n  title: {title}", flush=True)
                        if not dry and strong and uploaded < max_up:
                            yt = yt or cb.youtube()
                            vid = cb.upload(yt, path, clip, s.get('hashtag', ''), title, s.get('submit_url'))
                            if vid:
                                print(f"  uploaded https://youtu.be/{vid}", flush=True)
                                uploaded += 1
                    except Exception as e:  # one bad clip must not stop the recording
                        print(f"  ! clip failed: {e}", flush=True)
                    raw.unlink(missing_ok=True)
            else:
                history.append(c)
            cur = now
            for p in seg_dir.glob("seg_*.ts"):  # keep only the last ~3 minutes on disk
                if int(p.stem[4:]) < time.time() - 180:
                    p.unlink(missing_ok=True)
    finally:
        stop.set(); rec.terminate()
        print(f"stream ended or stopped; {n} spikes, {uploaded} uploaded", flush=True)


if __name__ == "__main__":
    main()
