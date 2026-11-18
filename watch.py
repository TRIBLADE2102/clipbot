"""Cron calls this every 5 minutes: starts the live clipper for any permitted streamer who is live."""
import os, subprocess, sys

import clipbot as cb

logins = [s["twitch_login"] for s in cb.CONFIG["streamers"] if s.get("permission", "").strip()]
live = cb.http_json("https://api.twitch.tv/helix/streams?" + "&".join(f"user_login={l}" for l in logins),
                    headers=cb.twitch_headers())["data"]
if os.environ.get("CI") and live:  # cloud: record the first live streamer inside this job (max 6h)
    # shortcut: one streamer at a time in the cloud; a second live streamer waits for the next check
    import live as lv
    print(f"{live[0]['user_login']} is live ({live[0]['game_name']}), recording", flush=True)
    sys.argv = ["live.py", live[0]["user_login"].lower()]
    lv.main(); sys.exit()
for st in live:
    login = st["user_login"].lower()
    if subprocess.run(["pgrep", "-f", f"live.py {login}"], capture_output=True).returncode == 0:
        continue  # already recording
    print(f"{login} is live ({st['game_name']}), starting live clipper", flush=True)
    subprocess.Popen(["nohup", str(cb.HERE / "live.sh"), login], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
