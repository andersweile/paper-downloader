"""Crossref API and DOI content negotiation for PDF URLs."""

import time

import requests

from src.core.log import get_logger

logger = get_logger()

CROSSREF_API_URL = "https://api.crossref.org/works"

# Headers for DOI content negotiation (request PDF directly)
PDF_HEADERS = {
    "Accept": "application/pdf",
    "User-Agent": "LBD-Systematic-Review/1.0 (mailto:aweilelarsen@gmail.com)",
}

# Headers for Crossref API (polite pool)
API_HEADERS = {
    "User-Agent": "LBD-Systematic-Review/1.0 (mailto:aweilelarsen@gmail.com)",
}


def find_pdf_url(doi: str, delay: float = 0.5) -> str | None:
    """Try to get a PDF URL via DOI content negotiation and Crossref API.

    Strategy 1: Content negotiation — request PDF directly from doi.org
    Strategy 2: Crossref API — look for PDF links in the work metadata

    Args:
        doi: The DOI to look up.
        delay: Seconds to wait between requests.

    Returns:
        PDF URL string if found, None otherwise.
    """
    if not doi:
        return None

    # Strategy 1: Content negotiation with doi.org
    url = _try_content_negotiation(doi)
    if url:
        return url

    time.sleep(delay)

    # Strategy 2: Crossref API metadata
    url = _try_crossref_api(doi)
    if url:
        return url

    time.sleep(delay)
    return None


def _try_content_negotiation(doi: str) -> str | None:
    """Request PDF directly via DOI content negotiation."""
    doi_url = f"https://doi.org/{doi}"
    try:
        # Use HEAD to check if we get redirected to a PDF without downloading it
        resp = requests.head(doi_url, headers=PDF_HEADERS, allow_redirects=True, timeout=15)

        content_type = resp.headers.get("content-type", "")
        if "application/pdf" in content_type:
            logger.info(f"Crossref content negotiation: direct PDF for {doi}")
            return resp.url

        # Some servers return 200 with a redirect to PDF
        if resp.status_code == 200 and resp.url != doi_url:
            # Check if the final URL looks like a PDF
            if resp.url.endswith(".pdf") or "pdf" in resp.url.lower():
                logger.info(f"Crossref content negotiation: PDF redirect for {doi}")
                return resp.url

    except requests.exceptions.RequestException as e:
        logger.debug(f"Content negotiation failed for {doi}: {e}")

    return None


def _try_crossref_api(doi: str) -> str | None:
    """Look up PDF links in Crossref work metadata."""
    try:
        resp = requests.get(f"{CROSSREF_API_URL}/{doi}", headers=API_HEADERS, timeout=15)

        if resp.status_code != 200:
            logger.debug(f"Crossref API returned {resp.status_code} for {doi}")
            return None

        data = resp.json()
        work = data.get("message", {})

        # Check link array for PDF entries
        for link in work.get("link", []):
            content_type = link.get("content-type", "")
            if "pdf" in content_type and link.get("URL"):
                logger.info(f"Crossref API: found PDF link for {doi}")
                return link["URL"]

        # Check resource -> primary -> URL (sometimes a direct PDF)
        resource_url = work.get("resource", {}).get("primary", {}).get("URL")
        if resource_url and resource_url.endswith(".pdf"):
            logger.info(f"Crossref API: found PDF resource URL for {doi}")
            return resource_url

    except requests.exceptions.RequestException as e:
        logger.debug(f"Crossref API request failed for {doi}: {e}")

    return None
