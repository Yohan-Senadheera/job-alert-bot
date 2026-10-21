"""Remote jobs that can be done from Sri Lanka, plus company portals
(careers-page.com, Recruitee, SmartRecruiters) and the 99x careers page.

Remote boards are full of roles that are remote only inside the US or EU,
senior roles and the odd scam, so every remote ad is checked before it can
become an alert:
  - the board must say the job is open worldwide, to APAC/Asia or to Sri Lanka
  - the ad text must not lock the job to another country (citizens only,
    "must be located in the US", security clearance and so on)
  - the title must not be senior and the ad must not ask for more years than
    max_years
  - the ad must not carry scam signs (WhatsApp/Telegram contact, fees,
    personal email addresses, gift cards)
Only established boards are used: Himalayas, Jobicy and We Work Remotely.
"""
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from bs4 import BeautifulSoup

OPEN_TO_SL = re.compile(r"worldwide|anywhere|global|sri lanka|\bapac\b|asia", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|lead|principal|architect|manager|head|director|vp|expert)\b", re.I)
JUNK = re.compile(r"expression of interest|talent (pool|community)|community|freelance|ai trainer|"
                  r"task auditor|tutor|annotat|contract(or)? only|volunteer|unpaid", re.I)
COUNTRY_LOCK = re.compile(
    r"(must|need to|should|required to) (be )?(currently )?(located|based|reside|residing|live|living) in "
    r"(the )?(us\b|u\.s|usa|united states|uk\b|united kingdom|europe|eu\b|canada|latam|latin america|"
    r"mexico|brazil|germany|poland|india|australia|philippines)"
    r"|\b(us|u\.s\.|uk|eu) citizens?\b|citizenship is required|authori[sz]ed to work in the "
    r"(us|u\.s|united states|uk|eu)|security clearance|\bw-?2\b|"
    r"remote (within|in) the (us|u\.s|united states|uk)", re.I)
SCAM = re.compile(r"whatsapp|telegram|signal app|registration fee|training fee|processing fee|"
                  r"upfront (payment|fee)|pay (a|the|for) (fee|training|equipment)|western union|gift card|"
                  r"crypto(currency)? wallet|@(gmail|yahoo|hotmail|outlook)\.com|no interview", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*\+?\s*years?[^.\n]{0,60}?experience", re.I)


def _text(markup):
    return BeautifulSoup(markup or "", "html.parser").get_text(" ", strip=True)


def checked(title, desc, max_years):
    """Return why this remote ad is rejected, or '' if it passes."""
    if SENIOR.search(title):
        return "senior title"
    if JUNK.search(title):
        return "not a real opening"
    if SCAM.search(desc):
        return "scam signs"
    if COUNTRY_LOCK.search(desc):
        return "locked to another country"
    yrs = [int(m.group(1)) for m in YEARS.finditer(desc)]
    if yrs and min(yrs) > max_years:
        return f"asks {min(yrs)}+ years"
    return ""


def _job(source, jid, title, company, where, url, desc, cfg):
    return {
        "id": f"remote:{source}:{jid}",
        "title": title.strip(),
        "company": company.strip(),
        "location": f"Remote ({where})",
        "closing": "",
        "url": url,
        "source": f"{source} (remote)",
        "desc": desc,
        "only": cfg["for"],  # remote alerts are for this profile only
    }


def himalayas(session, cfg):
    """Himalayas filters by country (LK = jobs that may hire from Sri Lanka) and seniority."""
    out, seen = [], set()
    now = time.time()
    for q in cfg["queries"]:
        r = session.get("https://himalayas.app/jobs/api/search",
                        params={"q": q, "country": "LK", "seniority": "Entry-level,Mid-level"}, timeout=40)
        for j in r.json().get("jobs", []):
            if j["guid"] in seen or now - j.get("pubDate", 0) > cfg["max_age_days"] * 86400:
                continue
            seen.add(j["guid"])
            places = j.get("locationRestrictions") or []
            if places and not any(OPEN_TO_SL.search(p) for p in places):
                continue
            desc = _text(j.get("description"))
            if checked(j["title"], desc, cfg["max_years"]) or not j.get("companyName"):
                continue
            out.append(_job("Himalayas", j["guid"], j["title"], j["companyName"],
                            ", ".join(places) or "worldwide", j.get("applicationLink") or j["guid"], desc, cfg))
    return out


def jobicy(session, cfg):
    out, seen = [], set()
    now = datetime.now(timezone.utc)
    for tag in cfg["jobicy_tags"]:
        r = session.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag}, timeout=40)
        for j in r.json().get("jobs", []):
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            geo = j.get("jobGeo", "")
            if not OPEN_TO_SL.search(geo) or re.search(r"senior|lead|director|manager|executive",
                                                       j.get("jobLevel", ""), re.I):
                continue
            try:
                age = (now - datetime.fromisoformat(j["pubDate"]).replace(tzinfo=timezone.utc)).days
            except (KeyError, ValueError):
                age = 0
            desc = _text(j.get("jobDescription"))
            if age > cfg["max_age_days"] or checked(j["jobTitle"], desc, cfg["max_years"]):
                continue
            out.append(_job("Jobicy", j["id"], html.unescape(j["jobTitle"]), html.unescape(j["companyName"]),
                            geo, j["url"], desc, cfg))
    return out


def weworkremotely(session, cfg):
    r = session.get("https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss", timeout=40)
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        region = it.findtext("region") or ""
        if "anywhere in the world" not in region.lower():
            continue  # "USA Only", "Europe Only" and similar regions are not open to Sri Lanka
        company, _, title = (it.findtext("title") or "").partition(": ")
        desc = _text(it.findtext("description"))
        if not title or checked(title, desc, cfg["max_years"]):
            continue
        url = it.findtext("link") or ""
        out.append(_job("We Work Remotely", url.rsplit("/", 1)[-1], title, company, "worldwide", url, desc, cfg))
    return out


def remote(session, cfg):
    out, seen = [], set()
    block = [b.lower() for b in cfg.get("block_companies", [])]
    for j in himalayas(session, cfg) + jobicy(session, cfg) + weworkremotely(session, cfg):
        # one ad is sometimes posted under two company names; the ad text gives it away
        key = re.sub(r"\W+", "", j["title"].lower()) + re.sub(r"\W+", "", j["desc"][-300:].lower())
        if key in seen or any(b in j["company"].lower() for b in block):
            continue
        seen.add(key)
        out.append(j)
    return out


def careers_page_com(session, portals):
    """careers-page.com boards list 10 jobs a page. Pages are fetched slowly
    because the site rate-limits quick repeat requests."""
    out = []
    for name, base in portals.items():
        for page in range(1, 11):
            if page > 1:
                time.sleep(2)
            r = session.get(base, params={"page": page}, timeout=40)
            if r.status_code == 429 and out:
                break  # keep what was read; the rest is picked up next run
            r.raise_for_status()
            links = BeautifulSoup(r.text, "html.parser").find_all("a", href=re.compile(r"^/jobs/[0-9a-f-]{36}$"))
            if not links:
                break
            for a in links:
                title = a.get_text(" ", strip=True)
                if title:
                    out.append({
                        "id": f"careerspage:{a['href'].rsplit('/', 1)[-1]}",
                        "title": title,
                        "company": name,
                        "location": "Colombo",
                        "closing": "",
                        "url": base.rstrip("/") + a["href"],
                        "source": f"{name} careers",
                    })
    return out


def recruitee(session, portals):
    out = []
    for name, slug in portals.items():
        r = session.get(f"https://{slug}.recruitee.com/api/offers/", timeout=40)
        for o in r.json().get("offers", []):
            out.append({
                "id": f"recruitee:{slug}:{o['id']}",
                "title": o["title"],
                "company": name,
                "location": o.get("location", ""),
                "closing": "",
                "url": o["careers_url"],
                "source": f"{name} careers",
            })
    return out


def smartrecruiters(session, portals):
    out = []
    for name, company in portals.items():
        for offset in range(0, 500, 100):
            r = session.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                            params={"limit": 100, "offset": offset, "country": "lk"}, timeout=40)
            page = r.json().get("content", [])
            for p in page:
                out.append({
                    "id": f"smartrecruiters:{company}:{p['id']}",
                    "title": p["name"].strip(),
                    "company": name,
                    "location": p["location"].get("fullLocation", ""),
                    "closing": "",
                    "url": f"https://jobs.smartrecruiters.com/{company}/{p['id']}",
                    "source": f"{name} careers",
                })
            if len(page) < 100:
                break
    return out


def x99(session):
    """99x lists its Sri Lanka openings as cards (a heading and a 'Read more' link)."""
    r = session.get("https://99x.io/careers/open-positions", params={"location": "sri-lanka"}, timeout=40)
    out = []
    for h in BeautifulSoup(r.text, "html.parser").find_all("h2"):
        card = h.parent
        a = card.find("a", href=re.compile(r"/careers/open-positions/.+/"))
        if not a:
            continue
        out.append({
            "id": "99x:" + a["href"].rstrip("/").rsplit("/", 1)[-1].lower(),
            "title": h.get_text(" ", strip=True),
            "company": "99x",
            "location": "Sri Lanka",
            "closing": "",
            "url": a["href"],
            "source": "99x careers",
        })
    return out
