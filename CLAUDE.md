# LBD Systematic Review - PDF Download Pipeline

## Purpose
Download PDFs for 408 literature-based discovery papers identified via Semantic Scholar. Multi-phase approach: DOI enrichment, open access URLs, Unpaywall, URL transforms, then Google Scholar fallback.

## Project Structure
```
lbd-systematic-review/
├── pyproject.toml              # Dependencies: click, requests, scholarly, pyyaml, tqdm
├── config/
│   └── settings.yaml           # Source JSON path, download/scholar/unpaywall/vpn settings
├── data/
│   ├── pdfs/                   # Downloaded PDFs (named {paperId}.pdf)
│   └── manifest.json           # Download status tracker (auto-generated)
├── src/
│   ├── core/
│   │   ├── file_paths.py       # Central path management
│   │   └── log.py              # get_logger() setup
│   ├── doi.py                  # DOI extraction (disclaimer text) + S2 batch API lookup
│   ├── download.py             # HTTP download with retries + PDF validation + URL transforms
│   ├── unpaywall.py            # Unpaywall API lookup for legal OA PDF URLs
│   ├── scholar.py              # Google Scholar PDF lookup via scholarly
│   ├── vpn.py                  # ExpressVPN IP rotation (proactive + reactive)
│   └── manifest.py             # Manifest read/write/query (includes doi field)
├── scripts/
│   └── download_papers.py      # CLI entry point (click)
└── CLAUDE.md
```

## Key Commands
```bash
# Full pipeline (all 5 phases)
uv run python scripts/download_papers.py download

# Open access only (fast, no scraping)
uv run python scripts/download_papers.py download --open-access-only

# Scholar only (for papers still pending)
uv run python scripts/download_papers.py download --scholar-only

# Adjust Scholar delay (default: 10s)
uv run python scripts/download_papers.py download --scholar-delay 15

# Use free proxies for Scholar
uv run python scripts/download_papers.py download --use-proxy

# Use ExpressVPN IP rotation for Scholar phase
uv run python scripts/download_papers.py download --use-vpn

# Scholar with VPN + retry not_found
uv run python scripts/download_papers.py download --scholar-only --use-vpn --retry-not-found

# Retry previously failed downloads
uv run python scripts/download_papers.py download --retry-failed

# Also retry not_found papers
uv run python scripts/download_papers.py download --retry-not-found

# Override Unpaywall email
uv run python scripts/download_papers.py download --unpaywall-email you@example.com

# Skip the Unpaywall phase
uv run python scripts/download_papers.py download --skip-unpaywall

# Show statistics (includes DOI coverage + failure domains)
uv run python scripts/download_papers.py stats
```

## Data Flow
1. Source: `../automated-discovery_literature-search/results/20260203_110525/papers/category_literature_based_discovery.json`
2. Papers loaded -> manifest initialized (pending status for new papers)
3. Phase 0: DOI enrichment (extract from metadata/disclaimer + S2 batch API)
4. Phase 1: Download from `openAccessPdf.url` where available
5. Phase 2: Unpaywall lookup for pending papers with DOIs
6. Phase 3: URL transforms for failed PMC/bioRxiv/MDPI papers
7. Phase 4: Google Scholar search by title (with optional VPN rotation)
8. Manifest updated after each paper (resumable)
9. PDFs saved as `data/pdfs/{paperId}.pdf`

## Manifest Statuses
- `pending`: Not yet attempted
- `downloaded`: PDF successfully saved
- `failed`: Download attempted but failed (retriable with `--retry-failed`)
- `not_found`: No PDF URL found via Google Scholar (retriable with `--retry-not-found`)

## Manifest Fields
Each entry stores: `title`, `authors`, `year`, `doi`, `status`, `source`, `url`, `file_path`, `timestamp`

## Download Sources
- `open_access`: Direct download from Semantic Scholar openAccessPdf URL
- `unpaywall`: Found via Unpaywall API (legal OA copy)
- `url_transform`: Downloaded using domain-specific URL transform (PMC, bioRxiv, MDPI)
- `google_scholar`: Found via Google Scholar search

## VPN IP Rotation
The `--use-vpn` flag enables ExpressVPN-based IP rotation during the Scholar phase to avoid rate limiting.

**Behavior:**
- **Proactive rotation**: Rotates IP every N papers (default: 20) to prevent rate limits
- **Reactive rotation**: On rate limit detection (`MaxTriesExceededException`), immediately rotates and retries
- **Reduced delay after rotation**: Uses `delay_after_rotation` (default: 3s) for fresh IPs, gradually increasing back to normal
- **Graceful degradation**: If VPN rotation fails repeatedly, continues without rotation

**Rotation strategies** (`vpn.rotation_strategy`):
- `smart` (default): Avoids the last 5 used locations
- `random`: Random choice from preferred locations
- `sequential`: Cycles through locations in order

**Requirements:** ExpressVPN CLI (`expressvpnctl`) must be installed and configured.

## Configuration
All settings in `config/settings.yaml`:
- `download.delay_seconds`: Delay between HTTP downloads (default: 1s)
- `download.timeout`: HTTP timeout (default: 30s)
- `download.max_retries`: Retry count with exponential backoff (default: 3)
- `s2_api.batch_size`: Papers per S2 batch API request (default: 500)
- `s2_api.delay_seconds`: Delay between S2 batch requests (default: 1s)
- `unpaywall.email`: Contact email for Unpaywall API (required)
- `unpaywall.delay_seconds`: Delay between Unpaywall requests (default: 0.1s)
- `scholar.delay_seconds`: Delay between Scholar queries (default: 10s)
- `scholar.use_proxy`: Use free proxies for Scholar (default: false)
- `scholar.delay_after_rotation`: Reduced delay after VPN rotation (default: 3s)
- `vpn.tool`: CLI binary name (default: `expressvpnctl`)
- `vpn.rotation_strategy`: `smart` / `random` / `sequential`
- `vpn.preferred_locations`: List of VPN server locations to rotate through
- `vpn.rotate_every_n_papers`: Papers between proactive rotations (default: 20)
- `vpn.connection_timeout`: Seconds to wait for VPN connection (default: 30)
- `vpn.max_rotation_failures`: Max consecutive failures before disabling rotation (default: 3)
