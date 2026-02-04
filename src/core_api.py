"""CORE.ac.uk API lookup for open access PDFs."""

import time

import requests

from src.core.log import get_logger

logger = get_logger()

BASE_URL = "https://api.core.ac.uk/v3/search/works"


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

    # Try DOI first, then title
    queries = []
    if doi:
        queries.append(f'doi:"{doi}"')
    if title:
        clean_title = title.replace('"', "")
        queries.append(f'title:"{clean_title}"')

    for query in queries:
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(
                    BASE_URL,
                    params={"q": query, "limit": 3},
                    timeout=15,
                    headers=headers,
                )

                if resp.status_code == 429:
                    if attempt < max_retries:
                        backoff = 5 * (backoff_factor ** attempt)
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
                    download_url = result.get("downloadUrl")
                    if download_url:
                        logger.info(f"CORE: found PDF for query {query[:60]}")
                        return download_url, False

                    # Also check links array
                    for link in result.get("links", []):
                        if link.get("type") == "download":
                            logger.info(f"CORE: found PDF via links for query {query[:60]}")
                            return link.get("url"), False

                break  # Success (200) but no PDF found — move to next query

            except requests.exceptions.RequestException as e:
                logger.warning(f"CORE API request failed: {e}")
                break
            finally:
                time.sleep(delay)

    return None, False
