#!/usr/bin/env python3
"""
SpaceX launch notifier.

Polls Launch Library 2 for upcoming SpaceX launches and pushes a phone
notification (via ntfy.sh) when:
  - a new SpaceX launch shows up on the schedule
  - a launch time slips or its status changes (Go / Hold / Scrub ...)
  - T-24h, T-1h and T-10min reminders (with the livestream link)
  - the webcast goes live

Config (environment variables):
  NTFY_TOPIC   required  - your secret ntfy topic name (pick something unguessable)
  STATE_FILE   optional  - default: state.json
  LOOKAHEAD_H  optional  - only track launches within this many hours (default 168 = 7 days)

Usage:
  python spacex_notifier.py            # single check (use with cron / GitHub Actions)
  python spacex_notifier.py --loop     # keep running, check every 5 minutes
  python spacex_notifier.py --test     # send a test notification
"""
import json
import os
import sys
import time
import smtplib
import urllib.request
from email.message import EmailMessage
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

API = ("https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
       "?lsp__name=SpaceX&mode=detailed&ordering=net&limit=10")
IST = ZoneInfo("Asia/Kolkata")
TOPIC = os.environ.get("NTFY_TOPIC", "")          # optional: ntfy.sh push
SMTP_USER = os.environ.get("SMTP_USER", "")        # Gmail address used to send
SMTP_PASS = os.environ.get("SMTP_PASS", "")        # Gmail app password
EMAIL_TO = os.environ.get("EMAIL_TO", "")          # where alerts are delivered
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
LOOKAHEAD_H = float(os.environ.get("LOOKAHEAD_H", "168"))
REMINDERS = [(1440, "24h"), (60, "1h"), (10, "10min")]  # minutes before NET
FALLBACK_STREAM = "https://www.spacex.com/launches"


def fetch_launches():
    req = urllib.request.Request(API, headers={"User-Agent": "spacex-notifier/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("results", [])


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_ist(dt):
    return dt.astimezone(IST).strftime("%a %d %b, %I:%M %p IST")


def stream_url(launch):
    urls = launch.get("vid_urls") or []
    # prefer YouTube / SpaceX links
    for u in urls:
        link = u.get("url", "")
        if "youtube" in link or "youtu.be" in link or "spacex.com" in link or "x.com" in link:
            return link
    return urls[0]["url"] if urls else FALLBACK_STREAM


def details(launch):
    net = parse_time(launch["net"])
    pad = launch.get("pad") or {}
    mission = launch.get("mission") or {}
    rocket = ((launch.get("rocket") or {}).get("configuration") or {}).get("full_name", "")
    lines = [
        f"When: {fmt_ist(net)}",
        f"Status: {(launch.get('status') or {}).get('name', 'Unknown')}",
        f"Rocket: {rocket}",
        f"Pad: {pad.get('name', '?')}, {(pad.get('location') or {}).get('name', '')}",
    ]
    if mission.get("name"):
        lines.append(f"Mission: {mission['name']}")
    if mission.get("orbit", {}) and mission["orbit"].get("name"):
        lines.append(f"Orbit: {mission['orbit']['name']}")
    if mission.get("description"):
        lines.append(mission["description"][:300])
    lines.append(f"Watch: {stream_url(launch)}")
    return "\n".join(lines)


def send_email(title, body, click=None, priority="default"):
    msg = EmailMessage()
    msg["Subject"] = ("[URGENT] " if priority == "urgent" else "") + title
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    if priority in ("high", "urgent"):
        msg["X-Priority"] = "1"
        msg["Importance"] = "high"
    msg.set_content(body + (f"\n\n{click}" if click and click not in body else ""))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print("emailed:", title)


def notify(title, body, click=None, priority="default"):
    sent = False
    if SMTP_USER and SMTP_PASS and EMAIL_TO:
        send_email(title, body, click, priority)
        sent = True
    if not TOPIC:
        if not sent:
            print("No NTFY_TOPIC or email configured; would send:\n", title, "\n", body)
        return
    headers = {"Title": title.encode("ascii", "ignore").decode(),
               "Priority": priority, "Tags": "rocket"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}",
                                 data=body.encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=30).read()
    print("sent:", title)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def check():
    state = load_state()
    now = datetime.now(timezone.utc)
    for l in fetch_launches():
        net = parse_time(l["net"])
        mins = (net - now).total_seconds() / 60
        if mins > LOOKAHEAD_H * 60 or mins < -180:
            continue
        lid = l["id"]
        status = (l.get("status") or {}).get("name", "Unknown")
        link = stream_url(l)
        s = state.setdefault(lid, {"net": None, "status": None, "sent": [], "live": False})
        name = l["name"]

        rearm = False
        if s["net"] is None:
            rearm = True
            notify(f"New SpaceX launch: {name}", details(l), click=link)
        else:
            if abs((net - parse_time(s["net"])).total_seconds()) > 300:
                rearm = True        # re-arm countdown reminders for the new time
                s["live"] = False
                notify(f"Time changed: {name}",
                       f"New time: {fmt_ist(net)}\nWas: {fmt_ist(parse_time(s['net']))}\n"
                       f"Status: {status}\nWatch: {link}", click=link)
            if status != s["status"]:
                notify(f"Status: {status} - {name}", details(l), click=link, priority="high")

        if rearm:
            # the email we just sent already covers every window we're inside;
            # only reminders for windows still ahead should fire later
            s["sent"] = [lbl for thr, lbl in REMINDERS if mins <= thr]

        # countdown reminders (mark skipped larger ones as sent so we don't spam)
        for threshold, label in sorted(REMINDERS):  # smallest window first
            if 0 < mins <= threshold and label not in s["sent"]:
                for t2, l2 in REMINDERS:
                    if t2 >= threshold and l2 not in s["sent"]:
                        s["sent"].append(l2)
                notify(f"T-{label}: {name}", details(l), click=link,
                       priority="high" if threshold <= 60 else "default")
                break

        if l.get("webcast_live") and not s["live"]:
            s["live"] = True
            notify(f"LIVE NOW: {name}", f"Webcast is live!\n{link}", click=link, priority="urgent")

        s["net"], s["status"] = l["net"], status
    save_state(state)


if __name__ == "__main__":
    if "--test" in sys.argv:
        notify("SpaceX notifier test", "If you see this, notifications work.")
    elif "--loop" in sys.argv:
        while True:
            try:
                check()
            except Exception as e:  # keep running through network hiccups / rate limits
                print("error:", e)
            time.sleep(300)
    else:
        check()
