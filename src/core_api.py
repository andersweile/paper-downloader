"""CORE.ac.uk API lookup for open access PDFs."""

import re
import time

import requests

from src.core.log import get_logger

logger = get_logger()

BASE_URL = "https://api.core.ac.uk/v3/search/works"


def _build_doi_query(doi: str) -> str:
    """Build CORE API query for DOI search (unquoted format)."""
    return f"doi:{doi}"


def _build_title_query(title: str) -> str:
    """Build CORE API query for title search using word-based matching.

    CORE API doesn't support quoted phrase search well. Instead, we use
    parentheses with individual words for better results.
    """
    # Remove special characters that might break the query
    clean_title = re.sub(r'["\'\(\)\[\]\{\}:;,]', " ", title)
    # Split into words and filter short/common words
    words = [w for w in clean_title.split() if len(w) > 2]
    # Limit to first 10 significant words to avoid query length issues
    words = words[:10]
    if not words:
        return ""
    return f"title:({' '.join(words)})"


def _extract_pdf_url(result: dict) -> str | None:
    """Extract PDF URL from a CORE API result, checking multiple fields."""
    # Primary: downloadUrl field
    download_url = result.get("downloadUrl")
    if download_url:
        return download_url

    # Secondary: links array with type "download"
    for link in result.get("links", []):
        if link.get("type") == "download":
            url = link.get("url")
            if url:
                return url

    # Tertiary: sourceFulltextUrls array (often contains repository PDFs)
    source_urls = result.get("sourceFulltextUrls") or []
    for url in source_urls:
        if url and (".pdf" in url.lower() or "pdf" in url.lower()):
            return url

    # Also try fullText link if it looks like a PDF URL
    for link in result.get("links", []):
        url = link.get("url", "")
        if url and (".pdf" in url.lower() or "/pdf/" in url.lower()):
            return url

    return None


def find_pdf_url(
    doi: str | None = None,
    title: str | None = None,
    delay: float = 1.0,
    api_key: str | None = None,
    max_retries: int = 3,
    backoff_factor: float = 2.0,
) -> tuple[str | None, bool]:
    """Search CORE.ac.uk for a PDF URL by DOI or title.

    Args:
        doi: DOI to search for (preferred).
        title: Paper title to search for (fallback).
        delay: Seconds to wait after each request (rate limiting).
        api_key: CORE API key for higher rate limits (free at core.ac.uk).
        max_retries: Number of retries on 429 with exponential backoff.
        backoff_factor: Multiplier for backoff delay (e.g., 2.0 → 5s, 10s, 20s).

    Returns:
        Tuple of (PDF URL or None, was_rate_limited bool).
    """
    if not doi and not title:
        return None, False

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Build queries: DOI first (unquoted), then title (word-based)
    queries = []
    if doi:
        queries.append(_build_doi_query(doi))
    if title:
        title_query = _build_title_query(title)
        if title_query:
            queries.append(title_query)

    for query in queries:
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(
                    BASE_URL,
                    params={"q": query, "limit": 5},
                    timeout=15,
                    headers=headers,
                )

                if resp.status_code == 429:
                    if attempt < max_retries:
                        backoff = 5 * (backoff_factor**attempt)
                        logger.warning(f"CORE API rate limited, retry {attempt + 1}/{max_retries} after {backoff:.0f}s")
                        time.sleep(backoff)
                        continue
                    else:
                        logger.warning("CORE API rate limited, exhausted retries")
                        return None, True

                if resp.status_code != 200:
                    logger.debug(f"CORE API returned {resp.status_code} for query: {query}")
                    break  # Move on to next query (DOI → title fallback)

                data = resp.json()
                results = data.get("results", [])

                for result in results:
                    pdf_url = _extract_pdf_url(result)
                    if pdf_url:
                        logger.info(f"CORE: found PDF for query {query[:60]}")
                        return pdf_url, False

                break  # Success (200) but no PDF found — move to next query

            except requests.exceptions.RequestException as e:
                logger.warning(f"CORE API request failed: {e}")
                break
            finally:
                time.sleep(delay)

    return None, False
