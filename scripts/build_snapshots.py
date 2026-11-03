#!/usr/bin/env python3
"""Build the static snapshot files for the XBI tape.

No API key. Downloads, then caches under data/.cache/ (gitignored):

- XBI portfolio holdings from SEC EDGAR filings for SPDR Series Trust
  (CIK 0001064642, series S000010018). N-Q, N-CSR, and N-CSRS cover the
  quarters from the fund's first schedule in 2006 through mid-2019.
  N-PORT-P covers quarter-ends from September 2019 onward. Those reports
  are quarterly, which is as often as the holdings are public.
- Unadjusted daily closes from CNBC's public chart endpoint
- Shares outstanding from SEC XBRL, used only to reconstruct market cap
- A current quote, used when a filing has no usable share count
- session_ids.json, the date-to-filename map. It stays out of git so published
  filenames do not spell the session date.

Each month from the first priced portfolio through the latest complete price
month contributes one real session, using the latest public portfolio dated
on or before that day. Prices are unadjusted closes. A ticker CNBC no longer
lists is left out rather than given a made-up price.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import secrets
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / ".cache"
SNAP_DIR = ROOT / "data" / "snapshots"
ID_MAP_PATH = CACHE / "session_ids.json"
FILING_DIR = CACHE / "filings"
HOLDING_DIR = CACHE / "holdings"
SESSION_ID_RE = re.compile(r"^[a-f0-9]{16}$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

CIK = "1064642"
SERIES_ID = "S000010018"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SEC_UA = "XbiGuesser research@example.com"

SHARE_CONCEPTS = (
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic"),
)
ALLOWED_FORMS = {
    "10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A",
}
COMMON_SPLITS = {2, 3, 4, 5, 7, 8, 10, 15, 20, 25, 50}
MAX_SHARE_AGE_DAYS = 550
MAX_STALE_DAYS = 200
MIN_ROWS = 10
PRICE_FROM = "2006-01-01"
PRICE_FROM_STAMP = "20060101000000"
NPORT_START = "2019-09-30"

# Same company, later name. The ticker still quotes that business.
ALIASES = {
    "isis pharmaceutical": ("IONS", "ionis"),
}

GENERIC = {
    "pharmaceutical", "therapeutic", "bioscience", "biopharmaceutical", "biopharma",
    "biotherapeutic", "science", "technology", "technologie", "holding", "group",
    "international", "laboratory", "laboratorie", "medical", "health", "healthcare",
    "biotech", "biotechnology", "common", "stock", "ordinary", "share", "class",
    "incorporated", "corporation", "company", "limited", "american", "national",
    "global", "united", "general", "first", "product", "service",
}
LEGAL = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
    "plc", "nv", "sa", "ag", "lp", "llc", "the", "and", "of", "se",
}
SUFFIXES = (
    " incorporated",
    " corporation",
    " & co.",
    " & co",
    " corp.",
    " corp",
    " inc.",
    " inc",
    " ltd.",
    " ltd",
    " plc",
    " l.p.",
    " n.v.",
    " s.a.",
    " limited",
    " co.",
    " co",
    " lp",
    " llc",
)

SCHEDULE_RE = re.compile(
    r"(?:spdr|streettracks)(?:\s+s\s*&\s*p)?\s+biotech\s+etf\b.{0,160}?"
    r"schedule of investments(?!\s*\(continued\))",
    re.I | re.S,
)
DOTTED_RE = re.compile(r"^(?P<name>.*?)\.{3,}\s*(?P<rest>.*)$")
NUMBER_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")
CATEGORY_RE = re.compile(r"(?:—|--).*\d")


class NotFound(Exception):
    pass


class Limiter:
    def __init__(self, per_sec: float):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.gap
        if delay:
            time.sleep(delay)


def http_get(url: str, headers: dict, timeout: int = 90, attempts: int = 5) -> bytes:
    delay = 1.5
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404:
                raise NotFound(url) from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError(f"failed {url}") from last_error


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def cache_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clean_name(name: str | None) -> str | None:
    if not name:
        return None
    text = " ".join(str(name).replace("\u00a0", " ").replace(",", " ").split())
    changed = True
    while changed and text:
        changed = False
        low = text.lower()
        for suffix in SUFFIXES:
            if low.endswith(suffix):
                text = text[: len(text) - len(suffix)].rstrip(" ,.")
                changed = True
                break
    if text.lower().startswith("the "):
        text = text[4:].strip()
    if text.lower().endswith(" company") and len(text.split()) > 2:
        text = text[: -len(" company")].strip()
    words = []
    for word in text.split(" "):
        if word.isupper() and word.isalpha() and len(word) >= 4:
            words.append(word.title())
        else:
            words.append(word)
    text = " ".join(words).strip(" ,.")
    return text or None


def clean_holding_name(name: str) -> str:
    text = re.sub(r"(?:\([a-h]\))+", "", name)
    text = text.replace("--", " ")
    return clean_name(text) or ""


def norm_key(name: str) -> str:
    text = name.lower().replace("&", " and ")
    text = re.sub(r"(?:\([a-h]\))+", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = []
    for token in text.split():
        if token in LEGAL:
            continue
        if len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        if token and token not in LEGAL:
            tokens.append(token)
    return " ".join(tokens)


def distinctive(tokens: list[str]) -> list[str]:
    return [token for token in tokens if len(token) >= 5 and token not in GENERIC]


def compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    shared = set(distinctive(norm_key(left).split())) & set(distinctive(norm_key(right).split()))
    return bool(shared)


def parse_num(text: str) -> float:
    return float(text.replace(",", ""))


def to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<script\b.*?>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style\b.*?>.*?</style>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|tr|h\d|li|table|page)>", "\n", raw)
    raw = re.sub(r"(?i)<td\b[^>]*>", "\t", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw).replace("\xa0", " ")
    raw = re.sub(r"[ \t]*\n[ \t]*", "\n", raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    raw = re.sub(r"\n{2,}", "\n", raw)
    return raw


def parse_holdings_body(body: str) -> list[dict]:
    holdings = []
    pending: list[str] = []
    numbers: list[float] = []

    def emit() -> None:
        nonlocal pending, numbers
        if pending and len(numbers) >= 2 and numbers[0] > 0 and numbers[1] > 0:
            name = clean_holding_name(" ".join(pending))
            if name and "total" not in name.lower():
                holdings.append({"name": name, "shares": numbers[0], "value": numbers[1]})
        pending = []
        numbers = []

    for raw_line in body.splitlines():
        line = " ".join(raw_line.split())
        if not line or line in {"-", "—"}:
            continue
        low = line.lower()
        if low.startswith("total ") or low.startswith("short term"):
            emit()
            break
        dotted = DOTTED_RE.match(line)
        if dotted and dotted.group("rest"):
            nums = re.findall(r"[\d,]+(?:\.\d+)?", dotted.group("rest"))
            name = clean_holding_name(" ".join(pending + [dotted.group("name")]))
            pending = []
            numbers = []
            if name and len(nums) >= 2:
                shares, value = parse_num(nums[0]), parse_num(nums[1])
                if shares > 0 and value > 0 and "total" not in name.lower():
                    holdings.append({"name": name, "shares": shares, "value": value})
            continue
        if low.startswith(("common stock", "biotechnology", "security description", "schedule of")):
            continue
        if low in {"shares", "value"}:
            continue
        if CATEGORY_RE.search(line) and "," not in line and not DOTTED_RE.search(line):
            continue
        if line == "$":
            continue
        if NUMBER_RE.fullmatch(line):
            if not pending:
                continue
            numbers.append(parse_num(line))
            if len(numbers) >= 2:
                emit()
            continue
        if numbers:
            emit()
        if len(" ".join(pending)) > 140:
            pending = []
        pending.append(line)
    emit()
    return holdings


def extract_schedule(raw: str) -> list[dict]:
    text = to_text(raw)
    match = None
    for candidate in SCHEDULE_RE.finditer(text):
        window = text[candidate.end():candidate.end() + 600]
        if re.search(r"common stocks", window, re.I):
            match = candidate
            break
    if match is None:
        return []
    rest = text[match.end():match.end() + 180000]
    end = re.search(
        r"\n(?:short term investments|total common stocks|total investments)\b",
        rest,
        re.I,
    )
    body = rest[: end.start()] if end else rest[:80000]
    common = re.search(r"common stocks", body, re.I)
    if common:
        body = body[common.start():]
    return parse_holdings_body(body)


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def nport_holding(el: ET.Element) -> dict | None:
    fields = {local_tag(child.tag): child for child in list(el)}
    name = (fields["name"].text or "").strip() if "name" in fields else ""
    title = (fields["title"].text or "").strip() if "title" in fields else ""
    cat = (fields["assetCat"].text or "").strip() if "assetCat" in fields else ""
    units = (fields["units"].text or "").strip() if "units" in fields else ""
    payoff = (fields["payoffProfile"].text or "").strip() if "payoffProfile" in fields else ""
    try:
        value = float(fields["valUSD"].text) if "valUSD" in fields and fields["valUSD"].text else 0.0
    except ValueError:
        value = 0.0
    try:
        balance = float(fields["balance"].text) if "balance" in fields and fields["balance"].text else 0.0
    except ValueError:
        balance = 0.0
    if cat != "EC" or units != "NS" or payoff != "Long" or value <= 0 or balance <= 0:
        return None
    blob = f"{name} {title}".lower()
    if any(word in blob for word in ("warrant", "preferred", "right", "bond", "note", "etf", "money market")):
        return None
    cusip = (fields["cusip"].text or "").strip() if "cusip" in fields else ""
    isin = ""
    ident = fields.get("identifiers")
    if ident is not None:
        for child in ident.iter():
            if local_tag(child.tag) == "isin":
                isin = (child.get("value") or child.text or "").strip()
                break
    cleaned = clean_holding_name(name)
    if not cleaned:
        return None
    return {"name": cleaned, "shares": balance, "value": value, "cusip": cusip, "isin": isin}


def parse_nport(payload: bytes) -> tuple[str | None, list[dict]]:
    """Read one N-PORT file. A trust-wide file has many series; keep XBI only."""
    import io

    current = None
    report = None
    seen: dict[str, dict] = {}
    for _event, el in ET.iterparse(io.BytesIO(payload), events=("end",)):
        tag = local_tag(el.tag)
        if tag == "seriesId" and (el.text or "").strip():
            current = el.text.strip()
        elif tag == "repPdDate" and current == SERIES_ID and (el.text or "").strip():
            report = el.text.strip()
        elif tag == "invstOrSec" and current == SERIES_ID:
            row = nport_holding(el)
            if row:
                key = row["isin"] or row["cusip"] or row["name"].lower()
                prev = seen.get(key)
                if prev is None or row["value"] > prev["value"]:
                    seen[key] = row
            el.clear()
    return report, list(seen.values())


def archive_url(accession: str, filename: str) -> str:
    folder = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{folder}/{filename}"


def efts_filings() -> list[dict]:
    headers = {"User-Agent": SEC_UA, "Accept": "application/json"}
    queries = (
        ("NPORT-P", '"S000010018"'),
        ("N-Q", '"SPDR Biotech ETF" OR "SPDR S&P Biotech ETF" OR "SPDR BIOTECH ETF"'),
        ("N-CSR", '"SPDR Biotech ETF" OR "SPDR S&P Biotech ETF" OR "SPDR BIOTECH ETF"'),
        ("N-CSRS", '"SPDR Biotech ETF" OR "SPDR S&P Biotech ETF" OR "SPDR BIOTECH ETF"'),
    )
    found: dict[tuple[str, str], dict] = {}
    for form, query in queries:
        start = 0
        while True:
            params = {
                "q": query,
                "forms": form,
                "ciks": f"000{CIK}",
                "dateRange": "custom",
                "startdt": "2006-01-01",
                "enddt": "2026-12-31",
                "from": str(start),
                "size": "50",
            }
            url = "https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params)
            payload = json.loads(http_get(url, headers, timeout=90))
            hits = payload["hits"]["hits"]
            total = payload["hits"]["total"]["value"]
            for hit in hits:
                src = hit["_source"]
                period = src.get("period_ending") or ""
                accession = src.get("adsh") or ""
                filename = hit["_id"].split(":", 1)[-1]
                if not period or not accession or not filename:
                    continue
                if form != "NPORT-P" and period >= NPORT_START:
                    continue
                row = {
                    "form": form,
                    "period": period,
                    "filed": src.get("file_date") or "",
                    "accession": accession,
                    "filename": filename,
                }
                key = (period, form)
                prev = found.get(key)
                if prev is None or row["filed"] > prev["filed"]:
                    found[key] = row
            start += len(hits)
            if start >= total or not hits:
                break
            time.sleep(0.2)
    rank = {"NPORT-P": 0, "N-Q": 1, "N-CSR": 2, "N-CSRS": 3}
    by_period: dict[str, dict] = {}
    for row in found.values():
        prev = by_period.get(row["period"])
        if prev is None or rank[row["form"]] < rank[prev["form"]]:
            by_period[row["period"]] = row
    chosen = sorted(by_period.values(), key=lambda item: item["period"])
    print(f"holding reports {len(chosen)} {chosen[0]['period']} → {chosen[-1]['period']}", flush=True)
    return chosen


def load_filing_bytes(row: dict) -> bytes:
    path = FILING_DIR / f"{row['accession']}{Path(row['filename']).suffix}"
    if path.exists() and path.stat().st_size > 1000:
        return path.read_bytes()
    print(f"download {row['period']} {row['form']} {row['accession']}", flush=True)
    payload = http_get(archive_url(row["accession"], row["filename"]), {"User-Agent": SEC_UA, "Accept": "*/*"}, timeout=180)
    atomic_write_bytes(path, payload)
    time.sleep(0.2)
    return payload


def load_holdings() -> list[dict]:
    reports = []
    for row in efts_filings():
        cache = HOLDING_DIR / f"{row['period']}.json"
        cached = cache_json(cache)
        if cached and cached.get("accession") == row["accession"] and cached.get("holdings"):
            reports.append(cached)
            continue
        try:
            payload = load_filing_bytes(row)
        except (urllib.error.URLError, TimeoutError, NotFound, ConnectionError) as exc:
            print(f"filing {row['period']} failed: {exc}", flush=True)
            continue
        if row["form"] == "NPORT-P":
            period, holdings = parse_nport(payload)
            if period and period != row["period"]:
                print(f"nport date {period} filed as {row['period']}", flush=True)
                row = dict(row)
                row["period"] = period
        else:
            holdings = extract_schedule(payload.decode("utf-8", "replace"))
        print(f"parsed {row['period']} {row['form']} names {len(holdings)}", flush=True)
        if len(holdings) < 8:
            print(f"skip thin parse {row['period']} {row['accession']}", flush=True)
            continue
        saved = {
            "date": row["period"],
            "form": row["form"],
            "accession": row["accession"],
            "holdings": holdings,
        }
        atomic_write(cache, json.dumps(saved) + "\n")
        reports.append(saved)
    reports.sort(key=lambda item: item["date"])
    if not reports:
        raise SystemExit("no XBI portfolios parsed")
    return reports


def listing_names() -> dict[str, str]:
    """Normalized security name → ticker. ETFs and test issues are left out."""
    headers = {"User-Agent": BROWSER_UA, "Accept": "text/plain,*/*"}
    mapped: dict[str, set[str]] = defaultdict(set)

    def add(symbol: str, security_name: str) -> None:
        if not TICKER_RE.fullmatch(symbol):
            return
        title = security_name.split(" - ", 1)[0]
        key = norm_key(title)
        if key:
            mapped[key].add(symbol)

    nasdaq = CACHE / "nasdaqlisted.txt"
    other = CACHE / "otherlisted.txt"
    if not nasdaq.exists():
        print("downloading nasdaq listings…", flush=True)
        atomic_write_bytes(nasdaq, http_get(NASDAQ_LISTED, headers, timeout=60))
    if not other.exists():
        atomic_write_bytes(other, http_get(OTHER_LISTED, headers, timeout=60))
    for line in nasdaq.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 7 or parts[3] == "Y" or parts[6] == "Y":
            continue
        add(parts[0].strip().upper(), parts[1])
    for line in other.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 7 or parts[4] == "Y" or parts[6] == "Y":
            continue
        add(parts[0].strip().upper(), parts[1])
    unique = {key: next(iter(tickers)) for key, tickers in mapped.items() if len(tickers) == 1}
    print(f"listing names {len(unique)}", flush=True)
    return unique


def match_listing(name: str, listings: dict[str, str]) -> str | None:
    key = norm_key(name)
    if not key:
        return None
    if key in ALIASES:
        return ALIASES[key][0]
    exact = listings.get(key)
    if exact:
        return exact
    query = key.split()
    if not query or query[0] in GENERIC or len(query[0]) < 5:
        return None
    hits = set()
    for listed, ticker in listings.items():
        tokens = listed.split()
        if not tokens or tokens[0] in GENERIC or len(tokens[0]) < 5:
            continue
        shorter, longer = (tokens, query) if len(tokens) <= len(query) else (query, tokens)
        if longer[: len(shorter)] == shorter and len(longer) - len(shorter) <= 2:
            hits.add(ticker)
    if len(hits) == 1:
        return next(iter(hits))
    return None


def openfigi_map(ids: list[tuple[str, str]]) -> dict[str, dict]:
    """Map ISIN or CUSIP to one US common-stock ticker. Cached."""
    cache_path = CACHE / "openfigi.json"
    cached = cache_json(cache_path) or {}
    missing = [(kind, value) for kind, value in ids if value and f"{kind}:{value}" not in cached]
    print(f"openfigi cached {len(cached)} to map {len(missing)}", flush=True)
    headers = {"User-Agent": SEC_UA, "Content-Type": "application/json"}
    for start in range(0, len(missing), 10):
        batch = missing[start:start + 10]
        body = [{"idType": kind, "idValue": value} for kind, value in batch]
        delay = 3.0
        payload = None
        for _attempt in range(6):
            req = urllib.request.Request(OPENFIGI_URL, data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read())
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                print(f"openfigi http {exc.code}", flush=True)
                payload = None
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"openfigi failed: {exc}", flush=True)
                time.sleep(delay)
        if payload is None:
            continue
        for (kind, value), item in zip(batch, payload):
            cached[f"{kind}:{value}"] = pick_figi(item)
        atomic_write(cache_path, json.dumps(cached) + "\n")
        time.sleep(3.0)
        if (start // 10) % 10 == 0:
            print(f"openfigi {min(start + 10, len(missing))}/{len(missing)}", flush=True)
    return cached


def pick_figi(item: dict) -> dict | None:
    rows = item.get("data") or []
    chosen = []
    for row in rows:
        if row.get("marketSector") != "Equity":
            continue
        if row.get("securityType2") not in {None, "Common Stock"} and row.get("securityType") != "Common Stock":
            continue
        if row.get("securityType2") not in {None, "Common Stock"}:
            continue
        ticker = str(row.get("ticker") or "").upper()
        if row.get("exchCode") == "US" and TICKER_RE.fullmatch(ticker):
            chosen.append(row)
    tickers = {row.get("ticker"): row.get("name") for row in chosen}
    if len(tickers) != 1:
        return None
    ticker, security_name = next(iter(tickers.items()))
    return {"ticker": ticker, "name": security_name}


def resolve_reports(reports: list[dict], listings: dict[str, str]) -> None:
    wanted = []
    seen = set()
    for report in reports:
        if report.get("form") != "NPORT-P":
            continue
        for holding in report["holdings"]:
            isin = (holding.get("isin") or "").upper()
            cusip = (holding.get("cusip") or "").upper()
            if len(isin) == 12 and isin not in seen:
                seen.add(isin)
                wanted.append(("ID_ISIN", isin))
            elif len(cusip) == 9 and cusip not in seen:
                seen.add(cusip)
                wanted.append(("ID_CUSIP", cusip))
    figi = openfigi_map(wanted) if wanted else {}
    for report in reports:
        for holding in report["holdings"]:
            isin = (holding.get("isin") or "").upper()
            cusip = (holding.get("cusip") or "").upper()
            mapped = figi.get(f"ID_ISIN:{isin}") if isin else None
            if not mapped and cusip:
                mapped = figi.get(f"ID_CUSIP:{cusip}")
            if mapped:
                holding["ticker"] = mapped["ticker"]
                holding["securityName"] = mapped.get("name") or holding["name"]
                holding["via"] = "cusip"
                continue
            ticker = match_listing(holding["name"], listings)
            if ticker:
                holding["ticker"] = ticker
                holding["securityName"] = holding["name"]
                holding["via"] = "alias" if norm_key(holding["name"]) in ALIASES else "name"
            else:
                holding["ticker"] = ""
                holding["via"] = ""


def load_session_ids() -> dict[str, str]:
    data = cache_json(ID_MAP_PATH)
    if not isinstance(data, dict):
        return {}
    return {
        iso: sid
        for iso, sid in data.items()
        if isinstance(iso, str) and isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid)
    }


def assign_session_id(iso: str, mapping: dict[str, str]) -> str:
    current = mapping.get(iso)
    if isinstance(current, str) and SESSION_ID_RE.fullmatch(current):
        if sum(1 for sid in mapping.values() if sid == current) == 1:
            return current
    used = set(mapping.values())
    while True:
        fresh = secrets.token_hex(8)
        if fresh not in used:
            mapping[iso] = fresh
            return fresh


def save_session_ids(mapping: dict[str, str]) -> None:
    atomic_write(ID_MAP_PATH, json.dumps(mapping, indent=2, sort_keys=True) + "\n")


def parse_cnbc_magnitude(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("$", "")
    if not text or text.upper() in {"N/A", "NULL", "NONE", "-"}:
        return None
    mult = 1.0
    suffix = text[-1].upper()
    if suffix in {"M", "B", "T"} and len(text) > 1:
        mult = {"M": 1e6, "B": 1e9, "T": 1e12}[suffix]
        text = text[:-1]
    try:
        value = float(text) * mult
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def match_ratio(ratio: float) -> int | None:
    if ratio < 1.85 or ratio > 80:
        return None
    candidates = []
    for n in range(2, 81):
        err = abs(ratio - n) / n
        limit = 0.045 if n <= 3 else 0.075
        if err <= limit:
            candidates.append((err, n))
    if not candidates:
        return None
    best_err = min(err for err, _n in candidates)
    close = [(err, n) for err, n in candidates if err <= best_err + 0.015]
    close.sort(key=lambda item: (0 if item[1] in COMMON_SPLITS else 1, item[0]))
    return close[0][1]


def price_divisors(bars: dict[str, float]) -> dict[str, float]:
    dates = sorted(bars)
    event = [1.0] * len(dates)
    for i in range(1, len(dates)):
        prev = bars[dates[i - 1]]
        cur = bars[dates[i]]
        if prev <= 0 or cur <= 0:
            continue
        if min(prev, cur) < 2 or max(prev, cur) < 8:
            continue
        nxt = bars[dates[i + 1]] if i + 1 < len(dates) else None
        if nxt is not None:
            if abs(nxt - prev) < abs(nxt - cur):
                continue
            if abs(nxt - cur) / cur > 0.35:
                continue
        overnight = max(prev, cur) / min(prev, cur)
        pre = [bars[dates[j]] for j in range(max(0, i - 3), i)]
        post = [bars[dates[j]] for j in range(i, min(len(dates), i + 3))]
        pre_med = statistics.median(pre) if pre else prev
        post_med = statistics.median(post) if post else cur
        if prev > cur:
            factor = match_ratio(prev / cur)
            if factor is None and overnight >= 3 and post_med:
                factor = match_ratio(pre_med / post_med)
            if factor:
                event[i] = float(factor)
        else:
            factor = match_ratio(cur / prev)
            if factor is None and overnight >= 3 and pre_med:
                factor = match_ratio(post_med / pre_med)
            if factor:
                event[i] = 1.0 / float(factor)
    suffix = [1.0] * (len(dates) + 1)
    for i in range(len(dates) - 1, -1, -1):
        suffix[i] = event[i] * suffix[i + 1]
    return {dates[i]: suffix[i + 1] for i in range(len(dates))}


def download_sec_tickers() -> dict[str, int]:
    path = CACHE / "sec_tickers.json"
    if not path.exists():
        print("downloading SEC ticker map…", flush=True)
        raw = http_get(SEC_TICKERS_URL, {"User-Agent": SEC_UA, "Accept": "application/json"})
        atomic_write(path, raw.decode("utf-8"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.values() if isinstance(payload, dict) else payload
    mapped: dict[str, int] = {}
    for record in records:
        ticker = str(record.get("ticker") or "").upper()
        if ticker:
            mapped[ticker] = int(record["cik_str"])
    print(f"SEC tickers {len(mapped)}", flush=True)
    return mapped


def price_cache_path(symbol: str) -> Path:
    return CACHE / "prices" / f"{symbol.replace('/', '_')}.json"


def price_end_stamp() -> str:
    return date.today().strftime("%Y%m%d") + "000000"


def parse_price_bars(payload: dict) -> tuple[dict[str, float], str]:
    bars_out = (payload.get("barData") or {}).get("priceBars") or []
    message = (payload.get("statusMessage") or "").lower()
    bars: dict[str, float] = {}
    for bar in bars_out:
        stamp = str(bar.get("tradeTime") or "")[:8]
        if len(stamp) != 8:
            continue
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if close > 0:
            bars[f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"] = round(close, 4)
    return bars, message


def fetch_bar_window(symbol: str, start_stamp: str, end_stamp: str) -> tuple[str, dict[str, float]]:
    url = (
        "https://ts-api.cnbc.com/harmony/app/bars/"
        f"{urllib.parse.quote(symbol, safe='.')}/1D/"
        f"{start_stamp}/{end_stamp}/unadjusted/EST5EDT.json"
    )
    try:
        raw = http_get(
            url,
            {"User-Agent": BROWSER_UA, "Accept": "application/json", "Referer": "https://www.cnbc.com/"},
        )
        payload = json.loads(raw)
    except NotFound:
        return "missing", {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
        print(f"price {symbol} failed: {exc}", flush=True)
        return "error", {}
    bars, message = parse_price_bars(payload)
    if bars:
        return "ok", bars
    if "not found" in message or "security master" in message:
        return "missing", {}
    return "empty", {}


def store_prices(cache: Path, data: dict) -> dict:
    atomic_write(cache, json.dumps(data, separators=(",", ":")))
    return data


def fetch_prices(symbol: str, limiter: Limiter | None = None) -> dict:
    cache = price_cache_path(symbol)
    cached = cache_json(cache)
    if cached and cached.get("from") == PRICE_FROM:
        return cached

    def online(start_stamp: str, end_stamp: str) -> tuple[str, dict[str, float]]:
        if limiter is not None:
            limiter.wait()
        return fetch_bar_window(symbol, start_stamp, end_stamp)

    if cached and cached.get("missing"):
        return store_prices(cache, {"missing": True, "from": PRICE_FROM, "bars": {}})

    bars: dict[str, float] = {}
    if cached:
        bars = dict(cached.get("bars") or {})

    if bars:
        first = min(bars)
        if first > PRICE_FROM:
            end_day = date.fromisoformat(first) - timedelta(days=1)
            status, older = online(PRICE_FROM_STAMP, end_day.strftime("%Y%m%d") + "000000")
            if status == "error":
                return {"missing": False, "bars": bars}
            bars.update(older)
    else:
        status, fetched = online(PRICE_FROM_STAMP, price_end_stamp())
        if status == "error":
            return {"missing": True, "bars": {}}
        if status == "missing":
            return store_prices(cache, {"missing": True, "from": PRICE_FROM, "bars": {}})
        bars = fetched

    if not bars:
        return store_prices(cache, {"missing": True, "from": PRICE_FROM, "bars": {}})
    return store_prices(cache, {"missing": False, "from": PRICE_FROM, "bars": bars})


def series_from_facts(facts: list[dict]) -> list[list]:
    def collect(require_form: bool) -> dict[str, tuple[str, float]]:
        best: dict[str, tuple[str, float]] = {}
        for fact in facts:
            form = fact.get("form") or ""
            if require_form and form not in ALLOWED_FORMS:
                continue
            end = fact.get("end")
            filed = fact.get("filed") or "9999-99-99"
            val = fact.get("val")
            if not end or not isinstance(val, (int, float)) or val <= 0:
                continue
            prev = best.get(end)
            if prev is None or filed < prev[0]:
                best[end] = (filed, float(val))
        return best

    chosen = collect(True) or collect(False)
    return [[end, val] for end, (_filed, val) in sorted(chosen.items())]


def fetch_shares(cik: int) -> dict:
    cache = CACHE / "shares" / f"{cik}.json"
    cached = cache_json(cache)
    if cached is not None:
        return cached
    best_name = None
    best_series: list[list] = []
    for taxonomy, concept in SHARE_CONCEPTS:
        url = (
            "https://data.sec.gov/api/xbrl/companyconcept/"
            f"CIK{cik:010d}/{taxonomy}/{concept}.json"
        )
        try:
            raw = http_get(url, {"User-Agent": SEC_UA, "Accept": "application/json"}, timeout=60)
        except NotFound:
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
            print(f"shares {cik} failed: {exc}", flush=True)
            return {"name": None, "series": []}
        payload = json.loads(raw)
        facts = (payload.get("units") or {}).get("shares") or []
        series = series_from_facts(facts)
        if len(series) > len(best_series):
            best_series = series
            best_name = payload.get("entityName")
        if len(best_series) >= 12:
            break
    data = {"name": best_name, "series": best_series}
    atomic_write(cache, json.dumps(data, separators=(",", ":")))
    return data


def shares_on(series: list[list], day: str) -> float | None:
    best_end = None
    best_val = None
    for end, val in series:
        if end <= day:
            best_end = end
            best_val = float(val)
        else:
            break
    if best_end is None or best_val is None:
        return None
    age = (date.fromisoformat(day) - date.fromisoformat(best_end)).days
    if age > MAX_SHARE_AGE_DAYS:
        return None
    return best_val


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    cache_path = CACHE / "quotes.json"
    cached = cache_json(cache_path) or {}
    needed = [symbol for symbol in symbols if symbol not in cached]
    print(f"quotes cached {len(cached)} needed {len(needed)}", flush=True)
    for start in range(0, len(needed), 40):
        batch = needed[start:start + 40]
        joined = "|".join(urllib.parse.quote(symbol, safe=".") for symbol in batch)
        url = (
            "https://quote.cnbc.com/quote-html-webservice/quote.htm"
            f"?symbols={joined}&requestMethod=itv&noform=1&partnerId=2&fund=1&output=json"
        )
        try:
            raw = http_get(url, {"User-Agent": BROWSER_UA, "Accept": "application/json", "Referer": "https://www.cnbc.com/"})
            payload = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, NotFound) as exc:
            print(f"quote batch failed: {exc}", flush=True)
            continue
        quotes = (payload.get("ITVQuoteResult") or {}).get("ITVQuote") or []
        if isinstance(quotes, dict):
            quotes = [quotes]
        by_symbol = {}
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if symbol:
                by_symbol[symbol] = quote
        for symbol in batch:
            quote = by_symbol.get(symbol) or by_symbol.get(symbol.replace(".", "-")) or by_symbol.get(symbol.replace("-", "."))
            if not quote:
                cached.setdefault(symbol, {"name": None, "last": None, "mcap": None})
                continue
            cached[symbol] = {
                "name": quote.get("name") or quote.get("altName"),
                "last": parse_cnbc_magnitude(quote.get("last")),
                "mcap": parse_cnbc_magnitude(quote.get("mktcapView")),
            }
        atomic_write(cache_path, json.dumps(cached) + "\n")
        print(f"quotes {min(start + 40, len(needed))}/{len(needed)}", flush=True)
    return cached


def accept_ticker(holding: dict, quote: dict | None) -> bool:
    if not holding.get("ticker") or not quote or not quote.get("name"):
        return False
    key = norm_key(holding["name"])
    if key in ALIASES:
        _ticker, token = ALIASES[key]
        return token in norm_key(quote["name"])
    if holding.get("via") == "cusip":
        return compatible(holding.get("securityName") or holding["name"], quote["name"])
    return compatible(holding["name"], quote["name"])


def fallback_mcap(day: str, hist_price: float, divisors: dict[str, float], quote: dict | None) -> float | None:
    if not quote:
        return None
    current = quote.get("last")
    mcap = quote.get("mcap")
    if not current or not mcap:
        return None
    divisor = divisors.get(day, 1.0) or 1.0
    adjusted = hist_price / divisor
    return mcap * (adjusted / current)


def era_ceiling(day: date) -> float:
    # Large enough for Amgen, Gilead, AbbVie, and Moderna's 2021 peak.
    # A reconstructed cap above these lines is a bad share count, not a biotech.
    if day < date(2013, 1, 1):
        return 250e9
    if day < date(2020, 1, 1):
        return 450e9
    return 800e9


def pick_sessions(bars: dict[str, float], first: date, last_report: date) -> list[date]:
    last = max(date.fromisoformat(stamp) for stamp in bars)
    by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
    for stamp in bars:
        current = date.fromisoformat(stamp)
        if current < first or current > last:
            continue
        by_month[(current.year, current.month)].append(current)
    sessions = []
    for key in sorted(by_month):
        days = sorted(by_month[key])
        if key == (last.year, last.month) and last.day < 10:
            continue
        month_start = date(key[0], key[1], 1)
        if month_start < date(first.year, first.month, 1):
            continue
        if month_start > last_report + timedelta(days=MAX_STALE_DAYS):
            continue
        digest = hashlib.sha256(f"{key[0]:04d}-{key[1]:02d}".encode()).digest()
        index = int.from_bytes(digest[:4], "big") % len(days)
        sessions.append(days[index])
    return sessions


def holdings_on(reports: list[dict], day: date) -> dict | None:
    chosen = None
    for report in reports:
        stamp = date.fromisoformat(report["date"])
        if stamp <= day:
            chosen = report
        else:
            break
    if chosen is None:
        return None
    if (day - date.fromisoformat(chosen["date"])).days > MAX_STALE_DAYS:
        return None
    return chosen


def run_pool(items: list, worker, workers: int, label: str) -> dict:
    results = {}
    done = 0
    total = len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            results[item] = future.result()
            done += 1
            if done % 25 == 0 or done == total:
                print(f"{label} {done}/{total}", flush=True)
    return results


def top_line(rows: list[dict], n: int = 8) -> str:
    ordered = sorted(rows, key=lambda row: (-row["mcap"], row["name"]))
    return "; ".join(f"{row['name']} ${row['price']:.2f} {row['mcap']/1e9:.0f}B" for row in ordered[:n])


def check_known_prices(reports: list[dict], prices: dict[str, dict]) -> None:
    checked = Counter()
    bad = Counter()
    for symbol in ("AMGN", "GILD", "VRTX"):
        bars = (prices.get(symbol) or {}).get("bars") or {}
        for report in reports:
            holding = next((row for row in report["holdings"] if row.get("ticker") == symbol and row.get("accepted")), None)
            if holding is None or not holding.get("shares"):
                continue
            close = bars.get(report["date"])
            if not close:
                continue
            implied = holding["value"] / holding["shares"]
            if implied <= 0:
                continue
            err = abs(close - implied) / implied
            checked[symbol] += 1
            if err > 0.25:
                bad[symbol] += 1
                print(
                    f"price check {symbol} {report['date']} filing ${implied:.2f} cnbc ${close:.2f} off {err:.0%}",
                    flush=True,
                )
    print(f"price checks {dict(checked)} outside 25% {dict(bad)}", flush=True)
    for symbol, count in checked.items():
        if count >= 4 and bad[symbol] / count > 0.35:
            raise SystemExit(f"{symbol} prices do not match the filing often enough")


def validate(snapshots: list[dict]) -> None:
    if len(snapshots) < 36:
        raise SystemExit(f"only {len(snapshots)} sessions")
    counts = [len(item["rows"]) for item in snapshots]
    print(
        f"sessions {len(snapshots)} names min/median/max "
        f"{min(counts)}/{sorted(counts)[len(counts)//2]}/{max(counts)}",
        flush=True,
    )
    by_year: dict[str, list[int]] = defaultdict(list)
    for item in snapshots:
        by_year[item["date"][:4]].append(len(item["rows"]))
    for year in sorted(by_year):
        vals = sorted(by_year[year])
        print(f"coverage {year} sessions={len(vals)} names {vals[0]}/{vals[len(vals)//2]}/{vals[-1]}", flush=True)
    recent = min(snapshots, key=lambda item: abs((date.fromisoformat(item["date"]) - date(2024, 6, 15)).days))
    print(f"check 2024 via {recent['date']}: {top_line(recent['rows'])}", flush=True)
    leaders = {row["name"].split()[0] for row in sorted(recent["rows"], key=lambda row: -row["mcap"])[:8]}
    expected = {"Amgen", "Gilead", "Vertex", "Regeneron", "AbbVie", "Moderna", "Alnylam", "Biogen"}
    if not (leaders & expected):
        raise SystemExit(f"2024 leaders look wrong: {leaders}")


def self_test() -> None:
    mapping: dict[str, str] = {}
    first = assign_session_id("2016-03-31", mapping)
    again = assign_session_id("2016-03-31", mapping)
    other = assign_session_id("2020-06-30", mapping)
    assert first == again and first != other
    assert SESSION_ID_RE.fullmatch(first)
    assert clean_name("Amgen Inc.") == "Amgen"
    assert clean_name("BIOGEN IDEC INC") == "Biogen Idec"
    assert clean_holding_name("Alexion Pharmaceuticals, Inc. (a)") == "Alexion Pharmaceuticals"
    assert compatible("Biogen Idec, Inc.", "Biogen Inc.")
    assert compatible("Amgen, Inc.", "Amgen Inc")
    assert not compatible("Genentech, Inc.", "Ginkgo Bioworks")
    assert not compatible("United Therapeutics", "United Airlines")
    assert norm_key("Isis Pharmaceuticals, Inc.") == "isis pharmaceutical"
    dotted = """
SPDR BIOTECH ETF
SCHEDULE OF INVESTMENTS
MARCH 31, 2006 (UNAUDITED)
COMMON STOCKS -- 100.0%
BIOTECHNOLOGY -- 100.0%
Amgen, Inc. (a)........................    15,568   $ 1,132,572
Charles River Laboratories
  International, Inc. (a)..............    22,740     1,114,715
SHORT TERM INVESTMENTS -- 0.1%
"""
    parsed = extract_schedule(dotted)
    by_name = {row["name"]: row for row in parsed}
    assert by_name["Amgen"]["shares"] == 15568
    assert by_name["Amgen"]["value"] == 1132572
    assert "Charles River Laboratories International" in by_name
    assert by_name["Charles River Laboratories International"]["shares"] == 22740
    lined = """
SPDR S&P Biotech ETF
SCHEDULE OF INVESTMENTS
September 30, 2010
COMMON STOCKS — 99.9%
BIOTECHNOLOGY — 99.9%
Amgen, Inc. (b)
299,484
$
16,504,563
Gilead Sciences, Inc. (b)
473,380
16,857,062
SHORT TERM INVESTMENTS — 22.1%
"""
    lined_rows = {row["name"]: row for row in extract_schedule(lined)}
    assert lined_rows["Amgen"]["shares"] == 299484
    assert lined_rows["Gilead Sciences"]["value"] == 16857062
    bars = {
        "2020-08-27": 500.0,
        "2020-08-28": 502.0,
        "2020-08-31": 126.0,
        "2020-09-01": 127.0,
    }
    divisors = price_divisors(bars)
    assert abs(divisors["2020-08-28"] - 4) < 1e-9
    assert shares_on([["2016-03-31", 100.0], ["2016-06-30", 110.0]], "2016-08-01") == 110.0
    assert shares_on([["2016-06-30", 110.0]], "2016-01-01") is None
    print("self-test ok")


def build(workers: int) -> None:
    reports = load_holdings()
    listings = listing_names()
    resolve_reports(reports, listings)
    tickers = sorted({row["ticker"] for report in reports for row in report["holdings"] if row.get("ticker")})
    print(f"candidate tickers {len(tickers)}", flush=True)
    quotes = fetch_quotes(tickers)
    accepted = []
    rejected = Counter()
    for report in reports:
        for holding in report["holdings"]:
            ticker = holding.get("ticker") or ""
            if accept_ticker(holding, quotes.get(ticker)):
                holding["accepted"] = True
                accepted.append(ticker)
            else:
                holding["accepted"] = False
                if ticker:
                    rejected[ticker] += 1
    print(f"accepted ticker rows {len(accepted)} distinct {len(set(accepted))}", flush=True)
    print("often rejected:", ", ".join(f"{name}×{n}" for name, n in rejected.most_common(12)), flush=True)

    cnbc_limit = Limiter(6)
    sec_limit = Limiter(5)
    symbols = sorted(set(accepted))

    def price_worker(symbol: str) -> dict:
        return fetch_prices(symbol, cnbc_limit)

    prices = run_pool(symbols, price_worker, workers, "prices")
    priced = sum(1 for item in prices.values() if item.get("bars"))
    print(f"price series with bars {priced}/{len(symbols)}", flush=True)
    calendar = prices.get("XBI") or fetch_prices("XBI", cnbc_limit)
    if not calendar.get("bars"):
        raise SystemExit("could not load XBI prices; CNBC may be blocking this machine")
    prices["XBI"] = calendar

    sec_map = download_sec_tickers()
    ciks = sorted({sec_map[symbol] for symbol in symbols if symbol in sec_map})

    def share_worker(cik: int) -> dict:
        if (CACHE / "shares" / f"{cik}.json").exists():
            return fetch_shares(cik)
        sec_limit.wait()
        return fetch_shares(cik)

    shares = run_pool(ciks, share_worker, min(workers, 4), "shares")
    divisors = {
        symbol: price_divisors(item["bars"])
        for symbol, item in prices.items()
        if item.get("bars")
    }
    check_known_prices(reports, prices)

    first_report = date.fromisoformat(reports[0]["date"])
    last_report = date.fromisoformat(reports[-1]["date"])
    sessions = pick_sessions(calendar["bars"], first_report, last_report)
    print(f"candidate sessions {len(sessions)} {sessions[0]} → {sessions[-1]}", flush=True)

    snapshots = []
    source_counts = Counter()
    drop_counts = Counter()
    skipped = []
    for day in sessions:
        report = holdings_on(reports, day)
        if report is None:
            skipped.append(day.isoformat())
            continue
        iso = day.isoformat()
        draft = []
        used = set()
        ordered = sorted(
            (row for row in report["holdings"] if row.get("accepted")),
            key=lambda row: -row["value"],
        )
        for holding in ordered:
            ticker = holding["ticker"]
            if ticker in used:
                continue
            series = (prices.get(ticker) or {}).get("bars") or {}
            price = series.get(iso)
            if not price:
                drop_counts[ticker] += 1
                continue
            cik = sec_map.get(ticker)
            sec_shares = shares_on((shares.get(cik) or {}).get("series") or [], iso) if cik else None
            sec_mcap = price * sec_shares if sec_shares else None
            alt = fallback_mcap(iso, price, divisors.get(ticker) or {}, quotes.get(ticker))
            ceiling = era_ceiling(day)
            mcap = None
            source = ""
            if sec_mcap and sec_mcap <= ceiling:
                mcap, source = sec_mcap, "sec"
            elif alt and alt <= ceiling:
                mcap, source = alt, "fallback"
            if not mcap or mcap < 10e6:
                drop_counts[ticker] += 1
                continue
            used.add(ticker)
            draft.append({
                "ticker": ticker,
                "name": holding["name"],
                "price": price,
                "mcap": mcap,
                "source": source,
            })
        names = Counter(row["name"] for row in draft)
        for row in draft:
            if names[row["name"]] > 1:
                row["name"] = f"{row['name']} ({row['ticker']})"
        if len(draft) < MIN_ROWS:
            skipped.append(iso)
            continue
        for row in draft:
            source_counts[row["source"]] += 1
        draft.sort(key=lambda row: (-row["mcap"], row["name"]))
        snapshots.append({"date": iso, "rows": draft, "report": report["date"]})

    print(f"mcap sources {dict(source_counts)}", flush=True)
    print(f"skipped months {len(skipped)}", flush=True)
    print("most often dropped:", ", ".join(f"{name}×{n}" for name, n in drop_counts.most_common(15)), flush=True)
    validate(snapshots)
    for sample in snapshots[:: max(1, len(snapshots) // 6)]:
        print(f"sample {sample['date']} portfolio {sample['report']} n={len(sample['rows'])} {top_line(sample['rows'])}", flush=True)

    id_map = load_session_ids()
    for snap in snapshots:
        snap["id"] = assign_session_id(snap["date"], id_map)
    save_session_ids(id_map)
    if SNAP_DIR.exists():
        for old in SNAP_DIR.glob("*.json"):
            old.unlink()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    for snap in snapshots:
        payload = {
            "d": snap["date"],
            "rows": [
                [row["name"], round(row["price"], 2), int(round(row["mcap"] / 1e6))]
                for row in snap["rows"]
            ],
        }
        atomic_write(SNAP_DIR / f"{snap['id']}.json", json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    years = sorted({int(snap["date"][:4]) for snap in snapshots})
    manifest = {
        "game": "xbi-tape",
        "yearMin": years[0],
        "yearMax": years[-1],
        "snapshots": sorted(snap["id"] for snap in snapshots),
        "built": date.today().isoformat(),
    }
    atomic_write(ROOT / "data" / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    total_bytes = sum(path.stat().st_size for path in SNAP_DIR.glob("*.json"))
    print(
        f"wrote {len(snapshots)} snapshots {years[0]}–{years[-1]}, {total_bytes/1e6:.1f} MB",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build static XBI sessions for the tape")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    build(max(1, args.workers))


if __name__ == "__main__":
    main()
