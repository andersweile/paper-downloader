"""CORE.ac.uk API lookup for open access PDFs."""

import time

import requests

from src.core.log import get_logger

logger = get_logger()

BASE_URL = "https://api.core.ac.uk/v3/search/works"


def find_pdf_url(doi: str | None = None, title: str | None = None, delay: float = 0.2) -> str | None:
    """Search CORE.ac.uk for a PDF URL by DOI or title.

    Args:
        doi: DOI to search for (preferred).
        title: Paper title to search for (fallback).
        delay: Seconds to wait after the request (rate limiting).

    Returns:
        PDF URL string if found, None otherwise.
    """
    if not doi and not title:
        return None

    # Try DOI first, then title
    queries = []
    if doi:
        queries.append(f'doi:"{doi}"')
    if title:
        # Quote the title for exact matching
        clean_title = title.replace('"', "")
        queries.append(f'title:"{clean_title}"')

    for query in queries:
        try:
            resp = requests.get(
                BASE_URL,
                params={"q": query, "limit": 3},
                timeout=15,
                headers={"Accept": "application/json"},
            )

            if resp.status_code == 429:
                logger.warning("CORE API rate limited, backing off")
                time.sleep(5)
                return None

            if resp.status_code != 200:
                logger.debug(f"CORE API returned {resp.status_code} for query: {query}")
                continue

            data = resp.json()
            results = data.get("results", [])

            for result in results:
                download_url = result.get("downloadUrl")
                if download_url:
                    logger.info(f"CORE: found PDF for query {query[:60]}")
                    return download_url

                # Also check links array
                for link in result.get("links", []):
                    if link.get("type") == "download":
                        logger.info(f"CORE: found PDF via links for query {query[:60]}")
                        return link.get("url")

        except requests.exceptions.RequestException as e:
            logger.warning(f"CORE API request failed: {e}")
        finally:
            time.sleep(delay)

    return None
