"""Read-only providers for restricted research workers; no user-controlled URLs or file writes."""

import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


MAX_BYTES = 2 * 1024 * 1024
ALLOWED_HOSTS = {"export.arxiv.org", "api.adsabs.harvard.edu"}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname != urllib.parse.urlsplit(req.full_url).hostname or parsed.port not in {None, 443} or parsed.username or parsed.password:
            raise ValueError("Blocked provider redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url, headers=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in {None, 443} or parsed.username or parsed.password:
        raise ValueError("Provider URL is not allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "astro-agent/restricted-research", **(headers or {})})
    with urllib.request.build_opener(SafeRedirect()).open(request, timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Provider response exceeds size limit")
        return raw.decode("utf-8", errors="replace")


def _validate(query, max_results):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000 or any(ord(char) < 32 for char in query):
        raise ValueError("Query must be 1–1000 printable characters")
    if type(max_results) is not int or not 1 <= max_results <= 6:
        raise ValueError("Result limit must be 1–6")


def arxiv(query, max_results=4):
    _validate(query, max_results)
    params = urllib.parse.urlencode({"search_query": query, "start": 0, "max_results": max_results, "sortBy": "relevance"})
    root = ET.fromstring(_get("https://export.arxiv.org/api/query?" + params))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("a:entry", ns)[:max_results]:
        aid = entry.findtext("a:id", "", ns).split("/abs/")[-1]
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", aid):
            continue
        papers.append({"provider": "arxiv", "identifier": aid, "url": "https://arxiv.org/abs/" + aid,
                       "title": " ".join(entry.findtext("a:title", "", ns).split()),
                       "authors": [author.findtext("a:name", "", ns) for author in entry.findall("a:author", ns)][:10],
                       "published": entry.findtext("a:published", "", ns)[:10],
                       "abstract": " ".join(entry.findtext("a:summary", "", ns).split())[:12000]})
    return {"papers": papers}


def ads(query, max_results=4):
    _validate(query, max_results)
    token = os.getenv("ADS_TOKEN")
    if not token:
        return {"papers": [], "error": "ADS token not configured; use arXiv instead"}
    params = urllib.parse.urlencode({"q": query, "rows": max_results, "fl": "title,author,pubdate,bibcode,abstract,doi", "sort": "score desc"})
    data = json.loads(_get("https://api.adsabs.harvard.edu/v1/search/query?" + params, {"Authorization": f"Bearer {token}"}))
    papers = []
    for doc in data.get("response", {}).get("docs", [])[:max_results]:
        identifier = doc.get("bibcode", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9.&]{10,32}", identifier):
            continue
        papers.append({"provider": "ads", "identifier": identifier,
                       "url": "https://ui.adsabs.harvard.edu/abs/" + urllib.parse.quote(identifier, safe="") + "/abstract",
                       "title": (doc.get("title") or [""])[0], "authors": doc.get("author", [])[:10],
                       "published": doc.get("pubdate", "")[:10], "abstract": str(doc.get("abstract", ""))[:12000], "doi": doc.get("doi", [])})
    return {"papers": papers}


def object_data(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(char) < 32 for char in name):
        raise ValueError("Object name is invalid")
    from astroquery.simbad import Simbad

    client = Simbad()
    client.server = "simbad.harvard.edu"
    client.TIMEOUT = 20
    table = client.query_object(name)
    if table is None or not len(table):
        return {"error": "Object not found", "sources": []}
    row = table[0]
    return {"provider": "SIMBAD", "identifier": str(row["main_id"]),
            "text": f"Object: {name}\nSIMBAD ID: {row['main_id']}\nRA={float(row['ra']):.8f} deg\nDec={float(row['dec']):.8f} deg\nCoordinate frame: ICRS"}
