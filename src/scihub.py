"""Sci-Hub PDF extraction (opt-in only)."""

import re
import time

import requests

from src.core.log import get_logger

logger = get_logger()

DEFAULT_MIRRORS = ["https://sci-hub.se", "https://sci-hub.st", "https://sci-hub.ru"]


def find_pdf_url(doi: str, mirrors: list[str] | None = None, delay: float = 3.0) -> str | None:
    """Look up a PDF URL on Sci-Hub by DOI.

    Parses the Sci-Hub page for the embedded PDF iframe/link.

    Args:
        doi: The DOI to look up.
        mirrors: List of Sci-Hub mirror URLs to try.
        delay: Seconds to wait between mirror attempts.

    Returns:
        Direct PDF URL string if found, None otherwise.
    """
    if not doi:
        return None

    if mirrors is None:
        mirrors = DEFAULT_MIRRORS

    for mirror in mirrors:
        try:
            url = f"{mirror}/{doi}"
            resp = requests.get(
                url,
                timeout=30,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                },
            )

            if resp.status_code != 200:
                logger.debug(f"Sci-Hub mirror {mirror} returned {resp.status_code}")
                time.sleep(delay)
                continue

            # Parse the page for PDF URL
            pdf_url = _extract_pdf_url(resp.text, mirror)
            if pdf_url:
                logger.info(f"Sci-Hub: found PDF for {doi} via {mirror}")
                return pdf_url

            logger.debug(f"Sci-Hub: no PDF link found on {mirror} for {doi}")

        except requests.exceptions.RequestException as e:
            logger.debug(f"Sci-Hub mirror {mirror} failed: {e}")

        time.sleep(delay)

    return None


def _extract_pdf_url(html: str, mirror_base: str) -> str | None:
    """Extract PDF URL from Sci-Hub HTML page.

    Looks for:
    1. iframe with src pointing to a PDF
    2. embed tag with PDF source
    3. Direct link to PDF
    """
    # Pattern 1: iframe src (most common)
    iframe_match = re.search(r'<iframe[^>]+src="([^"]+)"', html)
    if iframe_match:
        src = iframe_match.group(1)
        return _normalize_url(src, mirror_base)

    # Pattern 2: embed tag
    embed_match = re.search(r'<embed[^>]+src="([^"]+\.pdf[^"]*)"', html)
    if embed_match:
        src = embed_match.group(1)
        return _normalize_url(src, mirror_base)

    # Pattern 3: onclick or direct PDF link
    pdf_match = re.search(r'(https?://[^\s"\']+\.pdf)', html)
    if pdf_match:
        return pdf_match.group(1)

    return None


def _normalize_url(url: str, mirror_base: str) -> str:
    """Normalize a potentially relative URL to absolute."""
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{mirror_base}{url}"
    if not url.startswith("http"):
        return f"{mirror_base}/{url}"
    return url
