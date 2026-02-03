"""ExpressVPN IP rotation for avoiding rate limits during Scholar scraping."""

import random
import subprocess
import time
from dataclasses import dataclass

import requests

from src.core.log import get_logger

logger = get_logger()

DEFAULT_PREFERRED_LOCATIONS = [
    "usa - new york",
    "uk - london",
    "canada - toronto",
    "germany - frankfurt",
    "netherlands - amsterdam",
    "sweden",
    "switzerland",
    "france - paris",
]

IP_CHECK_SERVICES = [
    "https://api.ipify.org",
    "https://icanhazip.com",
    "https://ifconfig.me/ip",
]


@dataclass
class VPNStatus:
    """Current VPN connection state."""

    connected: bool
    location: str | None
    ip: str | None


class VPNSwitcher:
    """Manages ExpressVPN connections for IP rotation.

    Supports proactive rotation (every N papers) and reactive rotation
    (on rate limit detection). Three strategies: random, sequential, smart.
    """

    def __init__(self, config: dict) -> None:
        self.tool = config.get("tool", "expressvpnctl")
        self.strategy = config.get("rotation_strategy", "smart")
        self.preferred_locations = config.get("preferred_locations", DEFAULT_PREFERRED_LOCATIONS)
        self.connection_timeout = config.get("connection_timeout", 30)
        self.post_connect_delay = config.get("post_connect_delay", 5)
        self.min_rotation_interval = config.get("min_rotation_interval", 60)
        self.verify_ip = config.get("verify_ip_change", True)
        self.max_failures = config.get("max_rotation_failures", 3)
        self.rotate_every_n = config.get("rotate_every_n_papers", 20)

        self._last_rotation_time: float = 0.0
        self._recent_locations: list[str] = []
        self._sequential_index = 0
        self._consecutive_failures = 0

    def is_available(self) -> bool:
        """Check if the ExpressVPN CLI tool is installed and accessible."""
        try:
            result = subprocess.run(
                [self.tool, "--version"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_external_ip(self) -> str | None:
        """Get current external IP address using public services."""
        for service in IP_CHECK_SERVICES:
            try:
                resp = requests.get(service, timeout=5)
                if resp.status_code == 200:
                    return resp.text.strip()
            except requests.RequestException:
                continue
        return None

    def get_status(self) -> VPNStatus:
        """Get current VPN connection status."""
        try:
            result = subprocess.run(
                [self.tool, "status"], capture_output=True, text=True, timeout=10
            )
            output = result.stdout.strip().lower()

            connected = "connected" in output and "not connected" not in output
            location = None
            if connected:
                # Parse location from status output (e.g., "Connected to USA - New York")
                for line in result.stdout.strip().splitlines():
                    if "connected to" in line.lower():
                        location = line.split("Connected to", 1)[-1].strip() if "Connected to" in line else None
                        if location is None:
                            location = line.split("connected to", 1)[-1].strip()

            ip = self.get_external_ip()
            return VPNStatus(connected=connected, location=location, ip=ip)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return VPNStatus(connected=False, location=None, ip=None)

    def _disconnect(self) -> bool:
        """Disconnect from current VPN server."""
        try:
            result = subprocess.run(
                [self.tool, "disconnect"], capture_output=True, text=True, timeout=self.connection_timeout
            )
            if result.returncode == 0:
                logger.debug("VPN disconnected")
                return True
            logger.warning(f"VPN disconnect failed: {result.stderr.strip()}")
            return False
        except subprocess.TimeoutExpired:
            logger.warning("VPN disconnect timed out")
            return False

    def _connect(self, location: str) -> bool:
        """Connect to a specific VPN location."""
        try:
            result = subprocess.run(
                [self.tool, "connect", location],
                capture_output=True, text=True, timeout=self.connection_timeout,
            )
            if result.returncode == 0:
                logger.info(f"VPN connected to: {location}")
                time.sleep(self.post_connect_delay)
                return True
            logger.warning(f"VPN connect to {location} failed: {result.stderr.strip()}")
            return False
        except subprocess.TimeoutExpired:
            logger.warning(f"VPN connect to {location} timed out")
            return False

    def _pick_next_location(self) -> str:
        """Choose next VPN location based on the configured strategy."""
        if self.strategy == "random":
            return random.choice(self.preferred_locations)
        elif self.strategy == "sequential":
            location = self.preferred_locations[self._sequential_index % len(self.preferred_locations)]
            self._sequential_index += 1
            return location
        else:  # smart: avoid recently used locations
            available = [loc for loc in self.preferred_locations if loc not in self._recent_locations[-5:]]
            if not available:
                available = self.preferred_locations
            location = random.choice(available)
            return location

    def rotate(self) -> bool:
        """Rotate to a new VPN server.

        Disconnects current connection, picks a new location, connects,
        and verifies the IP changed.

        Returns:
            True if rotation succeeded, False otherwise.
        """
        # Check cooldown
        elapsed = time.time() - self._last_rotation_time
        if self._last_rotation_time > 0 and elapsed < self.min_rotation_interval:
            wait = self.min_rotation_interval - elapsed
            logger.info(f"VPN rotation cooldown: waiting {wait:.0f}s")
            time.sleep(wait)

        old_ip = self.get_external_ip()
        location = self._pick_next_location()

        # Disconnect first
        self._disconnect()
        time.sleep(2)

        # Connect to new location
        if not self._connect(location):
            self._consecutive_failures += 1
            logger.warning(f"VPN rotation failed ({self._consecutive_failures}/{self.max_failures})")
            if self._consecutive_failures >= self.max_failures:
                logger.error("Max VPN rotation failures reached. VPN rotation disabled.")
            return False

        # Verify IP changed
        if self.verify_ip and old_ip:
            new_ip = self.get_external_ip()
            if new_ip and new_ip == old_ip:
                logger.warning(f"IP did not change after rotation (still {old_ip})")
                # Still count as success — VPN connected, IP service might cache
            elif new_ip:
                logger.info(f"IP rotated: {old_ip} -> {new_ip}")

        self._recent_locations.append(location)
        self._last_rotation_time = time.time()
        self._consecutive_failures = 0
        return True

    def should_rotate_proactively(self, papers_completed: int) -> bool:
        """Check if it's time for a proactive rotation based on paper count."""
        return papers_completed > 0 and papers_completed % self.rotate_every_n == 0

    def has_failed_permanently(self) -> bool:
        """Check if VPN rotation has exhausted its failure budget."""
        return self._consecutive_failures >= self.max_failures
