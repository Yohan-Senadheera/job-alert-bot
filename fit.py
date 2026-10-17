"""Fit scoring for job alerts.

Gemini (free tier) reads each matched job's ad and rates it against the
person's profile. Jobs are sent in batches with short outputs so one API call
covers several alerts. If the quota is used up or the API fails, a keyword
score based on the profile's skills is used instead, and Telegram is told
once when the quota runs out and once when it is back.
"""
import base64
import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d{0,2}\s*\+?\s*years?", re.I)


# ---------- job description text / image ----------

REQ = re.compile(r"requirement|qualification|what you|you will need|you have|skills|experience|responsibilit", re.I)


def focus(text, n):
    """Cut the ad down to n characters, starting where the requirements begin
    (ads often open with a long company introduction)."""
    m = REQ.search(text)
    if m and m.start() > n // 3:
        text = text[m.start():]
    return text[:n]


def describe(session, job, max_chars):
    """Return (text, image) for a job; either may be empty. Never raises."""
    max_chars *= 4  # keyword scoring reads the whole ad; Gemini gets focus()
    try:
        src = job["source"]
        if src == "LinkedIn":
            jid = job["id"].split(":", 1)[1]
            r = session.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}", timeout=20)
            d = BeautifulSoup(r.text, "html.parser").select_one(".show-more-less-html__markup")
            return (d.get_text(" ", strip=True)[:max_chars] if d else ""), None
        if src == "topjobs":
            r = session.get(job["url"], timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
            for t in soup(["script", "style"]):
                t.decompose()
            text = soup.get_text(" ", strip=True)
            # most topjobs ads are a single image under /logo/<employer>/
            for img in soup.find_all("img"):
                s = img.get("src", "")
                if "/logo/" in s and "small" not in s:
                    url = requests.compat.urljoin(job["url"], s)
                    im = session.get(url, timeout=20)
                    if im.ok and len(im.content) < 1_500_000:
                        mime = im.headers.get("content-type", "image/png").split(";")[0]
                        return "", (mime, im.content)
            return text[:max_chars], None
        if src == "ikman":
            r = session.get(job["url"], timeout=20)
            m = re.search(r'"description":"(.*?)","', r.text)
            return (json.loads(f'"{m.group(1)}"')[:max_chars] if m else ""), None
        if src == "xpress.jobs":
            return "", None
    except Exception:
        pass
    return "", None


# ---------- keyword fallback ----------

def keyword_fit(prof_cfg, job, text):
    hay = (job["title"] + " " + text).lower()
    hits = [s for s in prof_cfg["skills"]
            if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", hay)]
    score = min(10, 3 + len(hits))
    gap = ""
    yrs = [int(m.group(1)) for m in YEARS.finditer(text)]
    if yrs and min(yrs) >= 2:
        gap = f"asks {min(yrs)}+ yrs"
        score = max(1, score - 3)
    why = ("matches " + ", ".join(hits[:4])) if hits else "title match only (no ad text)"
    return {"fit": score, "why": why, "gap": gap, "by": "keywords"}


# ---------- gemini ----------

def _next_reset():
    # free-tier daily quota resets at midnight US Pacific time (about 08:00 UTC)
    now = datetime.now(timezone.utc)
    r = now.replace(hour=8, minute=0, second=0, microsecond=0)
    return (r if r > now else r + timedelta(days=1)).timestamp()


class QuotaOut(Exception):
    pass


def _gemini_call(key, models, parts, n):
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 70 * n + 40,
                             "responseMimeType": "application/json"},
    }
    last = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        for attempt in range(2):
            r = requests.post(url, json=body, timeout=60)
            if r.status_code == 429:
                if "PerDay" in r.text or "per day" in r.text.lower():
                    raise QuotaOut(model)
                time.sleep(35)  # per-minute limit: wait and retry once
                continue
            break
        if r.status_code in (404, 400) and ("not found" in r.text.lower() or "not supported" in r.text.lower()):
            last = f"{model}: {r.status_code}"
            continue  # model retired or renamed: try the next one
        if r.status_code == 429:
            raise RuntimeError("per-minute rate limit")
        r.raise_for_status()
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt)
    raise RuntimeError(f"no Gemini model worked ({last})")


def score(prof_cfg, items, st, key, gcfg, notify, log):
    """items: list of (job, text, image). Returns a list of fit dicts, same order."""
    out = [keyword_fit(prof_cfg, j, t) for j, t, _ in items]
    g = st.setdefault("gemini", {})
    if not key:
        return out
    if g.get("blocked_until", 0) > time.time():
        return out
    if g.get("blocked_until"):
        g.pop("blocked_until")
        notify("\U0001f50b Gemini quota is back. Fit scores use AI again.")
    bs = gcfg["batch_size"]
    for start in range(0, len(items), bs):
        chunk = items[start:start + bs]
        parts = [{"text": (
            "Rate how well each job suits this candidate. Candidate: " + prof_cfg["summary"] +
            "\nReply as a JSON array, one object per job: "
            '{"i": job number, "fit": 0-10, "why": "max 12 words", "gap": "max 8 words, or empty"}. '
            "Be strict about required years of experience and seniority. Jobs:")}]
        for k, (j, text, img) in enumerate(chunk, 1):
            head = f"\nJob {k}: {j['title']} | {j.get('company', '')} | {j.get('location', '')}\n"
            body = focus(text, gcfg["max_desc_chars"]) if text else (
                "(ad is the image below)" if img else "(no ad text)")
            parts.append({"text": head + body})
            if img:
                parts.append({"inline_data": {"mime_type": img[0],
                                              "data": base64.b64encode(img[1]).decode()}})
        try:
            res = _gemini_call(key, gcfg["models"], parts, len(chunk))
            for row in res:
                i = int(row.get("i", 0)) - 1
                if 0 <= i < len(chunk):
                    out[start + i] = {"fit": int(row.get("fit", 0)), "why": str(row.get("why", ""))[:100],
                                      "gap": str(row.get("gap", ""))[:60], "by": "AI"}
        except QuotaOut:
            g["blocked_until"] = _next_reset()
            notify("\U0001faab Gemini free quota is used up for today. Fit scores use keyword matching "
                   "until about 1:30 pm Sri Lanka time.")
            log("gemini: daily quota used up")
            break
        except Exception as ex:
            log(f"gemini: error {ex!r}, using keyword scores for this batch")
        time.sleep(4)
    return out


def fit_line(f):
    tag = "" if f["by"] == "AI" else " (keyword)"
    line = f"\U0001f3af Fit {f['fit']}/10{tag}: {f['why']}"
    if f.get("gap"):
        line += f"\n⚠️ {f['gap']}"
    return line
