"""HTTP download with retries and PDF validation."""

import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from src.core.log import get_logger

logger = get_logger()

# Comprehensive browser headers to avoid 403s from academic publishers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def is_pdf(content: bytes) -> bool:
    """Check if content starts with PDF magic bytes."""
    return content[:5] == b"%PDF-"


def download_pdf(
    url: str, output_path: Path, timeout: int = 30, max_retries: int = 3, referer: str | None = None
) -> bool:
    """Download a PDF from url to output_path with retries.

    Args:
        url: URL to download PDF from
        output_path: Path to save the PDF file
        timeout: Timeout in seconds for HTTP request
        max_retries: Number of retry attempts
        referer: Optional Referer header (e.g., "https://scholar.google.com/" for Scholar results)

    Returns:
        True if download succeeded and file is a valid PDF.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a session for connection pooling and cookie persistence
    session = requests.Session()

    # Build headers with optional referer
    headers = HEADERS.copy()
    if referer:
        headers["Referer"] = referer

    for attempt in range(max_retries):
        try:
            resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()

            # Validate it's actually a PDF
            if not is_pdf(resp.content):
                content_type = resp.headers.get("content-type", "")
                logger.warning(f"Not a PDF (content-type: {content_type}): {url}")
                return False

            output_path.write_bytes(resp.content)
            return True

        except requests.exceptions.HTTPError as e:
            # Log response headers for 403 errors to help debug
            if e.response is not None and e.response.status_code == 403:
                logger.error(f"403 Forbidden for {url}")
                logger.debug(f"Response headers: {dict(e.response.headers)}")

            wait = 2**attempt
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"All {max_retries} attempts failed for {url}: {e}")
                return False

        except requests.exceptions.RequestException as e:
            wait = 2**attempt
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} failed for {url}: {e}. Retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"All {max_retries} attempts failed for {url}: {e}")
                return False

    return False


def get_transform_urls(url: str) -> list[str]:
    """Generate alternative download URLs for known academic domains.

    Some open access URLs point to HTML landing pages rather than direct PDF links.
    This function returns transformed URLs that are more likely to yield a PDF.

    Args:
        url: Original URL that failed to download.

    Returns:
        List of alternative URLs to try (may be empty).
    """
    parsed = urlparse(url)
    domain = parsed.hostname or ""
    path = parsed.path
    alternatives: list[str] = []

    # PMC: Various URL formats -> multiple PDF endpoints
    # Handles: /pmc/articles/PMC12345/, /pmc/articles/PMC12345/pdf/filename, etc.
    if "ncbi.nlm.nih.gov" in domain or "pmc" in domain:
        # Extract PMCID from various URL patterns
        pmc_match = re.search(r"(PMC\d+)", path, re.IGNORECASE)
        if pmc_match:
            pmc_id = pmc_match.group(1).upper()  # Normalize to uppercase
            # Try multiple PMC PDF endpoints
            # 1. EuropePMC PDF format
            alternatives.append(f"https://europepmc.org/articles/{pmc_id}?format=pdf")
            # 2. Direct NCBI PMC PDF (main article)
            alternatives.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/main.pdf")
            # 3. NCBI PMC PDF directory (may redirect)
            alternatives.append(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/pdf/")
            # 4. EuropePMC backend PDF
            alternatives.append(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmc_id}&blobtype=pdf")

    # bioRxiv / medRxiv: append .full.pdf if not already present
    if "biorxiv.org" in domain or "medrxiv.org" in domain:
        if not path.endswith(".pdf"):
            clean_path = path.rstrip("/")
            alternatives.append(f"https://{domain}{clean_path}.full.pdf")

    # MDPI: append /pdf to article URL
    if "mdpi.com" in domain:
        if "/pdf" not in path:
            clean_path = path.rstrip("/")
            alternatives.append(f"https://{domain}{clean_path}/pdf")

    # Springer: /article/ -> /content/pdf/ with .pdf extension
    if "link.springer.com" in domain:
        if "/article/" in path:
            pdf_path = path.replace("/article/", "/content/pdf/") + ".pdf"
            alternatives.append(f"https://{domain}{pdf_path}")

    # IEEE: /document/{id} -> stamp PDF endpoint
    if "ieeexplore.ieee.org" in domain:
        ieee_match = re.search(r"/document/(\d+)", path)
        if ieee_match:
            arnumber = ieee_match.group(1)
            alternatives.append(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?arnumber={arnumber}")

    # ACM: /doi/{path} -> /doi/pdf/{path}
    if "dl.acm.org" in domain:
        if "/doi/" in path and "/doi/pdf/" not in path:
            pdf_path = path.replace("/doi/", "/doi/pdf/", 1)
            alternatives.append(f"https://{domain}{pdf_path}")

    # OUP (Oxford University Press): append PDF format parameter
    if "academic.oup.com" in domain:
        if "pdfformat" not in path:
            alternatives.append(f"{url}?pdfformat=full")

    # doi.org links: resolve and extract the actual publisher URL
    if "doi.org" in domain:
        try:
            resp = requests.head(url, headers=HEADERS, allow_redirects=True, timeout=15)
            final_url = resp.url
            if final_url != url:
                # Recursively try transforms on the resolved URL
                alternatives.append(final_url)
                alternatives.extend(get_transform_urls(final_url))
        except requests.exceptions.RequestException:
            pass  # If we can't resolve, skip

    return alternatives
