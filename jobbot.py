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

import fit
import sources_extra as extra

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
    env = {k: v for k, v in os.environ.items() if k.startswith(("TELEGRAM_", "CHAT_", "GEMINI_"))}
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
        self.caution = _rx(p.get("caution_words", []))
        self.priority = _rx(p.get("priority_keywords", []))

    def classify(self, job):
        """Return 'top', 'strong', 'general', 'caution' or None for this person."""
        if job.get("career") and job["company"] not in self.career_names:
            return None
        title = job["title"].lower()
        comp = job.get("company", "").lower()
        where = (job.get("location", "") + " " + title).lower()
        if any_hit(self.excl, title) or any_hit(EXCL_LOC, where):
            return None
        lvl = None
        if job.get("career"):
            # a new job-like line on a company careers page: alert unless excluded above
            return "top" if any_hit(self.priority, title) else "strong"
        if any_hit(self.priority, title):
            lvl = "top"  # this person's first-choice field (e.g. datacom for Pavani)
        elif any_hit(self.strong, title) or (any_hit(self.companies, comp) and any_hit(self.role, title)):
            lvl = "strong"
        elif any_hit(self.general, title):
            lvl = "general"
        if lvl and any_hit(self.caution, title):
            lvl = "caution"  # still sent, but last and flagged (site visits / shifts likely)
        return lvl


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
        url = f"https://www.topjobs.lk/applicant/vacancybyfunctionalarea.jsp?FA={fa}"
        for attempt in range(3):  # topjobs is sometimes slow in the evening
            try:
                r = S.get(url, timeout=60)
                break
            except requests.exceptions.RequestException:
                if attempt == 2:
                    raise
                time.sleep(10)
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


LI_STATE = {"offset": 0}


def src_linkedin():
    # LinkedIn rate-limits cloud IPs, so each run does a rotating slice of the
    # searches; with "past week" results nothing is missed between slices.
    out = []
    qs = union("linkedin_queries")
    n = CFG.get("linkedin_per_run", 8)
    start = LI_STATE["offset"] % len(qs)
    batch = (qs + qs)[start:start + n]
    LI_STATE["offset"] = start + n
    for q in batch:
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote_plus(q)}&location=Sri%20Lanka&f_TPR=r604800&start=0")
        r = S.get(url, timeout=30)
        if r.status_code != 200:
            log(f"linkedin '{q}' -> HTTP {r.status_code}")
            time.sleep(15)
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
        time.sleep(6)
    return out


JOBWORDS = re.compile(r"\b(engineer|executive|officer|manager|analyst|specialist|intern|trainee|associate|"
                      r"technician|consultant|administrator|developer|lead|coordinator|architect)\b", re.I)
NOT_JOB = re.compile(r"@|https?:|resume|degree in|diploma in|qualification|identify|generate|responsib", re.I)


DIALOG_PORTAL = "https://hcmcloud.dialog.lk/CareerPortal/Careers?q=bEopnWmcv9llMiBG3zygOw%3D%3D"


def src_dialog():
    """Dialog's own career portal (MiHCM): every vacancy with closing date and description."""
    r = S.get(DIALOG_PORTAL, timeout=40)
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for d in soup.select("div.hcm-jpp-job"):
        name = d.select_one(".hcm-jpp-name")
        if not name:
            continue
        loc = d.select_one(".hcm-jpp-location")
        close = d.select(".hcm-jpp-closing_date span")
        desc = d.find("input", id="hcm-jpp-description")
        text = BeautifulSoup(desc["value"], "html.parser").get_text(" ", strip=True) if desc else ""
        out.append({
            "id": f"dialog:{d.get('data-rrf-code') or d.get('data-id')}",
            "title": name.get_text(" ", strip=True),
            "company": (d.select_one(".hcm-jpp-company") or name).get_text(" ", strip=True),
            "location": loc.get_text(" ", strip=True) if loc else "",
            "closing": close[-1].get_text(strip=True) if close else "",
            "url": DIALOG_PORTAL,
            "source": "Dialog careers",
            "desc": text,
        })
    return out


def src_mobitel():
    """mobitel.lk/careers lists each vacancy as a heading followed by the advert image."""
    r = S.get("https://www.mobitel.lk/careers", timeout=40)
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for h in soup.find_all("h6"):
        title = h.get_text(" ", strip=True)
        img = h.find_next("img")
        if not title or not img or not img.get("src"):
            continue
        src = requests.compat.urljoin("https://www.mobitel.lk/", img["src"])
        out.append({
            "id": "mobitel:" + hashlib.sha1(title.lower().encode()).hexdigest()[:16],
            "title": title.title(),
            "company": "SLT-Mobitel (Mobitel)",
            "location": "",
            "closing": "",
            "url": src,  # the advert image itself is the job post
            "source": "Mobitel careers",
            "image_url": src,
        })
    return out


def src_slt():
    """careers.slt.lk: SLT-Mobitel's application portal, one link per open intake."""
    r = S.get("https://careers.slt.lk/", timeout=40)
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a.joblink"):
        title = a.get_text(" ", strip=True)
        out.append({
            "id": "slt:" + hashlib.sha1(a.get("href", title).encode()).hexdigest()[:16],
            "title": title.title(),
            "company": "SLT-Mobitel (SLT)",
            "location": "",
            "closing": "",
            "url": requests.compat.urljoin("https://careers.slt.lk/", a.get("href", "")),
            "source": "SLT careers",
        })
    return out


def src_career_pages():
    """Company career pages have no common format, so collect the job-like
    text lines and links on each page; a line not seen before is an alert."""
    out = []
    for name, url in union("career_pages"):
        try:
            r = S.get(url, timeout=30)
        except requests.exceptions.RequestException as ex:
            log(f"career page {name}: {type(ex).__name__}, skipped this run")
            continue  # one broken site must not stop the other career pages
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            t.decompose()
        seen_here = set()
        for el in soup.find_all(["a", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "strong", "p", "span"]):
            txt = el.get_text(" ", strip=True)
            if not (8 <= len(txt) <= 90) or not JOBWORDS.search(txt) or NOT_JOB.search(txt) or txt in seen_here:
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
           ("linkedin", src_linkedin), ("dialog", src_dialog), ("mobitel", src_mobitel),
           ("slt", src_slt), ("career pages", src_career_pages),
           ("gazette", lambda: extra.gazette(S)),
           ("workday", lambda: extra.workday(S, CFG.get("workday_portals", {}))),
           ("wso2", lambda: extra.wso2(S)), ("ericsson", lambda: extra.ericsson(S))]
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


LISTING_SOURCES = {"Dialog careers", "Mobitel careers", "SLT careers", "LSEG careers", "WSO2 careers",
                   "Ericsson careers"}


def is_listing_page(job):
    return job.get("career") or job["source"] in LISTING_SOURCES


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


def fmt(job, level, label, f=None):
    e = html.escape
    star = {"top": " ⭐⭐", "strong": " ⭐", "caution": " 🚧"}.get(level, "")
    lines = [f"<b>{e(label)}</b>{star}", f"<b>{e(job['title'])}</b>"]
    if job.get("company"):
        lines.append(f"\U0001f3e2 {e(job['company'])}")
    if job.get("location"):
        lines.append(f"\U0001f4cd {e(job['location'])}")
    if job.get("closing"):
        lines.append(f"⏳ Closes: {e(job['closing'])}")
    if f:
        lines.append(e(fit.fit_line(f)))
    if level == "caution":
        lines.append("🚧 May involve site visits, field work or shifts. Check the ad.")
    lines.append(f"\U0001f517 <a href=\"{e(job['url'])}\">Open the job post ({e(job['source'])})</a>")
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
    q = CFG.get("quiet_hours")
    if q and q["from"] <= datetime.now(SL_TZ).hour < q["to"] and not dry:
        log("quiet hours, skipping this run")
        return  # nobody reads alerts at night; jobs posted now are picked up after quiet hours
    st = load_seen()
    LI_STATE["offset"] = st.get("li_offset", 0)
    now = time.time()
    jobs, checked = [], 0
    base_done = set(st.get("base_done", []))
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

    sources_ok = {j["source"] for j in jobs}
    cap = CFG["max_alerts_per_run"]
    total = 0
    for prof in PROFILES:
        if not chats_for(prof.send_to[:1]):
            log(f"{prof.key}: no chat id set ({prof.send_to[0]}), skipped")
            continue  # wait until this person has started the bot, so they get the intro
        sent = st["sent"].setdefault(prof.key, {})
        intro = not sent  # first run for this person: send what is open now
        matches = []
        digest = {}
        for j in jobs:
            # dedupe on id and on title+company (same job posted on several sites)
            k = "t:" + re.sub(r"\W+", "", (j["title"] + j.get("company", "")).lower())
            if j["id"] in sent or k in sent:
                continue
            lvl = prof.classify(j)
            if not lvl:
                continue
            sent[j["id"]] = sent[k] = now
            if is_listing_page(j) and f"{prof.key}|{j['source']}" not in base_done:
                # first check of a company careers page: its old adverts go in one summary
                digest.setdefault(j["source"], []).append((j, lvl))
                continue
            matches.append((j, lvl))
        matches.sort(key=lambda x: {"top": -1, "strong": 0, "general": 1, "caution": 2}[x[1]])
        for src in sources_ok:
            if src in LISTING_SOURCES or src.endswith("careers page"):
                base_done.add(f"{prof.key}|{src}")
        if not dry:
            for src, items in digest.items():
                items.sort(key=lambda x: {"top": -1, "strong": 0, "general": 1, "caution": 2}[x[1]])
                lines = [f"<b>{html.escape(prof.label)}</b>",
                         f"📋 Roles already listed on <b>{html.escape(src)}</b> (first check, so this "
                         "can include older adverts). From now on only new ones are alerted:"]
                for j, lvl in items[:25]:
                    star = "⭐⭐ " if lvl == "top" else ""
                    lines.append(f"• {star}<a href=\"{html.escape(j['url'])}\">{html.escape(j['title'])}</a>")
                send("\n".join(lines), prof.send_to)
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
        top = matches[:cap]
        gcfg = CFG["gemini"]
        items = [(j, *fit.describe(S, j, gcfg["max_desc_chars"])) for j, _ in top]
        fits = fit.score(CFG["profiles"][prof.key], items, st, ENV.get("GEMINI_API_KEY"), gcfg,
                         lambda m: send(m, ("CHAT_YOHAN",)), log)
        order = {"top": -1, "strong": 0, "general": 1, "caution": 2}
        for (j, lvl), f in sorted(zip(top, fits), key=lambda x: (order[x[0][1]], -x[1]["fit"])):
            send(fmt(j, lvl, prof.label, f), prof.send_to)
        if len(matches) > cap:
            send(f"{html.escape(prof.label)}: ... and {len(matches) - cap} more matches this round.",
                 prof.send_to)
    st["base_done"] = sorted(base_done)
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
    st["li_offset"] = LI_STATE["offset"]
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
    elif "--fit-test" in a:
        # score two real ads with Gemini and send them to Yohan, to check the key works
        samples = [("yohan", {"id": "linkedin:4477036862", "title": "DevOps Engineer - AI", "company": "IFS",
                              "location": "Colombo", "url": "https://lk.linkedin.com/jobs/view/4477036862",
                              "source": "LinkedIn"}),
                   ("pavani", {"id": "topjobs:0001559455", "title": "Engineer - Enterprise Fiber Planning",
                               "company": "Hutch", "location": "Colombo 4",
                               "url": "https://www.topjobs.lk/employer/JobAdvertismentServlet?ac=DEFZZZ&jc=0001559455&ec=DEFZZZ",
                               "source": "topjobs"})]
        st = {}
        for key, j in samples:
            prof = next(p for p in PROFILES if p.key == key)
            item = (j, *fit.describe(S, j, CFG["gemini"]["max_desc_chars"]))
            f = fit.score(CFG["profiles"][key], [item], st, ENV.get("GEMINI_API_KEY"), CFG["gemini"],
                          lambda m: send(m, ("CHAT_YOHAN",)), log)[0]
            log(f"fit-test {key}: {f}")
            send(fmt(j, "strong", prof.label, f) + "\n<i>(fit test)</i>", ("CHAT_YOHAN",))
    elif "--test" in a:
        send("✅ Job bot test message. Alerts will arrive here.")
        print("sent")
    else:
        run(dry="--dry" in a)
