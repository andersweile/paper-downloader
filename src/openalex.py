"""OpenAlex API lookup for open access PDFs.

OpenAlex is a free, open catalog of the global research system with excellent
coverage of OA copies. It often finds PDFs that Unpaywall and CORE miss.

API docs: https://docs.openalex.org/
"""

import time
from urllib.parse import quote

import requests

from src.core.log import get_logger

logger = get_logger()

BASE_URL = "https://api.openalex.org"


def find_pdf_url(
    doi: str | None = None,
    title: str | None = None,
    email: str | None = None,
    delay: float = 0.1,
) -> str | None:
    """Search OpenAlex for an open access PDF URL by DOI or title.

    Args:
        doi: DOI to search for (preferred, exact match).
        title: Paper title to search for (fallback, fuzzy search).
        email: Contact email for polite pool (better rate limits).
        delay: Seconds to wait after each request.

    Returns:
        PDF URL if found, None otherwise.
    """
    if not doi and not title:
        return None

    # OpenAlex polite pool: add email to get better rate limits
    headers = {"Accept": "application/json"}
    params = {}
    if email:
        params["mailto"] = email

    # Try DOI first (exact match via works endpoint)
    if doi:
        url = _find_by_doi(doi, headers, params)
        if url:
            return url
        time.sleep(delay)

    # Fallback to title search
    if title:
        url = _find_by_title(title, headers, params)
        if url:
            return url
        time.sleep(delay)

    return None


def _find_by_doi(doi: str, headers: dict, params: dict) -> str | None:
    """Look up a work by DOI and extract OA PDF URL."""
    # OpenAlex accepts DOI with or without https://doi.org/ prefix
    clean_doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    endpoint = f"{BASE_URL}/works/doi:{clean_doi}"

    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=15)

        if resp.status_code == 404:
            logger.debug(f"OpenAlex: DOI not found: {doi}")
            return None

        if resp.status_code != 200:
            logger.warning(f"OpenAlex API returned {resp.status_code} for DOI: {doi}")
            return None

        return _extract_pdf_url(resp.json())

    except requests.exceptions.RequestException as e:
        logger.warning(f"OpenAlex API request failed for DOI {doi}: {e}")
        return None


def _find_by_title(title: str, headers: dict, params: dict) -> str | None:
    """Search for a work by title and extract OA PDF URL."""
    # Use title.search filter for fuzzy matching
    search_params = {**params, "filter": f"title.search:{quote(title)}", "per_page": 3}
    endpoint = f"{BASE_URL}/works"

    try:
        resp = requests.get(endpoint, headers=headers, params=search_params, timeout=15)

        if resp.status_code != 200:
            logger.warning(f"OpenAlex API returned {resp.status_code} for title search")
            return None

        data = resp.json()
        results = data.get("results", [])

        for result in results:
            pdf_url = _extract_pdf_url(result)
            if pdf_url:
                return pdf_url

        return None

    except requests.exceptions.RequestException as e:
        logger.warning(f"OpenAlex API request failed for title search: {e}")
        return None


def _extract_pdf_url(work: dict) -> str | None:
    """Extract the best available OA PDF URL from an OpenAlex work object.

    OpenAlex provides multiple OA location fields:
    - open_access.oa_url: Best OA URL (may be landing page)
    - best_oa_location.pdf_url: Direct PDF link from best location
    - primary_location.pdf_url: PDF from primary source
    - locations[].pdf_url: All known PDF locations
    """
    # Check if the work is open access
    oa_info = work.get("open_access", {})
    if not oa_info.get("is_oa", False):
        return None

    # Priority 1: best_oa_location.pdf_url (most reliable direct PDF)
    best_location = work.get("best_oa_location") or {}
    pdf_url = best_location.get("pdf_url")
    if pdf_url:
        logger.info("OpenAlex: found PDF via best_oa_location")
        return pdf_url

    # Priority 2: primary_location.pdf_url
    primary = work.get("primary_location") or {}
    pdf_url = primary.get("pdf_url")
    if pdf_url:
        logger.info("OpenAlex: found PDF via primary_location")
        return pdf_url

    # Priority 3: Search all locations for a PDF URL
    locations = work.get("locations") or []
    for loc in locations:
        pdf_url = loc.get("pdf_url")
        if pdf_url:
            logger.info("OpenAlex: found PDF via locations array")
            return pdf_url

    # Priority 4: Fall back to oa_url if it looks like a PDF
    oa_url = oa_info.get("oa_url")
    if oa_url and (".pdf" in oa_url.lower() or "/pdf/" in oa_url.lower()):
        logger.info("OpenAlex: found PDF-like oa_url")
        return oa_url

    # No direct PDF URL found
    logger.debug("OpenAlex: work is OA but no PDF URL found")
    return None
