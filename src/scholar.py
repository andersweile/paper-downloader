"""Google Scholar PDF lookup via the scholarly library."""

import time

from scholarly import MaxTriesExceededException, scholarly

from src.core.log import get_logger

logger = get_logger()


def setup_proxy() -> None:
    """Configure scholarly to use free proxies for anti-blocking."""
    try:
        scholarly.use_proxy(scholarly.FreeProxies())
        logger.info("Configured scholarly with free proxy")
    except Exception as e:
        logger.warning(f"Failed to set up proxy: {e}. Continuing without proxy.")


def find_pdf_url(title: str, delay: float = 10.0) -> tuple[str | None, bool]:
    """Search Google Scholar by title and return a PDF URL if found.

    Args:
        title: Paper title to search for.
        delay: Seconds to wait after the request (rate limiting).

    Returns:
        Tuple of (url, was_rate_limited):
        - (url, False): PDF URL found
        - (None, False): no PDF found (normal)
        - (None, True): rate limited by Google Scholar
    """
    try:
        search = scholarly.search_pubs(title)
        result = next(search, None)

        if result is None:
            logger.debug(f"No Google Scholar result for: {title}")
            return None, False

        eprint_url = result.get("eprint_url")
        if eprint_url:
            logger.info(f"Found PDF via Scholar: {title[:60]}...")
            return eprint_url, False

        # Check pub_url as fallback (sometimes links to PDF)
        pub_url = result.get("pub_url", "")
        if pub_url and pub_url.endswith(".pdf"):
            logger.info(f"Found PDF via pub_url: {title[:60]}...")
            return pub_url, False

        logger.debug(f"Scholar result found but no PDF link: {title[:60]}...")
        return None, False

    except MaxTriesExceededException:
        logger.warning("Google Scholar rate limit hit (MaxTriesExceeded).")
        return None, True
    except StopIteration:
        return None, False
    except Exception as e:
        logger.warning(f"Scholar lookup failed for '{title[:60]}...': {e}")
        return None, False
    finally:
        time.sleep(delay)
