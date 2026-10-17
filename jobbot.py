"""Job alert bot for Pavani: polls Sri Lankan job sources, filters for
entry-level telecom / EEE roles and sends new matches to Telegram.

Usage:
  python jobbot.py            one polling run (Task Scheduler calls this)
  python jobbot.py --setup    find the Telegram chat id after messaging the bot
  python jobbot.py --test     send a test message
  python jobbot.py --dry      run without sending or saving (prints matches)
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
SEEN_FILE = HERE / "seen.json"
LOG_FILE = HERE / "jobbot.log"
SL_TZ = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
S = requests.Session()
S.headers.update(UA)


def log(msg):
    line = f"{datetime.now(SL_TZ):%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    # GitHub Actions passes these as environment variables; locally they come from .env
    env = {k: v for k, v in os.environ.items() if k.startswith(("TELEGRAM_", "CHAT_"))}
    p = HERE / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()


# ---------- matching ----------

def _rx(words):
    return [re.compile(r"(?<![a-z0-9])" + re.escape(w.strip().lower()) + r"(?![a-z0-9])")
            for w in words if w.strip()]


class Profile:
    def __init__(self, key, p):
        self.key, self.label, self.send_to = key, p["label"], p["send_to"]
        self.strong = _rx(p["strong_keywords"])
        self.general = _rx(p["general_keywords"])
        self.companies = _rx(p["telecom_companies"])
        self.role = _rx(p["company_role_words"])
        self.excl = _rx(p["exclude_title_words"])
        self.career_names = set(p.get("career_pages", {}))

    def classify(self, job):
        """Return 'strong', 'general' or None for this person."""
        if job.get("career"):
            return "strong" if job["company"] in self.career_names else None
        title = job["title"].lower()
        comp = job.get("company", "").lower()
        where = (job.get("location", "") + " " + title).lower()
        if any_hit(self.excl, title) or any_hit(EXCL_LOC, where):
            return None
        if any_hit(self.strong, title):
            return "strong"
        if any_hit(self.companies, comp) and any_hit(self.role, title):
            return "strong"
        if any_hit(self.general, title):
            return "general"
        return None


PROFILES = [Profile(k, v) for k, v in CFG["profiles"].items()]
EXCL_LOC = _rx(CFG["exclude_locations"])


def union(field):
    out = []
    for p in CFG["profiles"].values():
        items = p.get(field, [])
        for x in (items.items() if isinstance(items, dict) else items):
            if x not in out:
                out.append(x)
    return out


def any_hit(rxs, text):
    return any(r.search(text) for r in rxs)


# ---------- sources ----------

def src_topjobs():
    out = []
    for fa in union("topjobs_categories"):
        r = S.get(f"https://www.topjobs.lk/applicant/vacancybyfunctionalarea.jsp?FA={fa}", timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        for tr in soup.find_all("tr", onclick=re.compile("createAlert")):
            m = re.search(r"createAlert\('\d+','(\w+)','(\d+)','(\w+)'", tr["onclick"])
            if not m:
                continue
            ac, jc, ec = m.groups()
            h2, h1 = tr.find("h2"), tr.find("h1")
            tds = tr.find_all("td", recursive=False)
            out.append({
                "id": f"topjobs:{jc}",
                "title": h2.get_text(" ", strip=True) if h2 else "",
                "company": h1.get_text(" ", strip=True) if h1 else "",
                "closing": tds[5].get_text(strip=True) if len(tds) > 5 else "",
                "location": tds[6].get_text(strip=True) if len(tds) > 6 else "",
                "url": f"https://www.topjobs.lk/employer/JobAdvertismentServlet?ac={ac}&jc={jc}&ec={ec}",
                "source": "topjobs",
            })
    return out


def src_xpress():
    out = []
    for page in range(1, 41):
        r = S.get(f"https://xpress.jobs/api/jobs/searchJobs?page={page}", timeout=30,
                  headers={"Accept": "application/json, text/plain, */*",
                           "Referer": "https://xpress.jobs/jobs"})
        if "json" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"HTTP {r.status_code} {r.headers.get('content-type')} {r.text[:120]!r}")
        data = r.json()
        if not data:
            break
        for j in data:
            exp = (j.get("expiryDateOnWebsite") or "")[:10]
            out.append({
                "id": f"xpress:{j['jobId']}",
                "title": j.get("jobTitle", ""),
                "company": j.get("organizationName", ""),
                "location": (j.get("locations") or "").strip(),
                "closing": exp,
                "url": f"https://xpress.jobs/jobs/view/{j['jobId']}",
                "source": "xpress.jobs",
            })
        time.sleep(0.5)
    return out


def src_ikman():
    out = []
    for q in ["engineer", "telecom", "trainee engineer"]:
        r = S.get(f"https://ikman.lk/en/ads/sri-lanka/jobs?query={quote_plus(q)}", timeout=30)
        m = re.search(r"window.initialData = (\{.*?\})\s*</script>", r.text, re.S)
        if not m:
            continue
        ads = json.loads(m.group(1))["serp"]["ads"]["data"]["ads"]
        for a in ads:
            out.append({
                "id": f"ikman:{a['id']}",
                "title": a.get("title", ""),
                "company": a.get("details", ""),
                "location": a.get("description", ""),
                "closing": "",
                "url": f"https://ikman.lk/en/ad/{a['slug']}",
                "source": "ikman",
            })
    return out


def src_linkedin():
    out = []
    for q in union("linkedin_queries"):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote_plus(q)}&location=Sri%20Lanka&f_TPR=r604800&start=0")
        r = S.get(url, timeout=30)
        if r.status_code != 200:
            log(f"linkedin '{q}' -> HTTP {r.status_code}")
            time.sleep(10)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("div.base-card"):
            urn = card.get("data-entity-urn", "")
            jid = urn.rsplit(":", 1)[-1]
            t = card.select_one(".base-search-card__title")
            c = card.select_one(".base-search-card__subtitle")
            loc = card.select_one(".job-search-card__location")
            a = card.select_one("a.base-card__full-link")
            if not (jid and t):
                continue
            out.append({
                "id": f"linkedin:{jid}",
                "title": t.get_text(" ", strip=True),
                "company": c.get_text(" ", strip=True) if c else "",
                "location": loc.get_text(" ", strip=True) if loc else "",
                "closing": "",
                "url": (a["href"].split("?")[0] if a else f"https://www.linkedin.com/jobs/view/{jid}"),
                "source": "LinkedIn",
            })
        time.sleep(4)
    return out


JOBWORDS = re.compile(r"engineer|trainee|intern|graduate|vacanc|career|opportunit|executive|officer", re.I)


def src_career_pages():
    """Company career pages have no common format, so collect the job-like
    text lines and links on each page; a line not seen before is an alert."""
    out = []
    for name, url in union("career_pages"):
        r = S.get(url, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            t.decompose()
        seen_here = set()
        for el in soup.find_all(["a", "h1", "h2", "h3", "h4", "li", "td"]):
            txt = el.get_text(" ", strip=True)
            if not (8 <= len(txt) <= 120) or not JOBWORDS.search(txt) or txt in seen_here:
                continue
            seen_here.add(txt)
            link = el.get("href") if el.name == "a" else None
            if link and link.startswith("/"):
                link = url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + link
            out.append({
                "id": "career:" + hashlib.sha1(f"{name}|{txt}".encode()).hexdigest()[:16],
                "title": txt,
                "company": name,
                "location": "",
                "closing": "",
                "url": link if link and link.startswith("http") else url,
                "source": f"{name} careers page",
                "career": True,
            })
    return out


SOURCES = [("topjobs", src_topjobs), ("xpress", src_xpress), ("ikman", src_ikman),
           ("linkedin", src_linkedin), ("career pages", src_career_pages)]
if os.environ.get("GITHUB_ACTIONS"):
    # xpress.jobs answers 403 to GitHub's cloud servers, so it only works from a home connection
    SOURCES = [s for s in SOURCES if s[0] != "xpress"]


# ---------- telegram ----------

def tg(method, **params):
    tok = ENV.get("TELEGRAM_TOKEN")
    if not tok:
        raise SystemExit("TELEGRAM_TOKEN missing in .env")
    r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=params, timeout=30)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=params, timeout=30)
    r.raise_for_status()
    return r.json()


def chats_for(env_names):
    ids = []
    for n in env_names:
        for c in ENV.get(n, "").split(","):
            if c.strip() and c.strip() not in ids:
                ids.append(c.strip())
    return ids


def send(text, env_names=("CHAT_YOHAN", "CHAT_PAVANI")):
    for chat in chats_for(env_names):
        if chat:
            tg("sendMessage", chat_id=chat.strip(), text=text, parse_mode="HTML",
               disable_web_page_preview="true")
            time.sleep(1.1)


def fmt(job, level, label):
    e = html.escape
    star = " ⭐" if level == "strong" else ""
    lines = [f"<b>{e(label)}</b>{star}", f"<b>{e(job['title'])}</b>"]
    if job.get("company"):
        lines.append(f"\U0001f3e2 {e(job['company'])}")
    if job.get("location"):
        lines.append(f"\U0001f4cd {e(job['location'])}")
    if job.get("closing"):
        lines.append(f"⏳ Closes: {e(job['closing'])}")
    lines.append(f"\U0001f517 <a href=\"{e(job['url'])}\">Open on {e(job['source'])}</a>")
    return "\n".join(lines)


# ---------- state ----------

def load_seen():
    st = {}
    if SEEN_FILE.exists():
        st = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    st.setdefault("sent", {})
    st.setdefault("errors", {})
    return st


def save_seen(st):
    # forget entries older than 60 days so the file stays small
    cutoff = time.time() - 60 * 86400
    st["sent"] = {p: {k: v for k, v in d.items() if v > cutoff} for p, d in st["sent"].items()}
    SEEN_FILE.write_text(json.dumps(st), encoding="utf-8")


# ---------- main ----------

def run(dry=False):
    st = load_seen()
    now = time.time()
    jobs, checked = [], 0
    for name, fn in SOURCES:
        try:
            got = fn()
            jobs += got
            checked += len(got)
            st["errors"].pop(name, None)
            log(f"{name}: {len(got)} listings")
        except Exception as ex:
            n = st["errors"].get(name, 0) + 1
            st["errors"][name] = n
            log(f"{name}: ERROR {ex!r}")
            traceback.print_exc()
            if n == 6 and not dry:  # about 3 hours of failures at 30 min polling
                send(f"⚠️ Job bot: source <b>{html.escape(name)}</b> has failed 6 runs in a row "
                     "(site layout may have changed).", ("CHAT_YOHAN",))

    cap = CFG["max_alerts_per_run"]
    total = 0
    for prof in PROFILES:
        if not chats_for(prof.send_to[:1]):
            log(f"{prof.key}: no chat id set ({prof.send_to[0]}), skipped")
            continue  # wait until this person has started the bot, so they get the intro
        sent = st["sent"].setdefault(prof.key, {})
        intro = not sent  # first run for this person: send what is open now
        matches = []
        for j in jobs:
            # dedupe on id and on title+company (same job posted on several sites)
            k = "t:" + re.sub(r"\W+", "", (j["title"] + j.get("company", "")).lower())
            if j["id"] in sent or k in sent:
                continue
            lvl = prof.classify(j)
            if not lvl:
                continue
            sent[j["id"]] = sent[k] = now
            if j.get("career") and intro:
                continue  # career pages: the first run only records the baseline
            matches.append((j, lvl))
        matches.sort(key=lambda x: x[1] != "strong")
        total += len(matches)
        log(f"{prof.key}: {len(matches)} new matches")
        if dry:
            for j, lvl in matches:
                print(f"[{prof.key}/{lvl}] {j['title']} | {j.get('company')} | {j['source']} | {j['url']}")
            continue
        if intro and matches:
            send(f"👋 <b>{html.escape(prof.label)}: job alerts are live.</b> Here are the "
                 f"{min(len(matches), cap)} matching roles open right now. After this, only new ones.",
                 prof.send_to)
        for j, lvl in matches[:cap]:
            send(fmt(j, lvl, prof.label), prof.send_to)
        if len(matches) > cap:
            send(f"{html.escape(prof.label)}: ... and {len(matches) - cap} more matches this round.",
                 prof.send_to)
    log(f"checked {checked}, alerts {total}")
    if dry:
        return

    st["today_count"] = st.get("today_count", 0) + total
    today = datetime.now(SL_TZ).strftime("%Y-%m-%d")
    if datetime.now(SL_TZ).hour >= CFG["daily_summary_hour"] and st.get("last_summary") != today:
        broken = ", ".join(st["errors"]) or "none"
        send(f"📊 Daily check-in: job bot is running. {st['today_count']} alerts today. "
             f"Sources with errors: {html.escape(broken)}.")
        st["last_summary"] = today
        st["today_count"] = 0
    save_seen(st)


def setup():
    res = tg("getUpdates")
    chats = {}
    for u in res.get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat", {})
        if c:
            chats[c["id"]] = c.get("first_name") or c.get("title") or ""
    if not chats:
        print("No messages found. Open the bot in Telegram, press Start / send 'hi', then run --setup again.")
        return
    for cid, nm in chats.items():
        print(f"chat id {cid}  ({nm})")
    print("Put each person's chat id in .env as CHAT_YOHAN=... and CHAT_PAVANI=...")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--setup" in a:
        setup()
    elif "--test" in a:
        send("✅ Job bot test message. Alerts will arrive here.")
        print("sent")
    else:
        run(dry="--dry" in a)
