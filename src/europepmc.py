"""EuropePMC API lookup for open access PDFs."""

import time

import requests

from src.core.log import get_logger

logger = get_logger()

SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def find_pdf_url(doi: str | None = None, title: str | None = None, delay: float = 0.2) -> str | None:
    """Search EuropePMC for a PDF URL by DOI or title.

    Args:
        doi: DOI to search for (preferred).
        title: Paper title to search for (fallback).
        delay: Seconds to wait after the request (rate limiting).

    Returns:
        PDF URL string if found, None otherwise.
    """
    if not doi and not title:
        return None

    queries = []
    if doi:
        queries.append(f'DOI:"{doi}"')
    if title:
        clean_title = title.replace('"', "")
        queries.append(f'TITLE:"{clean_title}"')

    for query in queries:
        try:
            resp = requests.get(
                SEARCH_URL,
                params={"query": query, "format": "json", "resultType": "core", "pageSize": 3},
                timeout=15,
            )

            if resp.status_code != 200:
                logger.debug(f"EuropePMC returned {resp.status_code} for query: {query}")
                continue

            data = resp.json()
            results = data.get("resultList", {}).get("result", [])

            for result in results:
                pmcid = result.get("pmcid")
                if pmcid:
                    # Direct full text PDF endpoint
                    pdf_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextPDF"
                    logger.info(f"EuropePMC: found PMC PDF for {pmcid}")
                    return pdf_url

                # Some records have fullTextUrlList with PDF links
                url_list = result.get("fullTextUrlList", {}).get("fullTextUrl", [])
                for url_entry in url_list:
                    if url_entry.get("documentStyle") == "pdf" and url_entry.get("availability") == "Open access":
                        logger.info(f"EuropePMC: found OA PDF link for query {query[:60]}")
                        return url_entry.get("url")

        except requests.exceptions.RequestException as e:
            logger.warning(f"EuropePMC request failed: {e}")
        finally:
            time.sleep(delay)

    return None
