"""Institutional proxy URL rewriting for paywall access."""

from urllib.parse import urlparse

from src.core.log import get_logger

logger = get_logger()

# Domains known to be behind paywalls that an institutional proxy can unlock
DEFAULT_PUBLISHER_DOMAINS = [
    "ieeexplore.ieee.org",
    "link.springer.com",
    "sciencedirect.com",
    "elsevier.com",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "academic.oup.com",
    "dl.acm.org",
    "tandfonline.com",
    "sagepub.com",
    "nature.com",
    "science.org",
    "jstor.org",
    "cambridge.org",
    "karger.com",
    "worldscientific.com",
    "degruyter.com",
    "emerald.com",
    "liebertpub.com",
    "ingentaconnect.com",
    "doi.org",
]


def rewrite_url(url: str, proxy_base: str, publisher_domains: list[str] | None = None) -> str | None:
    """Rewrite a URL to go through an institutional proxy.

    Args:
        url: Original publisher URL.
        proxy_base: Proxy prefix URL (e.g., "https://login.proxy.itu.dk/login?url=").
        publisher_domains: List of domains to proxy. Defaults to common academic publishers.

    Returns:
        Proxied URL string if the domain matches, None otherwise.
    """
    if not url or not proxy_base:
        return None

    if publisher_domains is None:
        publisher_domains = DEFAULT_PUBLISHER_DOMAINS

    parsed = urlparse(url)
    domain = parsed.hostname or ""

    # Check if domain matches any publisher
    for pub_domain in publisher_domains:
        if pub_domain in domain:
            proxied = f"{proxy_base}{url}"
            logger.debug(f"Proxy rewrite: {domain} -> {proxied[:80]}")
            return proxied

    return None


def get_proxy_candidates(
    manifest: dict, proxy_base: str, publisher_domains: list[str] | None = None
) -> list[tuple[str, str]]:
    """Get (paper_id, proxied_url) pairs for failed papers with publisher URLs.

    Args:
        manifest: The manifest dict.
        proxy_base: Proxy prefix URL.
        publisher_domains: Optional list of publisher domains.

    Returns:
        List of (paper_id, proxied_url) tuples.
    """
    candidates = []
    for pid, entry in manifest.items():
        if entry["status"] not in ("failed", "not_found"):
            continue
        url = entry.get("url")
        if not url:
            # For not_found papers with DOI, construct a doi.org URL
            doi = entry.get("doi")
            if doi:
                url = f"https://doi.org/{doi}"
            else:
                continue

        proxied = rewrite_url(url, proxy_base, publisher_domains)
        if proxied:
            candidates.append((pid, proxied))

    return candidates
