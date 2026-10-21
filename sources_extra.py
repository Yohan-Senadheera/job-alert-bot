"""Extra job sources: government vacancies (gazette.lk), company portals on
Workday (LSEG, Sysco LABS) and SuccessFactors (GTN), WSO2's careers page and
Ericsson's Sri Lanka openings."""
import hashlib
import html
import re
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

SPLIT_ROLES = re.compile(r"\s*(?:,|/|&| and )\s*")


def gazette(session):
    """gazette.lk (WordPress) posts titles like 'Engineer, Driver - Org Vacancies 2026'.
    Each role in the title becomes its own job so the filters can judge it."""
    r = session.get("https://www.gazette.lk/wp-json/wp/v2/posts",
                    params={"per_page": 60, "_fields": "id,link,title"}, timeout=40)
    out = []
    for p in r.json():
        title = html.unescape(p["title"]["rendered"])
        title = re.sub(r"\s*(Job\s+)?Vacancies\s*\d{4}.*$", "", title, flags=re.I)
        roles, _, org = title.partition(" – ")
        if not org:
            roles, _, org = title.partition(" - ")
        if not org:
            continue  # titles without "role - organisation" are news or exam posts, not vacancies
        for role in SPLIT_ROLES.split(roles):
            role = role.strip()
            if len(role) < 4:
                continue
            out.append({
                "id": f"gazette:{p['id']}:{role.lower()}",
                "title": role,
                "company": org.strip(),
                "location": "",
                "closing": "",
                "url": p["link"],
                "source": "gazette.lk (govt & public)",
            })
    return out


def workday(session, portals):
    """Workday career sites share one JSON API: /wday/cxs/<tenant>/<site>/jobs."""
    out = []
    for name, (host, tenant, site) in portals.items():
        posts = []
        for offset in range(0, 100, 20):  # 20 is the most one call returns
            r = session.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                             json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "Sri Lanka"},
                             headers={"Accept": "application/json"}, timeout=40)
            page = r.json().get("jobPostings", [])
            posts += page
            if len(page) < 20:
                break
        for j in posts:
            if "sri lanka" not in (j.get("locationsText", "") + j.get("externalPath", "")).lower():
                continue
            out.append({
                "id": f"workday:{tenant}:{j['externalPath']}",
                "title": j["title"],
                "company": name,
                "location": j.get("locationsText", ""),
                "closing": "",
                # myworkdaysite.com hosts put job pages under /recruiting/<tenant>/<site>
                "url": (f"https://{host}/recruiting/{tenant}/{site}{j['externalPath']}"
                        if host.endswith("myworkdaysite.com") else f"https://{host}/en-US/{site}{j['externalPath']}"),
                "source": f"{name} careers",
            })
    return out


def successfactors(session, portals):
    """SuccessFactors career sites give every open job as XML with resultType=XML."""
    out = []
    for name, (host, company) in portals.items():
        r = session.get(f"https://{host}/career",
                        params={"company": company, "career_ns": "job_listing_summary", "resultType": "XML"},
                        timeout=40)
        for j in ET.fromstring(r.content).findall("Job"):
            req = j.findtext("ReqId", "").strip()
            out.append({
                "id": f"sapsf:{company}:{req}",
                "title": html.unescape(j.findtext("JobTitle", "").strip()),
                "company": name,
                "location": "",
                "closing": "",
                "url": f"https://{host}/career?company={company}&career_ns=job_listing"
                       f"&navBarLevel=JOB_SEARCH&career_job_req_id={req}",
                "source": f"{name} careers",
            })
    return out


def wso2(session):
    r = session.get("https://wso2.com/careers/", timeout=40)
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for col in soup.select("div.pd-card-col"):
        if "sri lanka" not in col.get("data-pd-location", "").lower():
            continue
        h = col.find("h5")
        a = col.find("a", href=True)
        if not (h and a):
            continue
        out.append({
            "id": "wso2:" + hashlib.sha1(a["href"].encode()).hexdigest()[:16],
            "title": h.get_text(" ", strip=True),
            "company": "WSO2",
            "location": "Sri Lanka",
            "closing": "",
            "url": requests.compat.urljoin("https://wso2.com/", a["href"]),
            "source": "WSO2 careers",
        })
    return out


def ericsson(session):
    out = []
    for start in (0, 10, 20):
        r = session.get("https://jobs.ericsson.com/api/pcsx/search",
                        params={"domain": "ericsson.com", "query": "", "location": "Sri Lanka", "start": start},
                        timeout=40)
        pos = (r.json().get("data") or {}).get("positions") or []
        for p in pos:
            locs = ", ".join(p.get("locations", []))
            if "sri lanka" not in locs.lower():
                continue
            out.append({
                "id": f"ericsson:{p['id']}",
                "title": p["name"],
                "company": "Ericsson",
                "location": locs,
                "closing": "",
                "url": requests.compat.urljoin("https://jobs.ericsson.com/", p.get("positionUrl", "")),
                "source": "Ericsson careers",
            })
        if len(pos) < 10:
            break
    return out
