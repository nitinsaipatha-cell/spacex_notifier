#!/usr/bin/env python3
"""
SpaceX launch notifier.

Watches upcoming SpaceX launches and emails you (and optionally pushes via
ntfy.sh) when:
  - a new SpaceX launch shows up on the schedule        (+ calendar invite)
  - a launch time slips or moves earlier                (+ updated invite)
  - a launch status changes (Go / Hold / ...)
  - T-24h, T-1h and T-10min reminders (with the livestream link)
  - the webcast goes live
  - the notifier itself keeps failing                   (health alert)

Data sources (tried in order):
  1. Launch Library 2   (primary, richest data, free tier = 15 requests/hour)
  2. RocketLaunch.Live  (fallback when the primary is rate-limited or down)

Config (environment variables):
  SMTP_USER / SMTP_PASS / EMAIL_TO   Gmail sender, app password, recipient
  NTFY_TOPIC        optional - also push via ntfy.sh
  TIMEZONE          optional - display time zone, default Asia/Kolkata
  STATE_FILE        optional - default: state.json
  LOOKAHEAD_H       optional - track launches within this many hours (default 168)
  FAIL_ALERT_AFTER  optional - consecutive failed runs before a health alert (default 3)

Usage:
  python spacex_notifier.py            # single check (cron / GitHub Actions)
  python spacex_notifier.py --loop     # keep running, check every 5 minutes
  python spacex_notifier.py --test     # send a test email with a sample calendar invite
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

LL2_URL = ("https://ll.thespacedevs.com/2.2.0/launch/upcoming/"
           "?lsp__name=SpaceX&mode=detailed&ordering=net&limit=10")
RLL_URL = "https://fdo.rocketlaunch.live/json/launches/next/10"
UA = "spacex-notifier/2.0"

LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
TOPIC = os.environ.get("NTFY_TOPIC", "")           # optional: ntfy.sh push
SMTP_USER = os.environ.get("SMTP_USER", "")        # Gmail address used to send
SMTP_PASS = os.environ.get("SMTP_PASS", "")        # Gmail app password
EMAIL_TO = os.environ.get("EMAIL_TO", "")          # where alerts are delivered
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
LOOKAHEAD_H = float(os.environ.get("LOOKAHEAD_H", "168"))
FAIL_ALERT_AFTER = int(os.environ.get("FAIL_ALERT_AFTER", "3"))
REMINDERS = [(1440, "24h"), (60, "1h"), (10, "10min")]  # minutes before NET
FALLBACK_STREAM = "https://www.spacex.com/launches"


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def parse_time(s):
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M%z")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_local(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%a %d %b, %I:%M %p %Z")


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------
class FetchError(Exception):
    pass


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_ll2():
    """Primary source. Retry once on network/5xx errors, never on 429."""
    for attempt in (1, 2):
        try:
            return http_json(LL2_URL).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code == 429 or attempt == 2 or e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(3)


def fetch_rll():
    """Fallback source, normalised to the same shape Launch Library 2 uses.

    Less detail than the primary (no live-webcast flag, no status names), so
    the rest of the code treats it as 'degraded' and avoids false alerts."""
    out = []
    for x in http_json(RLL_URL).get("result", []):
        if "spacex" not in ((x.get("provider") or {}).get("name") or "").lower():
            continue
        t0 = x.get("t0") or x.get("win_open")
        if not t0:
            continue  # no date yet
        vehicle = (x.get("vehicle") or {}).get("name") or ""
        mission = x.get("name") or "SpaceX launch"
        pad = x.get("pad") or {}
        urls = re.findall(r"https?://[^\s\"'<>]+", x.get("quicktext") or "")
        out.append({
            "id": f"rll-{x.get('id')}",
            "name": f"{vehicle} | {mission}" if vehicle else mission,
            "net": t0,
            "status": {"name": "Scheduled"},
            "vid_urls": [{"url": u} for u in urls],
            "webcast_live": False,
            "rocket": {"configuration": {"full_name": vehicle}},
            "pad": {"name": pad.get("name", "?"),
                    "location": {"name": (pad.get("location") or {}).get("name", "")}},
            "mission": {"name": mission,
                        "description": x.get("launch_description") or ""},
        })
    return out


SOURCES = [("ll2", fetch_ll2), ("rll", fetch_rll)]


def fetch_any():
    errors = []
    for name, fn in SOURCES:
        try:
            return fn(), name
        except Exception as e:  # any failure -> try the next source
            errors.append(f"{name}: {e}")
            print(f"source {name} failed: {e}")
    raise FetchError("; ".join(errors))


# --------------------------------------------------------------------------
# Launch -> text
# --------------------------------------------------------------------------
def stream_url(launch):
    urls = launch.get("vid_urls") or []
    for u in urls:  # prefer YouTube / SpaceX links
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
        f"When: {fmt_local(net)}",
        f"Status: {(launch.get('status') or {}).get('name', 'Unknown')}",
        f"Rocket: {rocket}",
        f"Pad: {pad.get('name', '?')}, {(pad.get('location') or {}).get('name', '')}",
    ]
    if mission.get("name"):
        lines.append(f"Mission: {mission['name']}")
    if (mission.get("orbit") or {}).get("name"):
        lines.append(f"Orbit: {mission['orbit']['name']}")
    if mission.get("description"):
        lines.append(mission["description"][:300])
    lines.append(f"Watch: {stream_url(launch)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Calendar (.ics for Apple/any calendar, link for Google Calendar)
# --------------------------------------------------------------------------
def _ics_text(t):
    return (t.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\r", "").replace("\n", "\\n"))


def _fold(line):
    """RFC 5545 line folding: max 75 octets, never split a UTF-8 character."""
    b, out = line.encode("utf-8"), []
    while len(b) > 75:
        cut = 75
        while (b[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(uid, seq, title, start, end, description, location, url):
    z = "%Y%m%dT%H%M%SZ"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//spacex-notifier//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@spacex-notifier",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(z)}",
        f"DTSTART:{start.astimezone(timezone.utc).strftime(z)}",
        f"DTEND:{end.astimezone(timezone.utc).strftime(z)}",
        f"SEQUENCE:{seq}",
        f"SUMMARY:{_ics_text(title)}",
        f"LOCATION:{_ics_text(location)}",
        f"DESCRIPTION:{_ics_text(description)}",
        f"URL:{url}",
        "STATUS:CONFIRMED",
    ]
    for trig, label in (("-PT1H", "1 hour"), ("-PT10M", "10 minutes")):
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                  f"DESCRIPTION:{_ics_text(title)} in {label}",
                  f"TRIGGER:{trig}", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(_fold(l) for l in lines) + "\r\n").encode("utf-8")


def gcal_link(title, start, end, description, location):
    z = "%Y%m%dT%H%M%SZ"
    q = urllib.parse.urlencode({
        "action": "TEMPLATE", "text": title,
        "dates": f"{start.astimezone(timezone.utc).strftime(z)}/"
                 f"{end.astimezone(timezone.utc).strftime(z)}",
        "details": description, "location": location,
    }, quote_via=urllib.parse.quote, safe="/")
    return "https://calendar.google.com/calendar/render?" + q


def calendar_for(launch, s):
    """Return (ics_bytes, [(label, url)]) for a launch. Same UID + rising
    SEQUENCE means calendars update the existing event when a launch moves."""
    start = parse_time(launch["net"])
    end = start + timedelta(hours=1)
    title = f"SpaceX: {launch['name']}"
    pad = launch.get("pad") or {}
    location = ", ".join(x for x in (pad.get("name"),
                                     (pad.get("location") or {}).get("name")) if x)
    desc = details(launch)
    ics = build_ics(s["uid"], s["seq"], title, start, end, desc, location,
                    stream_url(launch))
    return ics, [("Add to Google Calendar", gcal_link(title, start, end, desc, location))]


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def _to_html(body, links):
    esc = html.escape(body).replace("\n", "<br>")
    esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
    btns = "".join(
        f'<p><a href="{html.escape(u)}" style="display:inline-block;padding:10px 16px;'
        f'background:#1a73e8;color:#fff;border-radius:6px;text-decoration:none">'
        f'{html.escape(l)}</a></p>' for l, u in links)
    return (f'<div style="font-family:system-ui,sans-serif;font-size:15px;'
            f'line-height:1.5">{esc}{btns}</div>')


def send_email(title, body, click=None, priority="default", ics=None, links=None):
    links = links or []
    plain = body + (f"\n\n{click}" if click and click not in body else "")
    if links or ics:
        plain += "\n"
        plain += "".join(f"\n{l}: {u}" for l, u in links)
        if ics:
            plain += "\nApple Calendar / other: open the attached .ics file."
    msg = EmailMessage()
    msg["Subject"] = ("[URGENT] " if priority == "urgent" else "") + title
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    if priority in ("high", "urgent"):
        msg["X-Priority"] = "1"
        msg["Importance"] = "high"
    msg.set_content(plain)
    if links:
        html_body = body + (f"\n\n{click}" if click and click not in body else "")
        msg.add_alternative(_to_html(html_body, links), subtype="html")
    if ics:
        msg.add_attachment(ics, maintype="text", subtype="calendar",
                           filename="spacex-launch.ics",
                           params={"method": "PUBLISH", "charset": "utf-8"})
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    print("emailed:", title)


def send_ntfy(title, body, click=None, priority="default"):
    headers = {"Title": title.encode("ascii", "ignore").decode(),
               "Priority": priority, "Tags": "rocket"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}",
                                 data=body.encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=30).read()
    print("pushed:", title)


def notify(title, body, click=None, priority="default", ics=None, links=None):
    """Send on every configured channel. Raises only if ALL of them fail, so
    one broken channel never blocks the other (and the run goes red)."""
    channels, errors, ok = [], [], 0
    if SMTP_USER and SMTP_PASS and EMAIL_TO:
        channels.append(lambda: send_email(title, body, click, priority, ics, links))
    if TOPIC:
        channels.append(lambda: send_ntfy(title, body, click, priority))
    if not channels:
        print("No email or ntfy configured; would send:\n", title, "\n", body)
        return
    for ch in channels:
        try:
            ch()
            ok += 1
        except Exception as e:
            errors.append(str(e))
            print("channel failed:", e)
    if not ok:
        raise RuntimeError("all notification channels failed: " + "; ".join(errors))


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


def adopt_alias(state, lid, net, fetched_ids):
    """The same launch has different IDs in different sources. If an unclaimed
    state entry sits within 2h of this launch, it is the same launch: re-key it
    instead of announcing it as new."""
    best = None
    for k, v in state.items():
        if k == "_meta" or k in fetched_ids or not v.get("net"):
            continue
        d = abs((parse_time(v["net"]) - net).total_seconds())
        if d <= 7200 and (best is None or d < best[0]):
            best = (d, k)
    if best:
        state[lid] = state.pop(best[1])


# --------------------------------------------------------------------------
# Main logic
# --------------------------------------------------------------------------
def process(l, src, state, now, fetched_ids):
    net = parse_time(l["net"])
    mins = (net - now).total_seconds() / 60
    if mins > LOOKAHEAD_H * 60 or mins < -180:
        return
    lid, name, link = l["id"], l["name"], stream_url(l)
    if lid not in state:
        adopt_alias(state, lid, net, fetched_ids)
    s = state.setdefault(lid, {"net": None, "status": None, "sent": [], "live": False})
    s.setdefault("nets", {})
    s.setdefault("seq", 0)
    s.setdefault("uid", lid)
    if not s["nets"] and s.get("net"):          # state written by an older version
        s["nets"]["ll2"] = s["net"]

    degraded = src != "ll2"                      # fallback data: fewer fields
    status = (s["status"] or "Scheduled") if degraded else \
             (l.get("status") or {}).get("name", "Unknown")

    # Times are only compared against the same source's previous value, so
    # switching sources can never fake a delay.
    prev = s["nets"].get(src)
    changed = prev is not None and abs((net - parse_time(prev)).total_seconds()) > 300

    rearm = False
    if s["net"] is None:
        rearm = True
        ics, links = calendar_for(l, s)
        notify(f"New SpaceX launch: {name}", details(l), click=link, ics=ics, links=links)
    else:
        if changed:
            rearm = True
            s["live"] = False
            s["seq"] += 1
            ics, links = calendar_for(l, s)
            notify(f"Time changed: {name}",
                   f"New time: {fmt_local(net)}\nWas: {fmt_local(parse_time(prev))}\n"
                   f"Status: {status}\nWatch: {link}",
                   click=link, ics=ics, links=links)
        if not degraded and status != s["status"]:
            notify(f"Status: {status} - {name}", details(l), click=link, priority="high")

    if rearm:
        # the email just sent covers every window we're already inside
        s["sent"] = [lbl for thr, lbl in REMINDERS if mins <= thr]

    for threshold, label in sorted(REMINDERS):   # smallest window first
        if 0 < mins <= threshold and label not in s["sent"]:
            notify(f"T-{label}: {name}", details(l), click=link,
                   priority="high" if threshold <= 60 else "default")
            s["sent"] += [l2 for t2, l2 in REMINDERS if t2 >= threshold and l2 not in s["sent"]]
            break

    if not degraded and l.get("webcast_live") and not s["live"]:
        notify(f"LIVE NOW: {name}", f"Webcast is live!\n{link}", click=link, priority="urgent")
        s["live"] = True

    # Only after every notification succeeded do we remember the new values,
    # so a failed send is retried on the next run.
    s["net"], s["status"] = l["net"], status
    s["nets"][src] = l["net"]


def check():
    state = load_state()
    meta = state.setdefault("_meta", {"fails": 0, "alerted": False, "last_error": ""})
    now = datetime.now(timezone.utc)
    try:
        try:
            launches, src = fetch_any()
        except FetchError as e:
            meta["fails"] += 1
            meta["last_error"] = str(e)[:300]
            print(f"all sources failed ({meta['fails']} in a row): {e}")
            if meta["fails"] >= FAIL_ALERT_AFTER and not meta["alerted"]:
                notify("SpaceX notifier is failing",
                       f"Every data source has failed for {meta['fails']} runs in a row "
                       f"(about {meta['fails'] * 5} minutes), so launch alerts may be "
                       f"missing.\n\nLast error: {meta['last_error']}\n\n"
                       "You will get one message when it recovers.",
                       priority="high")
                meta["alerted"] = True
            return

        if meta["alerted"]:
            notify("SpaceX notifier recovered",
                   f"Data is flowing again after {meta['fails']} failed runs.")
        meta.update(fails=0, alerted=False, last_error="")

        fetched_ids = {l["id"] for l in launches}
        for l in launches:
            process(l, src, state, now, fetched_ids)

        cutoff = now - timedelta(days=2)       # forget launches that are long gone
        for k in [k for k, v in state.items()
                  if k != "_meta" and v.get("net") and parse_time(v["net"]) < cutoff]:
            del state[k]
    finally:
        save_state(state)


def send_test():
    """Test email with a sample calendar invite for a fake launch in 2 days."""
    net = datetime.now(timezone.utc) + timedelta(days=2)
    fake = {"id": "test", "name": "Test launch (ignore)", "net": net.isoformat(),
            "status": {"name": "Go for Launch"}, "vid_urls": [],
            "pad": {"name": "Test pad", "location": {"name": "Test site"}}}
    ics, links = calendar_for(fake, {"uid": "test", "seq": 0})
    notify("SpaceX notifier test",
           "If you see this, notifications work.\n"
           "Check the calendar buttons below: this is a fake launch.\n\n" + details(fake),
           ics=ics, links=links)


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_test()
    elif "--loop" in sys.argv:
        while True:
            try:
                check()
            except Exception:
                traceback.print_exc()   # keep running through hiccups
            time.sleep(300)
    else:
        try:
            check()
        except Exception:
            traceback.print_exc()
            sys.exit(1)   # red run -> GitHub's own failure email is the backstop
