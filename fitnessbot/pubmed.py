"""PubMed evidence lookup via NCBI E-utilities.

Free, no key required; set NCBI_API_KEY for higher rate limits (10 req/s vs 3).
Used to ground coaching answers in peer-reviewed literature — abstracts are fed
to the LLM to summarize, and citations (title + PubMed link) are appended
deterministically so links are never hallucinated.
"""

import logging

import httpx
from defusedxml import ElementTree as ET

from fitnessbot.config import Config

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TIMEOUT = 8.0


def _base_params(extra: dict) -> dict:
    params = {"db": "pubmed", "tool": "fitnessbot", "email": "support@fit-ness.ca"}
    if Config.NCBI_API_KEY:
        params["api_key"] = Config.NCBI_API_KEY
    params.update(extra)
    return params


def search_pmids(query: str, retmax: int = 4) -> list[str]:
    """Return a list of PubMed IDs most relevant to the query."""
    resp = httpx.get(
        f"{EUTILS_BASE}/esearch.fcgi",
        params=_base_params({
            "term": query,
            "retmax": retmax,
            "retmode": "json",
            "sort": "relevance",
        }),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


def _first_text(el, path: str) -> str:
    node = el.find(path)
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def parse_articles(xml_text: str) -> list[dict]:
    """Parse an efetch PubMed XML payload into article dicts."""
    root = ET.fromstring(xml_text)
    articles = []
    for art in root.findall(".//PubmedArticle"):
        pmid = _first_text(art, ".//PMID")
        title = _first_text(art, ".//Article/ArticleTitle")

        abstract_parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            txt = "".join(ab.itertext()).strip()
            if not txt:
                continue
            label = ab.get("Label")
            abstract_parts.append(f"{label}: {txt}" if label else txt)
        abstract = " ".join(abstract_parts)

        journal = _first_text(art, ".//Journal/ISOAbbreviation") or _first_text(art, ".//Journal/Title")
        year = _first_text(art, ".//JournalIssue/PubDate/Year") or _first_text(art, ".//JournalIssue/PubDate/MedlineDate")[:4]

        if not pmid or not title:
            continue
        articles.append({
            "pmid": pmid,
            "title": title,
            "abstract": abstract,
            "journal": journal,
            "year": year,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return articles


def fetch_articles(pmids: list[str]) -> list[dict]:
    """Fetch article metadata + abstracts for the given PubMed IDs."""
    if not pmids:
        return []
    resp = httpx.get(
        f"{EUTILS_BASE}/efetch.fcgi",
        params=_base_params({"id": ",".join(pmids), "retmode": "xml"}),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return parse_articles(resp.text)


def search_evidence(query: str, max_results: int = 4) -> list[dict]:
    """Search PubMed and return up to max_results articles. Never raises."""
    if not query or not query.strip():
        return []
    try:
        pmids = search_pmids(query, retmax=max_results)
        return fetch_articles(pmids)
    except Exception as e:
        logger.warning("PubMed lookup failed for %r: %s", query, e)
        return []
