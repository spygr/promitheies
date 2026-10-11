#!/usr/bin/env python3
"""Καθημερινή αναζήτηση προκηρύξεων: ΚΗΜΔΗΣ (ΕΣΗΔΗΣ open data), Διαύγεια και προσαρμοσμένα sites.

Τρέχει τοπικά:   python scraper/scrape.py [--debug]
Γράφει το:       docs/data/notices.json
"""
import argparse
import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
KEYWORDS_FILE = ROOT / "keywords.json"
SOURCES_FILE = ROOT / "sources.json"
OUT_FILE = ROOT / "docs" / "data" / "notices.json"

TIMEOUT = 45
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (promithies-radar; personal use)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
})

DEBUG = False


# ---------------------------------------------------------------- κείμενο

def norm(s):
    """Πεζά, χωρίς τόνους/διαλυτικά, τελικό ς -> σ."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def compile_kw(kw):
    """'φωτοβολταϊκ*' -> regex. Όλες οι λέξεις πρέπει να υπάρχουν με τη σειρά, ως ολόκληρες λέξεις
    (το * στο τέλος λέξης σημαίνει 'οτιδήποτε μετά')."""
    words = norm(kw).split()
    if not words:
        return None
    parts = []
    for w in words:
        if w.endswith("*"):
            parts.append(re.escape(w[:-1]) + r"\w*")
        else:
            parts.append(re.escape(w))
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)")


def matcher(keywords):
    compiled = [(k, compile_kw(k)) for k in keywords]
    compiled = [(k, rx) for k, rx in compiled if rx]

    def match(text):
        t = norm(text)
        return [k for k, rx in compiled if rx.search(t)]

    return match


def search_terms(keywords, hints):
    """Όροι που στέλνονται στα API (τα * αφαιρούνται, εκτός αν υπάρχει υπόδειξη)."""
    terms = []
    for k in keywords:
        if k in hints:
            terms.extend(hints[k])
        else:
            t = k.replace("*", "").strip()
            if t:
                terms.append(t)
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# ---------------------------------------------------------------- βοηθητικά

def parse_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        try:
            return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1]))
    return None


def pick(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def as_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return as_text(pick(v, "name", "value", "label", "title", "legalName") or "")
    if isinstance(v, list):
        return ", ".join(as_text(x) for x in v if x)
    return str(v)


def dump_debug(name, payload):
    if DEBUG:
        print(f"[debug] {name}: {json.dumps(payload, ensure_ascii=False)[:1500]}", file=sys.stderr)


# ---------------------------------------------------------------- ΚΗΜΔΗΣ

def fetch_kimdis(cfg, terms, since, match):
    out = []
    base = cfg["base_url"].rstrip("/")
    for endpoint in cfg.get("endpoints", ["notice"]):
        for term in terms:
            for page in range(cfg.get("max_pages", 3)):
                body = {"title": term}
                body.update(cfg.get("request_extra", {}))
                r = SESSION.post(f"{base}/{endpoint}", params={"page": page}, json=body, timeout=TIMEOUT)
                r.raise_for_status()
                data = r.json()
                if page == 0:
                    dump_debug(f"kimdis/{endpoint}/{term}", data)
                items = data.get("content") if isinstance(data, dict) else data
                if not items:
                    break
                for it in items:
                    title = as_text(pick(it, "title", "subject", "name"))
                    published = parse_date(pick(it, "publishedDate", "publishDate", "submissionDate",
                                                "dateCreated", "createdDate", "date"))
                    if published and published < since:
                        continue
                    matched = match(title)
                    if not matched:
                        continue
                    ref = as_text(pick(it, "referenceNumber", "adam", "id", "reference"))
                    link = as_text(pick(it, "documentUrl", "url", "link")) or \
                        "https://cerpp.eprocurement.gov.gr/upgkimdis/unprotected/home.xhtml"
                    out.append({
                        "id": f"kimdis:{endpoint}:{ref or title[:80]}",
                        "source": "ΚΗΜΔΗΣ",
                        "title": title,
                        "org": as_text(pick(it, "organization", "organizationName", "contractingAuthority", "authority")),
                        "date": published.isoformat() if published else None,
                        "url": link,
                        "ref": ref,
                        "matched": matched,
                    })
                if len(items) < 10:
                    break
    return out


# ---------------------------------------------------------------- Διαύγεια

def fetch_diavgeia(cfg, terms, since, today, match):
    out = []
    base = cfg["base_url"].rstrip("/")
    must = matcher(cfg.get("subject_must_contain_any", []))
    need_filter = bool(cfg.get("subject_must_contain_any"))
    org_cache = {}

    def org_name(uid):
        if not uid:
            return ""
        if uid not in org_cache:
            try:
                r = SESSION.get(f"{base}/organizations/{uid}.json", timeout=TIMEOUT)
                org_cache[uid] = r.json().get("label", uid) if r.ok else uid
            except Exception:
                org_cache[uid] = uid
        return org_cache[uid]

    for term in terms:
        q = f'"{term}"' if " " in term else term
        for page in range(cfg.get("max_pages", 5)):
            r = SESSION.get(f"{base}/search.json", params={
                "q": q,
                "from_issue_date": since.isoformat(),
                "to_issue_date": today.isoformat(),
                "size": 100,
                "page": page,
            }, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if page == 0:
                dump_debug(f"diavgeia/{term}", data)
            decisions = data.get("decisions", [])
            for d in decisions:
                subject = as_text(d.get("subject"))
                matched = match(subject)
                if not matched:
                    continue
                if need_filter and not must(subject):
                    continue
                issued = parse_date(d.get("issueDate") or d.get("submissionTimestamp"))
                ada = d.get("ada", "")
                out.append({
                    "id": f"diavgeia:{ada or d.get('protocolNumber') or subject[:80]}",
                    "source": "Διαύγεια",
                    "title": subject,
                    "org": org_name(d.get("organizationId")),
                    "date": issued.isoformat() if issued else None,
                    "url": d.get("documentUrl") or (f"https://diavgeia.gov.gr/decision/view/{ada}" if ada else ""),
                    "ref": ada,
                    "matched": matched,
                })
            if len(decisions) < 100:
                break
    return out


# ---------------------------------------------------------------- τυχαία sites

def fetch_site(site, match):
    r = SESSION.get(site["url"], timeout=TIMEOUT)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if len(text) < 8:
            continue
        matched = match(text)
        if not matched:
            continue
        url = urljoin(site["url"], a["href"])
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "id": f"site:{url}",
            "source": site["name"],
            "title": text,
            "org": site["name"],
            "date": None,
            "url": url,
            "ref": "",
            "matched": matched,
        })
    return out


# ---------------------------------------------------------------- κύριο πρόγραμμα

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def run():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="εμφανίζει δείγμα απαντήσεων των API")
    args = ap.parse_args()
    DEBUG = args.debug

    kw_cfg = load_json(KEYWORDS_FILE, {"keywords": []})
    src = load_json(SOURCES_FILE, {})
    keywords = [k for k in kw_cfg.get("keywords", []) if k.strip()]
    terms = search_terms(keywords, kw_cfg.get("search_hints", {}))
    match = matcher(keywords)

    today = dt.date.today()
    since = today - dt.timedelta(days=src.get("days_back", 3))
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    previous = load_json(OUT_FILE, {"notices": []})
    notices = {n["id"]: n for n in previous.get("notices", [])}

    status, fresh = [], []

    def attempt(name, fn):
        try:
            found = fn()
            fresh.extend(found)
            status.append({"name": name, "ok": True, "count": len(found)})
            print(f"{name}: {len(found)} αποτελέσματα")
        except Exception as e:  # μια πηγή που αποτυγχάνει δεν πρέπει να χαλάει τις υπόλοιπες
            status.append({"name": name, "ok": False, "count": 0, "error": f"{type(e).__name__}: {e}"[:300]})
            print(f"{name}: ΣΦΑΛΜΑ {e}", file=sys.stderr)

    if src.get("kimdis", {}).get("enabled", True):
        attempt("ΚΗΜΔΗΣ", lambda: fetch_kimdis(src["kimdis"], terms, since, match))
    if src.get("diavgeia", {}).get("enabled", True):
        attempt("Διαύγεια", lambda: fetch_diavgeia(src["diavgeia"], terms, since, today, match))
    for site in src.get("sites", []):
        if site.get("enabled", True):
            attempt(site["name"], lambda s=site: fetch_site(s, match))

    new_count = 0
    for n in fresh:
        old = notices.get(n["id"])
        if old:
            old["matched"] = sorted(set(old.get("matched", [])) | set(n["matched"]))
        else:
            n["first_seen"] = now
            notices[n["id"]] = n
            new_count += 1

    cutoff = (today - dt.timedelta(days=src.get("retention_days", 120))).isoformat()
    kept = [n for n in notices.values() if n["first_seen"][:10] >= cutoff]
    kept.sort(key=lambda n: (n["first_seen"], n.get("date") or ""), reverse=True)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "generated": now,
        "keywords": keywords,
        "sources": status,
        "notices": kept,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Νέες: {new_count} | Σύνολο: {len(kept)}")


if __name__ == "__main__":
    run()
