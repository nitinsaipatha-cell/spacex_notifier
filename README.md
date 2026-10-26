# SpaceX Launch Notifier

Emails you about upcoming SpaceX launches, with the livestream link and details, using live data. Nothing to install and no server: GitHub Actions runs it for free every 5 minutes.

## What you get

| Email | When |
|---|---|
| **New SpaceX launch** | A launch newly appears on the schedule (looks 7 days ahead) |
| **Time changed** | A launch is delayed or preponed by more than 5 minutes |
| **Status: ...** | Status changes, e.g. Go, Hold, Scrubbed |
| **T-24h / T-1h / T-10min** | Countdown reminders. They re-arm if the launch time moves |
| **LIVE NOW** | The webcast goes live |

Every email shows the launch time in IST, rocket, launch pad, mission and the stream link.

## How it works

`spacex_notifier.py` asks [Launch Library 2](https://thespacedevs.com/llapi) for upcoming SpaceX launches. It compares them with `state.json` (what it has already told you) and emails only what changed. The workflow in `.github/workflows/spacex.yml` runs it every 5 minutes and commits the updated `state.json`.

## Setup

1. **Gmail app password.** Turn on 2-Step Verification at myaccount.google.com → Security, then create an app password named `spacex` and copy the 16-character code.
2. **Fork or create a public repo** containing `spacex_notifier.py` (top level) and `.github/workflows/spacex.yml`. Public repos get unlimited free Actions minutes.
3. **Add secrets** under Settings → Secrets and variables → Actions:
   - `SMTP_USER`: the Gmail address that sends the alerts
   - `SMTP_PASS`: the app password (no spaces)
   - `EMAIL_TO`: where alerts are delivered (can be the same Gmail)
   - `NTFY_TOPIC`: optional, for [ntfy.sh](https://ntfy.sh) phone push as well
4. **Allow state saving.** Settings → Actions → General → Workflow permissions → *Read and write permissions*.
5. **Run once.** Actions tab → *SpaceX launch check* → *Run workflow*. A green tick plus an email means it works. After that it runs by itself.

## Configuration

Environment variables, set in the workflow's `env:` block:

| Variable | Default | Meaning |
|---|---|---|
| `LOOKAHEAD_H` | `168` | Only track launches within this many hours (7 days) |
| `STATE_FILE` | `state.json` | Where already-sent alerts are remembered |

Reminder times are the `REMINDERS` list near the top of the script.

## Run locally

```bash
export SMTP_USER=you@gmail.com SMTP_PASS=app-password EMAIL_TO=you@gmail.com
python spacex_notifier.py --test   # send a test email
python spacex_notifier.py          # one check
python spacex_notifier.py --loop   # check every 5 minutes
```

## Troubleshooting

- **No emails:** if no launch is within 7 days, nothing is sent. Use `--test` to confirm email delivery.
- **`SMTPAuthenticationError` / 535:** wrong app password or Gmail address. Re-create the app password.
- **HTTP 429:** the free launch API is rate-limiting. The next run retries.
- **"Save state" step fails with 403:** set workflow permissions to *Read and write*.
- **Emails stopped after weeks:** GitHub pauses scheduled workflows on inactive repos. Actions tab → *Enable workflow*.
- **Alerts in spam:** mark one as *Not spam* and add a Gmail filter on the subject `SpaceX`.

## Notes

- GitHub's scheduler can run a few minutes late, so alerts aren't second-exact.
- Launch times and webcast links come from a community-maintained database and can lag SpaceX's own announcements slightly.
- Not affiliated with SpaceX.
