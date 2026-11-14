# Job alert bot

A self-automated job alert bot I built for myself, so I never have to refresh job sites by hand and
never miss a new posting. It runs on its own in the cloud, checks about 30 Sri Lankan and remote job
sources every 30 minutes, filters by keyword and (optionally) an AI fit score, removes duplicates and
sends only the new matches to Telegram. I also use it to send alerts to my girlfriend.

**What it does**
- Polls job boards (topjobs.lk, xpress.jobs, ikman.lk, LinkedIn, itpro.lk, rooster.jobs, gazette.lk),
  company career portals (Workday, SuccessFactors, SmartRecruiters, Recruitee and custom pages) and
  remote-job feeds.
- Matches each ad by title against per-person keyword profiles, excludes seniority and location
  mismatches, and checks remote ads for eligibility, years asked and scam signs.
- Scores fit with Gemini when a key is set, and falls back to keyword scoring if the quota runs out.
- Remembers what it has already sent, so each job alerts once. Reports a source in Telegram when it
  starts failing, for example because the site layout changed.
- Runs on GitHub Actions (with cron-job.org as the trigger), plus a laptop task for the one site that
  blocks cloud servers.

## Setup (one time)
1. In Telegram, open **@BotFather**, send `/newbot` and pick a name. Copy the token it gives you.
2. Copy `.env.example` to `.env` and paste the token after `TELEGRAM_TOKEN=`.
3. On the phone that should get the alerts, open the new bot and press **Start**.
4. Run `python jobbot.py --setup`. This finds the chat id and saves it to `.env`.
5. Run `python jobbot.py --test`. A test message should arrive.
6. Run `powershell -ExecutionPolicy Bypass -File install_task.ps1`. This sets up the 30-minute schedule.

The first run sends the roles that are open now. After that the bot sends only new ones,
plus a check-in every day after 8 pm. If a site keeps failing (for example because its
layout changed), the bot says so in Telegram.

## Files
- `config.json`: the keywords, excluded words and locations, LinkedIn searches and career pages. Edit this file to tune what matches.
- `seen.json`: the jobs already alerted. Delete it to start fresh.
- `jobbot.log`: what each run found.
- `python jobbot.py --dry`: shows what would match, without sending or saving anything.
- To stop the bot: `Unregister-ScheduledTask JobBot`

To alert more than one person, add a profile in `config.json` with its own chat id variable (the second profile's label and description are read from the `PARTNER_LABEL` and `PARTNER_SUMMARY` secrets or `.env`).
`.env` holds the bot token, so keep it private.

## Cloud schedule
The bot runs on GitHub Actions. GitHub's own `schedule` is unreliable (it often skips runs), so
cron-job.org calls the `workflow_dispatch` API every 30 minutes, using a fine-grained token limited
to Actions on this repo. The GitHub schedule stays on as a backup, and `concurrency` stops two runs
from overlapping. **The token expires on 15 Jan 2027**: make a new one and paste it into the
cron-job.org job before then.

## Sources
Cloud (GitHub Actions): topjobs.lk, LinkedIn, ikman.lk, Dialog / Mobitel / SLT career portals,
Hutch / Superloop / Lanka Bell career pages, gazette.lk (government and public sector),
LSEG and Sysco LABS (Workday), GTN (SuccessFactors), WSO2 and Ericsson Sri Lanka, Port City BPO
(careers-page.com), Cloud Solutions International (Recruitee),
IFS (SmartRecruiters), 99x, itpro.lk (the newest 150 ads on each check) and rooster.jobs (the newest 200 Sri Lanka ads).
Remote jobs open to Sri Lanka, for Yohan only (Himalayas, Jobicy, We Work Remotely): each ad is
checked for worldwide/APAC/Sri Lanka eligibility, country locks, seniority, years asked (max 2)
and scam signs before it can alert (sources_remote.py).

Laptop (Task Scheduler "JobBot", every 30 min while the PC is on): xpress.jobs only,
because it blocks GitHub's servers. `run.bat` sets `JOBBOT_ONLY=xpress`, which keeps its own
`seen_xpress.json`. Add `GEMINI_API_KEY=` to `.env` for AI fit scores there too.

A company careers page's first check sends one summary of what is already listed; after that
only new adverts are alerted.
