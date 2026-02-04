"""CLI entry point for downloading LBD systematic review papers."""

import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import click
from tqdm import tqdm

# Add project root to path so src imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.file_paths import DATA_DIR, PDF_DIR, get_manifest_path, get_source_json_path, load_settings
from src.core.log import get_logger
from src.doi import batch_lookup_dois, extract_dois_from_papers
from src.download import download_pdf, get_transform_urls
from src.manifest import (
    count_by_status,
    get_by_status,
    get_papers_with_doi,
    get_pending,
    init_manifest,
    load_manifest,
    save_manifest,
    update_entry,
)
from src.scholar import find_pdf_url as scholar_find_pdf_url
from src.scholar import setup_proxy
from src.unpaywall import find_pdf_url as unpaywall_find_pdf_url

logger = get_logger()


def load_papers() -> list[dict]:
    """Load papers from the source JSON file."""
    source_path = get_source_json_path()
    if not source_path.exists():
        click.echo(f"Error: Source JSON not found at {source_path}", err=True)
        sys.exit(1)
    with open(source_path) as f:
        data = json.load(f)
    return data["papers"]


def papers_with_open_access(papers: list[dict]) -> dict[str, str]:
    """Return {paperId: url} for papers with non-empty open access URLs."""
    result = {}
    for paper in papers:
        oa = paper.get("openAccessPdf", {})
        url = oa.get("url", "")
        if url:
            result[paper["paperId"]] = url
    return result


def enrich_dois(papers: list[dict], manifest: dict, settings: dict) -> None:
    """Phase 0: Extract and look up DOIs, store in manifest.

    Sources (in priority order):
    1. externalIds.DOI from paper metadata
    2. DOI extracted from openAccessPdf.disclaimer text
    3. Semantic Scholar batch API lookup
    """
    s2_settings = settings.get("s2_api", {})
    batch_size = s2_settings.get("batch_size", 500)
    s2_delay = s2_settings.get("delay_seconds", 1.0)

    # Extract DOIs from paper metadata (externalIds + disclaimer)
    local_dois = extract_dois_from_papers(papers)
    new_local = 0
    for pid, doi in local_dois.items():
        if pid in manifest and not manifest[pid].get("doi"):
            manifest[pid]["doi"] = doi
            new_local += 1

    click.echo(f"  DOIs from metadata/disclaimer: {len(local_dois)} total, {new_local} newly added to manifest")

    # Find papers still missing DOIs
    missing_doi_ids = [pid for pid in manifest if not manifest[pid].get("doi")]

    if missing_doi_ids:
        click.echo(f"  Looking up {len(missing_doi_ids)} papers via S2 batch API...")
        api_dois = batch_lookup_dois(missing_doi_ids, batch_size=batch_size, delay=s2_delay)

        new_api = 0
        for pid, doi in api_dois.items():
            if pid in manifest and not manifest[pid].get("doi"):
                manifest[pid]["doi"] = doi
                new_api += 1
        click.echo(f"  DOIs from S2 batch API: {len(api_dois)} found, {new_api} newly added")

    total_with_doi = sum(1 for entry in manifest.values() if entry.get("doi"))
    click.echo(f"  Total papers with DOI: {total_with_doi}/{len(manifest)}")


def run_unpaywall_phase(manifest: dict, settings: dict, dl_timeout: int, dl_retries: int, dl_delay: float) -> None:
    """Phase 2: Query Unpaywall for pending papers that have DOIs."""
    unpaywall_settings = settings.get("unpaywall", {})
    email = unpaywall_settings.get("email", "")
    up_delay = unpaywall_settings.get("delay_seconds", 0.1)

    if not email:
        click.echo("  Skipping Unpaywall: no email configured. Set unpaywall.email in settings.yaml.")
        return

    # Get pending papers with DOIs
    papers_with_dois = get_papers_with_doi(manifest, statuses=["pending"])

    if not papers_with_dois:
        click.echo("  No pending papers with DOIs for Unpaywall lookup.")
        return

    click.echo(f"  Querying Unpaywall for {len(papers_with_dois)} papers...")
    downloaded = 0
    no_pdf = 0

    for paper_id, doi in tqdm(papers_with_dois.items(), desc="Unpaywall", unit="paper"):
        pdf_url = unpaywall_find_pdf_url(doi, email=email, delay=up_delay)

        if pdf_url is None:
            no_pdf += 1
            continue  # Leave as pending for Scholar phase

        output_path = PDF_DIR / f"{paper_id}.pdf"
        success = download_pdf(pdf_url, output_path, timeout=dl_timeout, max_retries=dl_retries)

        if success:
            update_entry(
                manifest,
                paper_id,
                status="downloaded",
                source="unpaywall",
                url=pdf_url,
                file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
            )
            downloaded += 1
        else:
            # Don't mark as failed — Scholar can still try
            logger.debug(f"Unpaywall PDF download failed for {doi}, leaving as pending")

        time.sleep(dl_delay)

    click.echo(f"  Unpaywall: {downloaded} downloaded, {no_pdf} no PDF found (still pending)")


def run_url_transform_phase(manifest: dict, dl_timeout: int, dl_retries: int, dl_delay: float) -> None:
    """Phase 3/6: Retry failed papers using domain-specific URL transforms."""
    # Find failed papers that have a URL we can try to transform
    candidates = []
    for pid, entry in manifest.items():
        if entry["status"] == "failed" and entry.get("url"):
            transforms = get_transform_urls(entry["url"])
            if transforms:
                candidates.append((pid, entry["url"], transforms))

    if not candidates:
        click.echo("  No failed papers with transformable URLs.")
        return

    click.echo(f"  Trying URL transforms for {len(candidates)} papers...")
    downloaded = 0

    for paper_id, original_url, alt_urls in tqdm(candidates, desc="URL Transforms", unit="paper"):
        for alt_url in alt_urls:
            output_path = PDF_DIR / f"{paper_id}.pdf"
            success = download_pdf(alt_url, output_path, timeout=dl_timeout, max_retries=dl_retries)

            if success:
                update_entry(
                    manifest,
                    paper_id,
                    status="downloaded",
                    source="url_transform",
                    url=alt_url,
                    file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                )
                downloaded += 1
                break  # Stop trying alternatives once one works

            time.sleep(dl_delay)

    click.echo(f"  URL transforms: {downloaded} downloaded")


def run_repo_api_phase(
    manifest: dict, settings: dict, dl_timeout: int, dl_retries: int, dl_delay: float, vpn=None
) -> None:
    """Phase 5: Search open repository APIs (CORE, EuropePMC, arXiv)."""
    from src.arxiv_search import find_pdf_url as arxiv_find_pdf_url
    from src.core_api import find_pdf_url as core_find_pdf_url
    from src.europepmc import find_pdf_url as europepmc_find_pdf_url

    core_settings = settings.get("core", {})
    core_delay = core_settings.get("delay_seconds", 1.0)
    core_api_key = core_settings.get("api_key", "") or None
    core_max_retries = core_settings.get("max_retries", 3)
    core_backoff = core_settings.get("backoff_factor", 2.0)
    epmc_delay = settings.get("europepmc", {}).get("delay_seconds", 0.2)
    arxiv_delay = settings.get("arxiv", {}).get("delay_seconds", 3.0)

    # Get candidates: failed and not_found papers
    candidates = {pid: entry for pid, entry in manifest.items() if entry["status"] in ("failed", "not_found")}

    if not candidates:
        click.echo("  No failed/not_found papers for repository API lookup.")
        return

    click.echo(f"  Searching repository APIs for {len(candidates)} papers...")

    # --- 5a: CORE.ac.uk ---
    click.echo(f"\n  5a. CORE.ac.uk ({len(candidates)} papers)...")
    if core_api_key:
        click.echo("  Using CORE API key for higher rate limits.")
    if vpn and not vpn.has_failed_permanently():
        click.echo(f"  VPN rotation enabled: every {vpn.rotate_every_n} papers")

    core_downloaded = 0
    core_completed = 0
    consecutive_rate_limits = 0

    for pid, entry in tqdm(candidates.items(), desc="CORE", unit="paper"):
        # Proactive VPN rotation
        if vpn and not vpn.has_failed_permanently():
            if vpn.should_rotate_proactively(core_completed):
                click.echo(f"\n  Proactive VPN rotation after {core_completed} CORE papers...")
                if vpn.rotate():
                    consecutive_rate_limits = 0
                    click.echo("  Rotated to fresh IP.")

        doi = entry.get("doi")
        title = entry.get("title")
        pdf_url, was_rate_limited = core_find_pdf_url(
            doi=doi, title=title, delay=core_delay,
            api_key=core_api_key, max_retries=core_max_retries, backoff_factor=core_backoff,
        )

        # Reactive VPN rotation on persistent rate limit
        if was_rate_limited:
            consecutive_rate_limits += 1
            if vpn and not vpn.has_failed_permanently():
                click.echo(f"\n  CORE rate limited (streak: {consecutive_rate_limits}). Rotating VPN...")
                if vpn.rotate():
                    consecutive_rate_limits = 0
                    click.echo("  Rotated. Retrying paper with fresh IP...")
                    pdf_url, was_rate_limited = core_find_pdf_url(
                        doi=doi, title=title, delay=core_delay,
                        api_key=core_api_key, max_retries=core_max_retries, backoff_factor=core_backoff,
                    )
            elif consecutive_rate_limits >= 5:
                click.echo("\n  CORE: 5 consecutive rate limits without VPN. Aborting CORE phase.")
                break

        if not was_rate_limited:
            consecutive_rate_limits = 0

        if pdf_url:
            output_path = PDF_DIR / f"{pid}.pdf"
            success = download_pdf(pdf_url, output_path, timeout=dl_timeout, max_retries=dl_retries)
            if success:
                update_entry(
                    manifest,
                    pid,
                    status="downloaded",
                    source="core",
                    url=pdf_url,
                    file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                )
                core_downloaded += 1
                save_manifest(manifest, get_manifest_path())
            time.sleep(dl_delay)

        core_completed += 1

    click.echo(f"  CORE: {core_downloaded} downloaded")

    # Refresh candidates (remove newly downloaded)
    candidates = {pid: entry for pid, entry in manifest.items() if entry["status"] in ("failed", "not_found")}

    # --- 5b: EuropePMC ---
    click.echo(f"\n  5b. EuropePMC ({len(candidates)} papers)...")
    epmc_downloaded = 0
    for pid, entry in tqdm(candidates.items(), desc="EuropePMC", unit="paper"):
        doi = entry.get("doi")
        title = entry.get("title")
        pdf_url = europepmc_find_pdf_url(doi=doi, title=title, delay=epmc_delay)

        if pdf_url:
            output_path = PDF_DIR / f"{pid}.pdf"
            success = download_pdf(pdf_url, output_path, timeout=dl_timeout, max_retries=dl_retries)
            if success:
                update_entry(
                    manifest,
                    pid,
                    status="downloaded",
                    source="europepmc",
                    url=pdf_url,
                    file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                )
                epmc_downloaded += 1
                save_manifest(manifest, get_manifest_path())
            time.sleep(dl_delay)

    click.echo(f"  EuropePMC: {epmc_downloaded} downloaded")

    # Refresh candidates
    candidates = {pid: entry for pid, entry in manifest.items() if entry["status"] in ("failed", "not_found")}

    # --- 5c: arXiv ---
    click.echo(f"\n  5c. arXiv ({len(candidates)} papers)...")
    arxiv_downloaded = 0
    for pid, entry in tqdm(candidates.items(), desc="arXiv", unit="paper"):
        title = entry.get("title")
        if not title:
            continue
        pdf_url = arxiv_find_pdf_url(title=title, delay=arxiv_delay)

        if pdf_url:
            output_path = PDF_DIR / f"{pid}.pdf"
            success = download_pdf(pdf_url, output_path, timeout=dl_timeout, max_retries=dl_retries)
            if success:
                update_entry(
                    manifest,
                    pid,
                    status="downloaded",
                    source="arxiv",
                    url=pdf_url,
                    file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                )
                arxiv_downloaded += 1
                save_manifest(manifest, get_manifest_path())
            time.sleep(dl_delay)

    click.echo(f"  arXiv: {arxiv_downloaded} downloaded")
    click.echo(f"  Repository APIs total: {core_downloaded + epmc_downloaded + arxiv_downloaded} downloaded")


def run_crossref_phase(manifest: dict, settings: dict, dl_timeout: int, dl_retries: int, dl_delay: float) -> None:
    """Phase 7: Crossref DOI content negotiation."""
    from src.crossref import find_pdf_url as crossref_find_pdf_url

    crossref_delay = settings.get("crossref", {}).get("delay_seconds", 0.5)

    # Get failed/not_found papers with DOIs
    candidates = get_papers_with_doi(manifest, statuses=["failed", "not_found"])

    if not candidates:
        click.echo("  No failed/not_found papers with DOIs for Crossref lookup.")
        return

    click.echo(f"  Trying Crossref content negotiation for {len(candidates)} papers...")
    downloaded = 0

    for pid, doi in tqdm(candidates.items(), desc="Crossref", unit="paper"):
        pdf_url = crossref_find_pdf_url(doi=doi, delay=crossref_delay)

        if pdf_url:
            output_path = PDF_DIR / f"{pid}.pdf"
            success = download_pdf(pdf_url, output_path, timeout=dl_timeout, max_retries=dl_retries)
            if success:
                update_entry(
                    manifest,
                    pid,
                    status="downloaded",
                    source="crossref",
                    url=pdf_url,
                    file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                )
                downloaded += 1
                save_manifest(manifest, get_manifest_path())
            time.sleep(dl_delay)

    click.echo(f"  Crossref: {downloaded} downloaded")


def run_proxy_phase(manifest: dict, settings: dict, dl_timeout: int, dl_retries: int, dl_delay: float) -> None:
    """Phase 8: Retry paywall papers through institutional proxy."""
    from src.proxy import get_proxy_candidates

    proxy_settings = settings.get("proxy", {})
    proxy_base = proxy_settings.get("base_url", "")
    publisher_domains = proxy_settings.get("publisher_domains")

    if not proxy_base:
        click.echo("  Skipping institutional proxy: no proxy.base_url configured in settings.yaml.")
        return

    candidates = get_proxy_candidates(manifest, proxy_base, publisher_domains)

    if not candidates:
        click.echo("  No papers eligible for institutional proxy.")
        return

    click.echo(f"  Trying institutional proxy for {len(candidates)} papers...")
    downloaded = 0

    for pid, proxied_url in tqdm(candidates, desc="Institutional Proxy", unit="paper"):
        output_path = PDF_DIR / f"{pid}.pdf"
        success = download_pdf(proxied_url, output_path, timeout=dl_timeout, max_retries=dl_retries)

        if success:
            update_entry(
                manifest,
                pid,
                status="downloaded",
                source="institutional_proxy",
                url=proxied_url,
                file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
            )
            downloaded += 1
            save_manifest(manifest, get_manifest_path())

        time.sleep(dl_delay)

    click.echo(f"  Institutional proxy: {downloaded} downloaded")


def run_scihub_phase(manifest: dict, settings: dict, dl_timeout: int, dl_retries: int, dl_delay: float) -> None:
    """Phase 9: Sci-Hub lookup (opt-in only)."""
    from src.scihub import find_pdf_url as scihub_find_pdf_url

    scihub_settings = settings.get("scihub", {})
    mirrors = scihub_settings.get("mirrors")
    scihub_delay = scihub_settings.get("delay_seconds", 3.0)

    # Get failed/not_found papers with DOIs
    candidates = get_papers_with_doi(manifest, statuses=["failed", "not_found"])

    if not candidates:
        click.echo("  No failed/not_found papers with DOIs for Sci-Hub lookup.")
        return

    click.echo(f"  Trying Sci-Hub for {len(candidates)} papers...")
    downloaded = 0

    for pid, doi in tqdm(candidates.items(), desc="Sci-Hub", unit="paper"):
        pdf_url = scihub_find_pdf_url(doi=doi, mirrors=mirrors, delay=scihub_delay)

        if pdf_url:
            output_path = PDF_DIR / f"{pid}.pdf"
            success = download_pdf(pdf_url, output_path, timeout=dl_timeout, max_retries=dl_retries)
            if success:
                update_entry(
                    manifest,
                    pid,
                    status="downloaded",
                    source="scihub",
                    url=pdf_url,
                    file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                )
                downloaded += 1
                save_manifest(manifest, get_manifest_path())
            time.sleep(dl_delay)

    click.echo(f"  Sci-Hub: {downloaded} downloaded")


@click.group()
def cli():
    """LBD Systematic Review - PDF Download Pipeline."""
    pass


@cli.command()
@click.option("--open-access-only", is_flag=True, help="Only download papers with direct open access URLs.")
@click.option("--scholar-only", is_flag=True, help="Only search Google Scholar for pending papers.")
@click.option("--repos-only", is_flag=True, help="Only run repository API phases (CORE, EuropePMC, arXiv).")
@click.option("--scholar-delay", type=float, default=None, help="Seconds between Google Scholar requests.")
@click.option("--use-proxy", is_flag=True, help="Use free proxies for Google Scholar requests.")
@click.option("--use-vpn", is_flag=True, help="Use ExpressVPN IP rotation during Scholar phase.")
@click.option("--use-proxy-institutional", is_flag=True, help="Use institutional proxy for paywall papers.")
@click.option("--use-scihub", is_flag=True, help="Use Sci-Hub as last resort (explicit opt-in).")
@click.option("--retry-failed", is_flag=True, help="Retry papers marked as 'failed' (reset them to pending).")
@click.option("--retry-not-found", is_flag=True, help="Retry papers marked as 'not_found' (reset them to pending).")
@click.option("--unpaywall-email", type=str, default=None, help="Email for Unpaywall API (overrides settings.yaml).")
@click.option("--skip-unpaywall", is_flag=True, help="Skip the Unpaywall phase.")
def download(
    open_access_only: bool,
    scholar_only: bool,
    repos_only: bool,
    scholar_delay: float | None,
    use_proxy: bool,
    use_vpn: bool,
    use_proxy_institutional: bool,
    use_scihub: bool,
    retry_failed: bool,
    retry_not_found: bool,
    unpaywall_email: str | None,
    skip_unpaywall: bool,
):
    """Download PDFs for all papers in the source dataset."""
    settings = load_settings()
    manifest_path = get_manifest_path()
    dl_settings = settings.get("download", {})
    scholar_settings = settings.get("scholar", {})

    if scholar_delay is None:
        scholar_delay = scholar_settings.get("delay_seconds", 10.0)
    dl_delay = dl_settings.get("delay_seconds", 1.0)
    dl_timeout = dl_settings.get("timeout", 30)
    dl_retries = dl_settings.get("max_retries", 3)

    # Override unpaywall email if provided via CLI
    if unpaywall_email:
        settings.setdefault("unpaywall", {})["email"] = unpaywall_email

    # Initialize VPN if requested
    vpn = None
    if use_vpn:
        from src.vpn import VPNSwitcher

        vpn_config = settings.get("vpn", {})
        vpn = VPNSwitcher(vpn_config)

        if not vpn.is_available():
            click.echo("Error: --use-vpn specified but ExpressVPN CLI not found.", err=True)
            sys.exit(1)

        status = vpn.get_status()
        click.echo(f"VPN active: connected={status.connected}, location={status.location}, ip={status.ip}")

        if not status.connected:
            click.echo("  Performing initial VPN connection...")
            if vpn.rotate():
                status = vpn.get_status()
                click.echo(f"  Connected: location={status.location}, ip={status.ip}")
            else:
                click.echo("Error: Failed to establish initial VPN connection.", err=True)
                sys.exit(1)

    # Load papers and manifest
    papers = load_papers()
    manifest = load_manifest(manifest_path)
    manifest = init_manifest(papers, manifest)
    save_manifest(manifest, manifest_path)

    click.echo(f"Loaded {len(papers)} papers. Manifest: {dict(count_by_status(manifest))}")

    # Optionally reset failed/not_found papers to pending
    if retry_failed:
        failed_ids = get_by_status(manifest, "failed")
        for pid in failed_ids:
            manifest[pid]["status"] = "pending"
        if failed_ids:
            click.echo(f"Reset {len(failed_ids)} failed papers to pending.")
            save_manifest(manifest, manifest_path)

    if retry_not_found:
        nf_ids = get_by_status(manifest, "not_found")
        for pid in nf_ids:
            manifest[pid]["status"] = "pending"
        if nf_ids:
            click.echo(f"Reset {len(nf_ids)} not_found papers to pending.")
            save_manifest(manifest, manifest_path)

    # --- repos-only shortcut: jump directly to Phase 5 ---
    if repos_only:
        click.echo("\n--- Phase 5: Repository APIs (repos-only mode) ---")
        run_repo_api_phase(manifest, settings, dl_timeout, dl_retries, dl_delay, vpn=vpn)
        save_manifest(manifest, manifest_path)
        click.echo(f"\nFinal status: {dict(count_by_status(manifest))}")
        return

    # --- Phase 0: DOI Enrichment ---
    click.echo("\n--- Phase 0: DOI Enrichment ---")
    enrich_dois(papers, manifest, settings)
    save_manifest(manifest, manifest_path)

    # --- Phase 1: Open Access downloads ---
    if not scholar_only:
        oa_urls = papers_with_open_access(papers)
        # Filter to only pending papers
        pending_oa = {pid: url for pid, url in oa_urls.items() if manifest.get(pid, {}).get("status") == "pending"}

        if pending_oa:
            click.echo(f"\n--- Phase 1: Open Access ({len(pending_oa)} papers) ---")
            downloaded = 0
            failed = 0

            for paper_id, url in tqdm(pending_oa.items(), desc="Open Access", unit="paper"):
                output_path = PDF_DIR / f"{paper_id}.pdf"
                success = download_pdf(url, output_path, timeout=dl_timeout, max_retries=dl_retries)

                if success:
                    update_entry(
                        manifest,
                        paper_id,
                        status="downloaded",
                        source="open_access",
                        url=url,
                        file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                    )
                    downloaded += 1
                else:
                    update_entry(manifest, paper_id, status="failed", source="open_access", url=url)
                    failed += 1

                save_manifest(manifest, manifest_path)
                time.sleep(dl_delay)

            click.echo(f"Open Access phase: {downloaded} downloaded, {failed} failed")
        else:
            click.echo("\nNo pending open access papers to download.")

    # --- Phase 2: Unpaywall ---
    if not open_access_only and not skip_unpaywall:
        click.echo("\n--- Phase 2: Unpaywall ---")
        run_unpaywall_phase(manifest, settings, dl_timeout, dl_retries, dl_delay)
        save_manifest(manifest, manifest_path)

    # --- Phase 3: URL Transforms ---
    if not scholar_only and not open_access_only:
        click.echo("\n--- Phase 3: URL Transforms ---")
        run_url_transform_phase(manifest, dl_timeout, dl_retries, dl_delay)
        save_manifest(manifest, manifest_path)

    # --- Phase 4: Google Scholar ---
    if not open_access_only:
        pending_ids = get_pending(manifest)

        if pending_ids:
            click.echo(f"\n--- Phase 4: Google Scholar ({len(pending_ids)} papers) ---")

            if use_proxy or scholar_settings.get("use_proxy", False):
                setup_proxy()

            if vpn:
                click.echo(f"  VPN rotation enabled: every {vpn.rotate_every_n} papers (strategy: {vpn.strategy})")

            delay_after_rotation = scholar_settings.get("delay_after_rotation", 3.0)
            current_delay = scholar_delay
            downloaded = 0
            not_found = 0
            failed = 0
            scholar_completed = 0

            for paper_id in tqdm(pending_ids, desc="Google Scholar", unit="paper"):
                # Proactive VPN rotation
                if vpn and not vpn.has_failed_permanently():
                    if vpn.should_rotate_proactively(scholar_completed):
                        click.echo(f"\n  Proactive VPN rotation after {scholar_completed} papers...")
                        if vpn.rotate():
                            current_delay = delay_after_rotation
                            click.echo(f"  Rotated. Using reduced delay ({current_delay}s) for fresh IP.")

                title = manifest[paper_id]["title"]
                pdf_url, was_rate_limited = scholar_find_pdf_url(title, delay=current_delay)

                # Reactive VPN rotation on rate limit
                if was_rate_limited and vpn and not vpn.has_failed_permanently():
                    click.echo("\n  Rate limited! Rotating VPN...")
                    if vpn.rotate():
                        current_delay = delay_after_rotation
                        click.echo(f"  Rotated. Retrying paper with fresh IP (delay={current_delay}s)...")
                        pdf_url, was_rate_limited = scholar_find_pdf_url(title, delay=current_delay)
                    else:
                        click.echo("  VPN rotation failed. Marking paper as failed.")

                # If still rate limited after rotation (or no VPN), mark as failed
                if was_rate_limited:
                    update_entry(manifest, paper_id, status="failed", source="google_scholar")
                    failed += 1
                    save_manifest(manifest, manifest_path)
                    scholar_completed += 1
                    continue

                if pdf_url is None:
                    update_entry(manifest, paper_id, status="not_found", source="google_scholar")
                    not_found += 1
                else:
                    output_path = PDF_DIR / f"{paper_id}.pdf"
                    success = download_pdf(
                        url=pdf_url,
                        output_path=output_path,
                        timeout=dl_timeout,
                        max_retries=dl_retries,
                        referer="https://scholar.google.com/",
                    )

                    if success:
                        update_entry(
                            manifest,
                            paper_id,
                            status="downloaded",
                            source="google_scholar",
                            url=pdf_url,
                            file_path=str(output_path.relative_to(PDF_DIR.parent.parent)),
                        )
                        downloaded += 1
                    else:
                        update_entry(manifest, paper_id, status="failed", source="google_scholar", url=pdf_url)
                        failed += 1

                save_manifest(manifest, manifest_path)
                scholar_completed += 1

                # Gradually increase delay back to normal after rotation
                if current_delay < scholar_delay:
                    current_delay = min(current_delay + 1.0, scholar_delay)

            click.echo(f"Google Scholar phase: {downloaded} downloaded, {not_found} not found, {failed} failed")
        else:
            click.echo("\nNo pending papers for Google Scholar lookup.")

    # --- Phase 5: Repository APIs (CORE, EuropePMC, arXiv) ---
    if not open_access_only and not scholar_only:
        click.echo("\n--- Phase 5: Repository APIs ---")
        run_repo_api_phase(manifest, settings, dl_timeout, dl_retries, dl_delay, vpn=vpn)
        save_manifest(manifest, manifest_path)

    # --- Phase 6: Expanded URL Transforms (re-run on failed with new transforms) ---
    if not open_access_only and not scholar_only:
        click.echo("\n--- Phase 6: Expanded URL Transforms ---")
        run_url_transform_phase(manifest, dl_timeout, dl_retries, dl_delay)
        save_manifest(manifest, manifest_path)

    # --- Phase 7: Crossref Content Negotiation ---
    if not open_access_only and not scholar_only:
        click.echo("\n--- Phase 7: Crossref Content Negotiation ---")
        run_crossref_phase(manifest, settings, dl_timeout, dl_retries, dl_delay)
        save_manifest(manifest, manifest_path)

    # --- Phase 8: Institutional Proxy (opt-in) ---
    if use_proxy_institutional:
        click.echo("\n--- Phase 8: Institutional Proxy ---")
        run_proxy_phase(manifest, settings, dl_timeout, dl_retries, dl_delay)
        save_manifest(manifest, manifest_path)

    # --- Phase 9: Sci-Hub (opt-in) ---
    if use_scihub:
        click.echo("\n--- Phase 9: Sci-Hub ---")
        run_scihub_phase(manifest, settings, dl_timeout, dl_retries, dl_delay)
        save_manifest(manifest, manifest_path)

    # --- Summary ---
    click.echo(f"\nFinal status: {dict(count_by_status(manifest))}")


@cli.command(name="export-remaining")
def export_remaining():
    """Export remaining undownloaded papers to CSV for manual download."""
    manifest_path = get_manifest_path()
    manifest = load_manifest(manifest_path)

    if not manifest:
        click.echo("No manifest found. Run 'download' first.")
        return

    output_path = DATA_DIR / "manual_downloads.csv"
    remaining = []

    for pid, entry in manifest.items():
        if entry["status"] in ("failed", "not_found", "pending"):
            title = entry.get("title", "")
            authors = entry.get("authors", "")
            year = entry.get("year", "")
            doi = entry.get("doi", "")
            last_url = entry.get("url", "")
            scholar_query = quote_plus(title)
            suggested_search = f"https://scholar.google.com/scholar?q={scholar_query}"

            remaining.append(
                {
                    "paper_id": pid,
                    "title": title,
                    "authors": authors,
                    "year": year,
                    "doi": doi,
                    "status": entry["status"],
                    "last_url": last_url,
                    "suggested_search": suggested_search,
                }
            )

    if not remaining:
        click.echo("All papers have been downloaded!")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["paper_id", "title", "authors", "year", "doi", "status", "last_url", "suggested_search"]
        )
        writer.writeheader()
        writer.writerows(remaining)

    click.echo(f"Exported {len(remaining)} remaining papers to {output_path}")

    # Breakdown by status
    status_counts = Counter(r["status"] for r in remaining)
    for status, count in status_counts.most_common():
        click.echo(f"  {status}: {count}")


@cli.command()
def stats():
    """Show download statistics from the manifest."""
    manifest_path = get_manifest_path()
    manifest = load_manifest(manifest_path)

    if not manifest:
        click.echo("No manifest found. Run 'download' first.")
        return

    counts = count_by_status(manifest)
    total = len(manifest)

    click.echo(f"Total papers: {total}")
    click.echo(f"  Downloaded:  {counts.get('downloaded', 0)}")
    click.echo(f"  Pending:     {counts.get('pending', 0)}")
    click.echo(f"  Not found:   {counts.get('not_found', 0)}")
    click.echo(f"  Failed:      {counts.get('failed', 0)}")

    # DOI coverage
    with_doi = sum(1 for entry in manifest.values() if entry.get("doi"))
    click.echo(f"\nDOI coverage: {with_doi}/{total} ({100 * with_doi / total:.0f}%)")

    # Source breakdown for downloaded papers
    sources: dict[str, int] = {}
    for entry in manifest.values():
        if entry["status"] == "downloaded":
            src = entry.get("source", "unknown")
            sources[src] = sources.get(src, 0) + 1

    if sources:
        click.echo("\nDownloaded by source:")
        for src, count in sorted(sources.items()):
            click.echo(f"  {src}: {count}")

    # Failure domain breakdown
    domain_counts: Counter[str] = Counter()
    for entry in manifest.values():
        if entry["status"] == "failed" and entry.get("url"):
            parsed = urlparse(entry["url"])
            domain = parsed.hostname or "unknown"
            # Simplify domain (remove www. prefix)
            domain = domain.removeprefix("www.")
            domain_counts[domain] += 1

    if domain_counts:
        click.echo("\nFailed downloads by domain:")
        for domain, count in domain_counts.most_common(15):
            click.echo(f"  {domain}: {count}")


if __name__ == "__main__":
    cli()
