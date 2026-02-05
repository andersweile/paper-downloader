"""Browser-based PDF download using nodriver for Cloudflare bypass."""

import asyncio
import shutil
import tempfile
from pathlib import Path

from src.core.log import get_logger

logger = get_logger()

# Domains where Cloudflare JavaScript challenges block curl/requests
BROWSER_PRIORITY_DOMAINS = {
    "sciencedirect.com",
    "academic.oup.com",
    "dl.acm.org",
    "emerald.com",
    "tandfonline.com",
    "onlinelibrary.wiley.com",
    "downloads.hindawi.com",
    "link.springer.com",
}


def should_try_browser(url: str) -> bool:
    """Check if URL is from a Cloudflare-protected domain that requires browser automation."""
    from urllib.parse import urlparse

    domain = urlparse(url).hostname or ""
    return any(d in domain for d in BROWSER_PRIORITY_DOMAINS)


async def _download_pdf_async(url: str, output_path: Path, timeout: int = 60) -> str:
    """Async browser download implementation.

    Returns:
        "ok" if PDF downloaded successfully,
        "not_pdf" if page didn't yield a PDF,
        "error" if browser automation failed.
    """
    try:
        import nodriver as uc
        import nodriver.cdp as cdp
    except ImportError:
        logger.error("nodriver not installed. Run: uv add nodriver")
        return "error"

    download_dir = tempfile.mkdtemp(prefix="paper_dl_")

    try:
        # Start headless Chrome with download directory configured
        browser = await uc.start(
            headless=True,
            browser_args=[
                f"--download.default_directory={download_dir}",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        # Configure download behavior via CDP
        page = await browser.get("about:blank")
        await page.send(cdp.browser.set_download_behavior(behavior="allow", download_path=download_dir))

        # Navigate to the URL
        logger.debug(f"Browser navigating to: {url}")
        page = await browser.get(url)

        # Wait for Cloudflare challenge to resolve (typically 3-5 seconds)
        await asyncio.sleep(5)

        # Check if we got redirected to a PDF viewer or download started
        # Wait a bit more for potential downloads
        await asyncio.sleep(3)

        # Look for downloaded PDF in the download directory
        pdfs = list(Path(download_dir).glob("*.pdf"))
        if pdfs:
            # Move the downloaded PDF to the output path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pdfs[0]), str(output_path))
            logger.info(f"Browser download succeeded: {url}")
            browser.stop()
            return "ok"

        # Last resort: try clicking a download button if present
        try:
            # Common download button selectors for academic sites
            download_selectors = [
                'a[href*=".pdf"]',
                'a[href*="pdf"]',
                'button[aria-label*="download"]',
                'a[aria-label*="PDF"]',
                ".pdf-download",
                "#downloadPdf",
            ]

            for selector in download_selectors:
                try:
                    elements = await page.select_all(selector)
                    if elements:
                        await elements[0].click()
                        await asyncio.sleep(5)  # Wait for download

                        pdfs = list(Path(download_dir).glob("*.pdf"))
                        if pdfs:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(pdfs[0]), str(output_path))
                            logger.info(f"Browser download via click succeeded: {url}")
                            browser.stop()
                            return "ok"
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"No download button found: {e}")

        logger.warning(f"Browser: no PDF obtained from {url}")
        browser.stop()
        return "not_pdf"

    except asyncio.TimeoutError:
        logger.error(f"Browser timeout for {url}")
        return "error"
    except Exception as e:
        logger.error(f"Browser download failed for {url}: {e}")
        return "error"
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)


def download_with_browser(url: str, output_path: Path, timeout: int = 60) -> str:
    """Synchronous wrapper for browser-based PDF download.

    Args:
        url: URL to download PDF from.
        output_path: Path to save the PDF file.
        timeout: Timeout in seconds.

    Returns:
        "ok" if download succeeded,
        "not_pdf" if page didn't yield a PDF,
        "error" if browser automation failed.
    """
    try:
        return asyncio.run(_download_pdf_async(url, output_path, timeout))
    except Exception as e:
        logger.error(f"Browser download wrapper failed: {e}")
        return "error"
