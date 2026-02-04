"""arXiv API search for preprint PDFs."""

import time
import xml.etree.ElementTree as ET

import requests

from src.core.log import get_logger

logger = get_logger()

ARXIV_API_URL = "http://export.arxiv.org/api/query"


def find_pdf_url(title: str, delay: float = 3.0) -> str | None:
    """Search arXiv by title and return a PDF URL if found.

    Args:
        title: Paper title to search for.
        delay: Seconds to wait after the request (rate limiting). arXiv recommends 3s.

    Returns:
        PDF URL string if found, None otherwise.
    """
    if not title:
        return None

    # Clean title for search
    clean_title = title.replace('"', "").replace("\n", " ").strip()
    query = f'ti:"{clean_title}"'

    try:
        resp = requests.get(
            ARXIV_API_URL,
            params={"search_query": query, "max_results": 3, "sortBy": "relevance"},
            timeout=15,
        )

        if resp.status_code != 200:
            logger.debug(f"arXiv API returned {resp.status_code}")
            return None

        # Parse Atom XML response
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        entries = root.findall("atom:entry", ns)
        for entry in entries:
            entry_title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")

            # Check title similarity (case-insensitive, strip whitespace)
            if _titles_match(clean_title, entry_title):
                # Find PDF link
                for link in entry.findall("atom:link", ns):
                    if link.get("title") == "pdf":
                        pdf_url = link.get("href")
                        if pdf_url:
                            # Ensure it ends with .pdf
                            if not pdf_url.endswith(".pdf"):
                                pdf_url += ".pdf"
                            logger.info(f"arXiv: found PDF for '{clean_title[:60]}'")
                            return pdf_url

        logger.debug(f"arXiv: no matching result for '{clean_title[:60]}'")
        return None

    except requests.exceptions.RequestException as e:
        logger.warning(f"arXiv API request failed: {e}")
        return None
    except ET.ParseError as e:
        logger.warning(f"arXiv API XML parse error: {e}")
        return None
    finally:
        time.sleep(delay)


def _titles_match(query_title: str, result_title: str) -> bool:
    """Check if two titles are similar enough to be the same paper."""
    q = query_title.lower().strip()
    r = result_title.lower().strip()

    # Exact match
    if q == r:
        return True

    # One contains the other (handles subtitles, etc.)
    if q in r or r in q:
        return True

    # Word-level overlap: require 80%+ of words to match
    q_words = set(q.split())
    r_words = set(r.split())
    if not q_words or not r_words:
        return False
    overlap = len(q_words & r_words) / max(len(q_words), len(r_words))
    return overlap >= 0.8
