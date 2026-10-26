# SpaceX Launch Notifier

Get an email whenever a SpaceX launch is scheduled, delayed, preponed or goes live, with the livestream link and launch details. It runs for free on GitHub Actions, so there is nothing to install and no server to keep on.

## What you get

| Email | When it is sent |
|---|---|
| **New SpaceX launch** | A launch newly appears in the next 7 days |
| **Time changed** | A launch is delayed or preponed by more than 5 minutes |
| **Status: ...** | Status changes, e.g. Go, Hold, Scrubbed |
| **T-24h / T-1h / T-10min** | Countdown reminders. They re-arm if the launch time moves |
| **LIVE NOW** | The webcast goes live |

Each email shows the time (in IST by default), rocket, pad, mission and the stream link.

## How it works

1. Every 5 minutes, GitHub Actions runs `spacex_notifier.py`.
2. The script fetches upcoming SpaceX launches from [Launch Library 2](https://thespacedevs.com/llapi), a free community-run API. No API key is needed.
3. It compares them with `state.json` (what you were already told) and emails only what is new or changed.
4. The workflow commits the updated `state.json` back to the repo, so you never get the same alert twice.

## Setup (about 10 minutes)

**Your emails are sent from your own Gmail. Forking this repo does not copy anyone's secrets, so you must add your own.**

1. **Fork this repo** (keep it **public**: public repos get unlimited free Actions minutes; a private repo can run out of the free monthly quota at a 5-minute schedule).
2. **Create a Gmail app password.** Go to myaccount.google.com → Security, turn on 2-Step Verification, then search "App passwords" and create one called `spacex`. Copy the 16-character code. Consider a separate Gmail just for alerts.
3. **Add three secrets** in your fork under Settings → Secrets and variables → Actions → New repository secret. Names must match exactly:
   - `SMTP_USER`: the Gmail address that sends the alerts
   - `SMTP_PASS`: the app password, without spaces
   - `EMAIL_TO`: the address that receives alerts (can be the same Gmail)
   - `NTFY_TOPIC` *(optional)*: a topic name if you also want [ntfy.sh](https://ntfy.sh) phone push
4. **Enable Actions.** On a fork, open the Actions tab and click "I understand my workflows, go ahead and enable them". Forks start with scheduled workflows switched off.
5. **Allow state saving.** Settings → Actions → General → Workflow permissions → *Read and write permissions* → Save.
6. **Run it once.** Actions tab → *SpaceX launch check* → *Run workflow*. A green tick plus an email means it works. After that it runs on its own, though the first scheduled run can take up to about 15 minutes.

**Test email:** to check delivery even when no launch is near, temporarily change the run line in `.github/workflows/spacex.yml` to `python spacex_notifier.py --test`, run it, then **change it back**, or you will only ever get test emails.

## Configuration

Set these in the `env:` block of `.github/workflows/spacex.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `LOOKAHEAD_H` | `168` | Only track launches within this many hours (7 days) |
| `STATE_FILE` | `state.json` | File that remembers what was already sent |

Other tweaks in `spacex_notifier.py`: `REMINDERS` (countdown times, in minutes) and `IST` (change the time zone, e.g. `ZoneInfo("America/New_York")`).

## Run locally (optional)

```bash
export SMTP_USER=you@gmail.com SMTP_PASS=app-password EMAIL_TO=you@gmail.com
python spacex_notifier.py --test   # send a test email
python spacex_notifier.py          # one check
python spacex_notifier.py --loop   # keep running, check every 5 minutes
```

Needs Python 3.9+ and no extra packages.

## Troubleshooting

| Problem | Fix |
|---|---|
| No emails, green run | Normal if no launch is within 7 days. Use the test email to confirm delivery |
| `SMTPAuthenticationError` / `535` | Wrong address or app password. Re-create the app password, paste without spaces |
| "Save state" step fails with `403` | Set workflow permissions to *Read and write* (step 5) |
| `HTTP Error 429` | The free API is rate-limiting. The next run retries automatically |
| Emails stopped after weeks | GitHub pauses schedules on inactive repos. Actions tab → *Enable workflow* |
| Alerts land in spam | Mark one as *Not spam* and add a Gmail filter on the subject `SpaceX` |
| Secret not found / empty | Secret names must be exactly `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`, in capitals |

## Good to know

- GitHub's scheduler can run a few minutes late, so alerts are close to real time, not to the second.
- Data comes from a community-maintained database, not directly from SpaceX, so changes can show up a few minutes after SpaceX announces them.
- The free API is rate-limited. Don't lower the 5-minute interval.
- Never put your app password in code, issues or commits. If you leak it, revoke it in Google Account → App passwords and make a new one.
- Not affiliated with SpaceX.
