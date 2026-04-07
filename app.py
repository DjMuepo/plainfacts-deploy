import os
import csv
import io
import re
import time
import hashlib
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import feedparser
import httpx
from dateutil import parser as dtparser
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

APP_VERSION = "0.4.0"

# -----------------------------
# Config
# -----------------------------
RSS_FEEDS = {
    "ProPublica": "https://www.propublica.org/feeds/propublica/main",
}

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
FEDERAL_REGISTER_API = "https://www.federalregister.gov/api/v1/documents.json"
WAYBACK_AVAILABLE = "https://archive.org/wayback/available"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"

# Official .gov domains dataset (CISA / get.gov)
DOTGOV_CSV_URL = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"

CONGRESS_API_BASE = "https://api.congress.gov/v3"
CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY", "")

GOVINFO_API_BASE = "https://api.govinfo.gov"
GOVINFO_API_KEY = os.getenv("GOVINFO_API_KEY", "")

HTTP_TIMEOUT = 20

# Stooq CSV data (stocks, ETFs, indices, crypto)
STOOQ_CSV_BASE = "https://stooq.com/q/d/l/"

# Simple in-memory cache (TTL)
_CACHE: Dict[str, Any] = {}

# Simple per-IP rate limiting for /markets
_RL: Dict[str, Any] = {}

# -----------------------------
# Models
# -----------------------------

class SourceItem(BaseModel):
    source: str
    title: str
    url: str
    published_at: Optional[datetime] = None
    summary: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class ClusterBrief(BaseModel):
    cluster_id: str
    topic_title: str
    what: str
    when: Optional[str] = None
    where: Optional[str] = None
    who: Optional[str] = None
    how: Optional[str] = None
    unknowns: List[str] = Field(default_factory=list)
    evidence_links: List[str] = Field(default_factory=list)
    sources: List[SourceItem] = Field(default_factory=list)
    confidence: float = 0.0


class WaybackAvailableResponse(BaseModel):
    url: str
    available: bool
    closest_timestamp: Optional[str] = None
    closest_url: Optional[str] = None
    status: Optional[str] = None


class GovDomain(BaseModel):
    domain: str
    organization: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    domain_type: Optional[str] = None


class LawResult(BaseModel):
    source: str
    title: str
    url: str
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


# -----------------------------
# Helpers
# -----------------------------

def safe_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = dtparser.parse(str(val))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_text(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def cache_get(key: str) -> Any:
    v = _CACHE.get(key)
    if not v:
        return None
    exp, val = v
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return val


def cache_set(key: str, val: Any, ttl_s: int) -> None:
    _CACHE[key] = (time.time() + ttl_s, val)


def norm_symbol(symbol: str) -> str:
    """Normalize market symbols.

    - Stocks/ETFs default to .US if symbol is bare letters/numbers.
    - Crypto uses Stooq's .V symbols (e.g., BTC.V, ETH.V).
    """
    s = (symbol or "").strip().upper()
    s = s.replace(" ", "")
    if not s:
        return s
    # Preserve explicit suffixes
    if "." in s:
        return s
    # If it looks like a crypto pair (e.g., BTCUSD) keep it
    if s.endswith("USD") and len(s) <= 8:
        return s
    # Default: US listing
    return f"{s}.US"


async def stooq_csv(symbol: str) -> str:
    sym = norm_symbol(symbol)
    url = f"{STOOQ_CSV_BASE}?s={sym.lower()}&i=d"
    cache_key = f"stooq_csv:{url}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Market data upstream error ({r.status_code})")
        txt = r.text
    # Cache for 2 minutes; series endpoints are frequently requested for sparklines
    cache_set(cache_key, txt, ttl_s=120)
    return txt


def parse_stooq_csv(text: str) -> List[Dict[str, Any]]:
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    rows: List[Dict[str, Any]] = []
    for row in reader:
        # Expected headers: Date, Open, High, Low, Close, Volume
        date = row.get("Date") or row.get("<DATE>")
        if not date:
            continue
        def num(x: Any) -> Optional[float]:
            try:
                if x is None:
                    return None
                xs = str(x).strip()
                if not xs or xs in ("-", "N/A", "na"):
                    return None
                return float(xs)
            except Exception:
                return None
        rows.append({
            "date": date,
            "open": num(row.get("Open") or row.get("<OPEN>")),
            "high": num(row.get("High") or row.get("<HIGH>")),
            "low": num(row.get("Low") or row.get("<LOW>")),
            "close": num(row.get("Close") or row.get("<CLOSE>")),
            "volume": num(row.get("Volume") or row.get("<VOL>")),
        })
    return rows


def fingerprint(title: str, url: str) -> str:
    h = hashlib.sha256()
    h.update((title + "|" + url).encode("utf-8"))
    return h.hexdigest()[:16]


def similarity(a: SourceItem, b: SourceItem) -> float:
    t1 = (a.title or "") + " " + (a.summary or "")
    t2 = (b.title or "") + " " + (b.summary or "")
    return fuzz.token_set_ratio(t1, t2) / 100.0


def cluster_items(items: List[SourceItem], threshold: float = 0.84) -> List[List[SourceItem]]:
    clusters: List[List[SourceItem]] = []
    for it in items:
        placed = False
        for c in clusters:
            if similarity(it, c[0]) >= threshold:
                c.append(it)
                placed = True
                break
        if not placed:
            clusters.append([it])

    def cluster_time(c: List[SourceItem]) -> float:
        dts = [x.published_at for x in c if x.published_at]
        return max([dt.timestamp() for dt in dts], default=0.0)

    clusters.sort(key=cluster_time, reverse=True)
    return clusters


def extract_simple_entities(text: str) -> Dict[str, Optional[str]]:
    t = text or ""
    when_match = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b", t, re.I)
    when = when_match.group(0) if when_match else None

    where_match = re.search(r"\bin\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3})\b", t)
    where = where_match.group(1) if where_match else None

    who_match = re.search(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3})\b", t)
    who = who_match.group(1) if who_match else None

    return {"when": when, "where": where, "who": who}


def is_loaded_language(text: str) -> bool:
    loaded = [
        "shocking",
        "outrage",
        "evil",
        "corrupt",
        "disgrace",
        "slam",
        "destroy",
        "bombshell",
        "explosive",
        "devastating",
        "stunning",
        "radical",
        "traitor",
        "hoax",
        "propaganda",
    ]
    t = (text or "").lower()
    return any(w in t for w in loaded)


def make_brief(cluster: List[SourceItem]) -> ClusterBrief:
    cluster = sorted(cluster, key=lambda x: x.published_at or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    lead = cluster[0]

    text_for_extract = clean_text((lead.title or "") + ". " + (lead.summary or ""))
    ents = extract_simple_entities(text_for_extract)

    what = lead.title or ""
    if is_loaded_language(what):
        what = re.sub(r"\b(shocking|bombshell|explosive|stunning|devastating)\b", "", what, flags=re.I)
        what = clean_text(what)

    evidence = []
    for it in cluster:
        if it.source in ("Federal Register", "Congress.gov", "GovInfo"):
            evidence.append(it.url)
    all_links = [it.url for it in cluster if it.url]

    unknowns = []
    if not ents.get("when"):
        unknowns.append("Exact timing/date not clearly stated in the lead source.")
    if not ents.get("where"):
        unknowns.append("Location not clearly stated in the lead source.")
    unknowns.append("Motive/intent claims are unverified unless supported by documents or direct quotes.")

    n_sources = len({(it.source, it.url) for it in cluster})
    conf = 0.35 + min(0.45, 0.08 * n_sources)
    if evidence:
        conf += 0.15
    if is_loaded_language(lead.title or ""):
        conf -= 0.08
    conf = max(0.0, min(1.0, conf))

    return ClusterBrief(
        cluster_id=fingerprint(lead.title or "", lead.url or ""),
        topic_title=clean_text(lead.title or "")[:140],
        what=clean_text(what),
        when=ents.get("when"),
        where=ents.get("where"),
        who=ents.get("who"),
        how=None,
        unknowns=unknowns,
        evidence_links=list(dict.fromkeys(evidence)) or all_links[:3],
        sources=cluster,
        confidence=round(conf, 2),
    )


# -----------------------------
# Ingestors
# -----------------------------

async def fetch_rss(name: str, url: str) -> List[SourceItem]:
    fp = feedparser.parse(url)
    items: List[SourceItem] = []
    for e in fp.entries[:50]:
        items.append(
            SourceItem(
                source=name,
                title=clean_text(getattr(e, "title", "")),
                url=getattr(e, "link", ""),
                published_at=safe_dt(getattr(e, "published", None) or getattr(e, "updated", None)),
                summary=clean_text(getattr(e, "summary", "")[:2000]),
                raw={"rss": True},
            )
        )
    return items


async def fetch_gdelt(query: str, max_items: int = 30) -> List[SourceItem]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_items),
        "sort": "HybridRel",
        "formatdatetime": "true",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(GDELT_DOC_API, params=params)
        r.raise_for_status()
        data = r.json()

    out: List[SourceItem] = []
    for a in data.get("articles", []) or []:
        out.append(
            SourceItem(
                source="GDELT",
                title=clean_text(a.get("title", "")),
                url=a.get("url", ""),
                published_at=safe_dt(a.get("seendate")),
                summary=clean_text(a.get("snippet", "")),
                raw={"domain": a.get("domain"), "sourceCountry": a.get("sourceCountry")},
            )
        )
    return out


async def fetch_federal_register(term: str, per_page: int = 20) -> List[SourceItem]:
    params = {
        "conditions[term]": term,
        "order": "newest",
        "per_page": str(per_page),
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(FEDERAL_REGISTER_API, params=params)
        r.raise_for_status()
        data = r.json()

    out: List[SourceItem] = []
    for d in data.get("results", []) or []:
        out.append(
            SourceItem(
                source="Federal Register",
                title=clean_text(d.get("title", "")),
                url=d.get("html_url", ""),
                published_at=safe_dt(d.get("publication_date")),
                summary=clean_text(d.get("abstract", "")),
                raw={"document_number": d.get("document_number"), "type": d.get("type")},
            )
        )
    return out


async def fetch_congress(query: str, limit: int = 10) -> List[SourceItem]:
    if not CONGRESS_API_KEY:
        return []
    url = f"{CONGRESS_API_BASE}/bill"
    params = {"api_key": CONGRESS_API_KEY, "format": "json", "limit": str(limit), "query": query}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    out: List[SourceItem] = []
    for b in data.get("bills", []) or []:
        title = b.get("title", "")
        link = b.get("url", "")
        out.append(
            SourceItem(
                source="Congress.gov",
                title=clean_text(title),
                url=link,
                published_at=safe_dt(b.get("updateDate") or b.get("introducedDate")),
                summary=clean_text((b.get("latestAction") or {}).get("text", "")),
                raw={"congress": b.get("congress"), "billType": b.get("type"), "billNumber": b.get("number")},
            )
        )
    return out


# -----------------------------
# .gov dataset cache
# -----------------------------

_dotgov_cache: Dict[str, Any] = {"ts": 0.0, "rows": []}
DOTGOV_CACHE_SECONDS = 12 * 60 * 60


async def load_dotgov_rows(force: bool = False) -> List[Dict[str, str]]:
    now = time.time()
    if (not force) and _dotgov_cache["rows"] and (now - _dotgov_cache["ts"] < DOTGOV_CACHE_SECONDS):
        return _dotgov_cache["rows"]

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(DOTGOV_CSV_URL)
        r.raise_for_status()
        content = r.text

    f = io.StringIO(content)
    reader = csv.DictReader(f)
    rows: List[Dict[str, str]] = []
    for row in reader:
        rows.append({k: (v or "").strip() for k, v in row.items()})

    _dotgov_cache["ts"] = now
    _dotgov_cache["rows"] = rows
    return rows


# -----------------------------
# FastAPI
# -----------------------------

app = FastAPI(title="PlainFacts API", version=APP_VERSION)


@app.middleware("http")
async def markets_rate_limiter(request, call_next):
    # Lightweight in-memory rate limit for market endpoints.
    # Default: 60 requests/minute per IP for /markets*.
    if request.url.path.startswith("/markets"):
        ip = request.headers.get("x-real-ip") or request.client.host or "unknown"
        now = time.time()
        bucket = int(now // 60)
        key = f"{ip}:{bucket}"
        cnt = _RL.get(key, 0) + 1
        _RL[key] = cnt
        # prune old buckets occasionally
        if cnt == 1:
            for k in list(_RL.keys()):
                try:
                    _, b = k.rsplit(":", 1)
                    if int(b) < bucket - 2:
                        _RL.pop(k, None)
                except Exception:
                    pass
        if cnt > 60:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded for market data (60/min)."})
    return await call_next(request)


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "congress_api_key_configured": bool(CONGRESS_API_KEY),
        "govinfo_api_key_configured": bool(GOVINFO_API_KEY),
    }


@app.get("/briefs", response_model=List[ClusterBrief])
async def briefs(q: str = Query(...), max_clusters: int = Query(10, ge=1, le=30)):
    items: List[SourceItem] = []

    for name, url in RSS_FEEDS.items():
        try:
            items.extend(await fetch_rss(name, url))
        except Exception:
            pass

    try:
        items.extend(await fetch_gdelt(q, max_items=40))
    except Exception:
        pass

    try:
        items.extend(await fetch_federal_register(q, per_page=20))
    except Exception:
        pass

    try:
        items.extend(await fetch_congress(q, limit=10))
    except Exception:
        pass

    items = [it for it in items if it.title and it.url]
    clusters = cluster_items(items, threshold=0.84)[:max_clusters]
    return [make_brief(c) for c in clusters]


DEFAULT_FALLBACK_QUERIES = ["federal register", "congress", "inflation", "jobs", "wildfire", "FAA", "FDA", "SEC"]


@app.get("/brief/{cluster_id}", response_model=ClusterBrief)
async def get_brief_by_id(cluster_id: str, q: Optional[str] = Query(None)):
    # Try hinted query first (best)
    if q and q.strip():
        bs = await briefs(q=q.strip(), max_clusters=30)
        for b in bs:
            if b.cluster_id == cluster_id:
                return b

    for hint in DEFAULT_FALLBACK_QUERIES:
        bs = await briefs(q=hint, max_clusters=25)
        for b in bs:
            if b.cluster_id == cluster_id:
                return b

    raise HTTPException(status_code=404, detail="Brief not found. Provide ?q=topic as a hint.")


@app.get("/wayback/available", response_model=WaybackAvailableResponse)
async def wayback_available(url: str = Query(...), timestamp: Optional[str] = Query(None)):
    params = {"url": url}
    if timestamp:
        params["timestamp"] = timestamp

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(WAYBACK_AVAILABLE, params=params)
        r.raise_for_status()
        data = r.json()

    archived = (data.get("archived_snapshots") or {}).get("closest")
    if not archived:
        return WaybackAvailableResponse(url=url, available=False)

    return WaybackAvailableResponse(
        url=url,
        available=True,
        closest_timestamp=str(archived.get("timestamp")) if archived.get("timestamp") else None,
        closest_url=str(archived.get("url")) if archived.get("url") else None,
        status=str(archived.get("status")) if archived.get("status") else None,
    )


@app.get("/wayback/cdx")
async def wayback_cdx(url: str = Query(...), limit: int = Query(25, ge=1, le=500)):
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": str(limit),
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(WAYBACK_CDX, params=params)
        r.raise_for_status()
        data = r.json()

    # data[0] is header
    if not isinstance(data, list) or len(data) <= 1:
        return {"url": url, "captures": []}

    header = data[0]
    captures = []
    for row in data[1:]:
        item = dict(zip(header, row))
        ts = item.get("timestamp")
        item["wayback_url"] = f"https://web.archive.org/web/{ts}/{item.get('original','')}" if ts else None
        captures.append(item)

    return {"url": url, "captures": captures}


@app.get("/gov/domains", response_model=List[GovDomain])
async def gov_domains(
    state: Optional[str] = Query(None, description="Two-letter state code, e.g. CA"),
    q: Optional[str] = Query(None, description="Keyword for domain/org/city"),
    limit: int = Query(25, ge=1, le=200),
    refresh: bool = Query(False, description="Force refresh of the .gov dataset cache"),
):
    rows = await load_dotgov_rows(force=refresh)
    st = (state or "").strip().upper()
    needle = (q or "").strip().lower()

    out: List[GovDomain] = []
    for r in rows:
        # dataset columns include at least: domain, organization_name, city, state_territory, domain_type
        domain = r.get("domain", "")
        org = r.get("organization_name") or r.get("organization") or ""
        city = r.get("city", "")
        row_state = (r.get("state_territory") or r.get("state") or "").upper()
        dtype = r.get("domain_type", "")

        if st and row_state != st:
            continue

        if needle:
            hay = f"{domain} {org} {city} {row_state} {dtype}".lower()
            if needle not in hay:
                continue

        out.append(
            GovDomain(
                domain=domain,
                organization=org or None,
                city=city or None,
                state=row_state or None,
                domain_type=dtype or None,
            )
        )
        if len(out) >= limit:
            break

    return out


@app.get("/laws/search", response_model=List[LawResult])
async def laws_search(
    query: str = Query(..., description="Search term, e.g. 'clean air act' or 'labor code'"),
    jurisdiction: str = Query("federal", description="Currently supports: federal (state provided only used to find official sites)"),
    state: Optional[str] = Query(None, description="Two-letter state code, used for official-site discovery"),
    limit: int = Query(20, ge=1, le=50),
):
    q = query.strip()
    results: List[LawResult] = []

    # 1) Federal Register (official)
    try:
        items = await fetch_federal_register(q, per_page=min(limit, 20))
        for it in items:
            results.append(
                LawResult(
                    source="Federal Register",
                    title=it.title,
                    url=it.url,
                    published_at=it.published_at.isoformat() if it.published_at else None,
                    snippet=it.summary,
                    raw=it.raw,
                )
            )
    except Exception:
        pass

    # 2) Congress.gov (official) - optional key
    if CONGRESS_API_KEY:
        try:
            items = await fetch_congress(q, limit=min(10, limit))
            for it in items:
                results.append(
                    LawResult(
                        source="Congress.gov",
                        title=it.title,
                        url=it.url,
                        published_at=it.published_at.isoformat() if it.published_at else None,
                        snippet=it.summary,
                        raw=it.raw,
                    )
                )
        except Exception:
            pass

    # 3) GovInfo search (official) - optional key
    # Docs: https://api.govinfo.gov/docs/
    if GOVINFO_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                # GovInfo search endpoint
                # Example: /search?query=...&pageSize=...&offset=...&api_key=...
                r = await client.get(
                    f"{GOVINFO_API_BASE}/search",
                    params={"query": q, "pageSize": str(min(10, limit)), "offset": "0", "api_key": GOVINFO_API_KEY},
                )
                r.raise_for_status()
                data = r.json()

            for p in (data.get("results") or []):
                title = clean_text(p.get("title", ""))
                pkg = p.get("packageId") or p.get("packageId")
                link = p.get("packageLink") or p.get("link")
                if not link and pkg:
                    link = f"{GOVINFO_API_BASE}/packages/{pkg}/summary?api_key={GOVINFO_API_KEY}"
                results.append(
                    LawResult(
                        source="GovInfo",
                        title=title or (pkg or "GovInfo result"),
                        url=str(link or ""),
                        published_at=str(p.get("dateIssued") or "") or None,
                        snippet=clean_text(p.get("collectionName", "") or p.get("summary", "")) or None,
                        raw=p,
                    )
                )
        except Exception:
            pass

    # 4) Official-site discovery via .gov index (helps users find their state/city legal pages)
    # This is not "law text", but it helps users quickly reach official local legal sources.
    if state:
        try:
            state_domains = await gov_domains(state=state, q="legislature", limit=5, refresh=False)
            for d in state_domains:
                results.append(
                    LawResult(
                        source=".gov index",
                        title=f"Official .gov site (state search): {d.domain}",
                        url=f"https://{d.domain}",
                        snippet=d.organization,
                        raw=d.model_dump(),
                    )
                )
        except Exception:
            pass

    # De-dupe by url
    seen = set()
    deduped: List[LawResult] = []
    for r in results:
        if not r.url or r.url in seen:
            continue
        seen.add(r.url)
        deduped.append(r)
        if len(deduped) >= limit:
            break

    return deduped


# -----------------------------
# Markets (Stooq-based)
# -----------------------------

STOCKS_TOP = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "UNH",
]

ETFS_TOP = [
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "XLK", "XLF", "XLE", "GLD",
]

CRYPTO_TOP = [
    "BTC.V", "ETH.V", "SOL.V", "XRP.V", "DOGE.V", "ADA.V", "DOT.V", "LINK.V", "LTC.V", "BCH.V",
]


@app.get("/markets/quote")
async def markets_quote(symbol: str = Query(..., description="Ticker symbol, e.g. AAPL, SPY, BTC.V")):
    txt = await stooq_csv(symbol)
    rows = parse_stooq_csv(txt)
    if len(rows) < 1:
        raise HTTPException(status_code=404, detail="No market data for that symbol.")
    last = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    close = last.get("close")
    prev_close = prev.get("close") if prev else None
    chg = (close - prev_close) if (close is not None and prev_close is not None) else None
    chg_pct = ((chg / prev_close) * 100.0) if (chg is not None and prev_close) else None
    return {
        "symbol": norm_symbol(symbol),
        "date": last.get("date"),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "close": close,
        "volume": last.get("volume"),
        "change": chg,
        "change_pct": chg_pct,
    }


@app.get("/markets/series")
async def markets_series(
    symbol: str = Query(..., description="Ticker symbol, e.g. AAPL, SPY, BTC.V"),
    days: int = Query(60, ge=5, le=365, description="Number of most-recent daily points"),
):
    txt = await stooq_csv(symbol)
    rows = parse_stooq_csv(txt)
    if not rows:
        raise HTTPException(status_code=404, detail="No market data for that symbol.")
    rows = rows[-days:]
    # Return as compact arrays for UI
    return {
        "symbol": norm_symbol(symbol),
        "points": rows,
    }


async def _top_item(sym: str, spark_days: int = 30) -> Dict[str, Any]:
    q = await markets_quote(sym)
    try:
        s = await markets_series(sym, days=spark_days)
        closes = [p.get("close") for p in s.get("points", []) if p.get("close") is not None]
    except Exception:
        closes = []
    q["spark"] = closes[-spark_days:]
    return q


@app.get("/markets/top")
async def markets_top(
    kind: str = Query("stocks", description="stocks|etfs|crypto"),
    limit: int = Query(10, ge=1, le=25),
):
    kind_l = (kind or "stocks").lower()
    if kind_l == "stocks":
        syms = STOCKS_TOP[:limit]
    elif kind_l == "etfs":
        syms = ETFS_TOP[:limit]
    elif kind_l == "crypto":
        syms = CRYPTO_TOP[:limit]
    else:
        raise HTTPException(status_code=400, detail="kind must be stocks|etfs|crypto")

    # Fetch concurrently with a conservative fan-out
    sem = asyncio.Semaphore(6)

    async def run_one(s: str):
        async with sem:
            try:
                return await _top_item(s)
            except Exception:
                return {"symbol": norm_symbol(s), "error": True}

    items = await asyncio.gather(*[run_one(s) for s in syms])
    # Keep order, drop empties
    return [it for it in items if it.get("symbol")]

# -----------------------------
# Preview / image proxy helpers
# -----------------------------
IMG_CACHE: Dict[str, Dict[str, Any]] = {}
IMG_CACHE_TTL_SEC = 60 * 60  # 1 hour

def _abs_url(base: str, u: str) -> str:
    try:
        from urllib.parse import urljoin
        return urljoin(base, u)
    except Exception:
        return u

def _extract_meta_images(html: str) -> Dict[str, List[str]]:
    """
    Lightweight HTML extraction without external parsers.
    Returns candidate urls for: og, twitter, icons, imgs.
    """
    og = re.findall(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I)
    tw = re.findall(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I)
    icons = re.findall(r'<link[^>]+rel=["\'](?:apple-touch-icon|icon|shortcut icon)["\'][^>]+href=["\']([^"\']+)["\']', html, flags=re.I)
    # Some sites omit quotes; handle a common unquoted href pattern
    icons += re.findall(r'<link[^>]+rel=(?:apple-touch-icon|icon|shortcut icon)[^>]+href=([^ >]+)', html, flags=re.I)
    imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
    return {
        "og": og[:5],
        "twitter": tw[:5],
        "icons": icons[:10],
        "imgs": imgs[:15],
    }

def _pick_best_image(base_url: str, candidates: Dict[str, List[str]]) -> Dict[str, Any]:
    """
    Choose a neutral-ish image:
    - Prefer icon/logo-like assets when available (helps for organizations).
    - Else fall back to og:image / twitter:image.
    """
    # Normalize and de-dupe
    def norm_list(lst):
        out = []
        for u in lst:
            u = (u or "").strip().strip('"').strip("'")
            if not u:
                continue
            out.append(_abs_url(base_url, u))
        # de-dupe preserving order
        seen = set()
        uniq = []
        for u in out:
            if u in seen: 
                continue
            seen.add(u)
            uniq.append(u)
        return uniq

    icons = norm_list(candidates.get("icons", []))
    og = norm_list(candidates.get("og", []))
    tw = norm_list(candidates.get("twitter", []))
    imgs = norm_list(candidates.get("imgs", []))

    # Heuristic: if an icon href includes 'logo' or 'seal' or is an apple-touch-icon, likely a safe logo
    logo_like = [u for u in icons if re.search(r'(logo|seal|emblem|crest)', u, flags=re.I)]
    best = None
    best_type = None

    if logo_like:
        best = logo_like[0]; best_type = "logo"
    elif icons:
        best = icons[0]; best_type = "logo"
    elif og:
        best = og[0]; best_type = "context"
    elif tw:
        best = tw[0]; best_type = "context"
    elif imgs:
        best = imgs[0]; best_type = "context"
    else:
        best = None; best_type = "fallback"

    return {
        "best_image": best,
        "best_type": best_type,
        "og_image": og[0] if og else None,
        "twitter_image": tw[0] if tw else None,
        "icons": icons[:5],
    }

@app.get("/preview")
async def preview(url: str = Query(..., description="Article URL to extract a neutral preview image from")):
    """
    Returns neutral preview metadata for an article page (best image + candidates).
    We avoid heavy parsing; result is best-effort.
    """
    now = time.time()
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached = IMG_CACHE.get(key)
    if cached and (now - cached.get("_ts", 0)) < IMG_CACHE_TTL_SEC:
        out = dict(cached)
        out.pop("_ts", None)
        return out

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers={"User-Agent": "PlainFacts/0.4 (+https://example.com)"}) as client:
            r = await client.get(url)
            r.raise_for_status()
            html = r.text or ""
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Preview fetch failed: {type(e).__name__}")

    cand = _extract_meta_images(html)
    picked = _pick_best_image(str(r.url), cand)

    out = {"url": str(r.url), **picked}
    IMG_CACHE[key] = {"_ts": now, **out}
    return out

@app.get("/proxy/image")
async def proxy_image(url: str = Query(..., description="Image URL to proxy for CORS-safe rendering")):
    """
    Proxies an image with permissive CORS headers so the web UI / extension can display
    preview images without browser CORS issues.
    """
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers={"User-Agent": "PlainFacts/0.4 (+https://example.com)"}) as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content
            ctype = r.headers.get("content-type", "image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Image proxy failed: {type(e).__name__}")

    from fastapi.responses import Response
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
    }
    return Response(content=content, media_type=ctype, headers=headers)


