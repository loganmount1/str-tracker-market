"""HTTP client utilities with retry logic and rate limiting."""

import time
import random
import logging
from collections import defaultdict

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


class CollectionError(Exception):
    """Base error for collection failures."""
    pass


class RateLimitedError(CollectionError):
    """Platform returned 429."""
    pass


class BlockedError(CollectionError):
    """Request was blocked by anti-bot measures."""
    pass


class StructureChangedError(CollectionError):
    """Expected data structure not found."""
    pass


class RateLimiter:
    """Per-domain rate limiter with jitter."""

    def __init__(self, delays: dict = None, jitter: float = 2.0):
        self.delays = delays or {
            "airbnb.com": 3.0,
            "vrbo.com": 6.0,
            "booking.com": 10.0,
        }
        self.jitter = jitter
        self._last_request: dict[str, float] = defaultdict(float)

    def wait(self, domain: str) -> None:
        """Block until enough time has passed since last request to this domain."""
        delay = self.delays.get(domain, 5.0) + random.uniform(0, self.jitter)
        elapsed = time.time() - self._last_request[domain]
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request[domain] = time.time()


class ServerError(CollectionError):
    """Server returned 5xx — transient, worth retrying."""
    pass


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, ServerError)),
    before_sleep=lambda retry_state: logger.warning(
        f"Retrying request (attempt {retry_state.attempt_number})"
    ),
)
def make_request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """Make an HTTP request with automatic retry on transient failures."""
    kwargs.setdefault("timeout", 30)
    response = session.request(method, url, **kwargs)

    if response.status_code == 429:
        raise RateLimitedError(f"Rate limited by {url}")

    if response.status_code == 403:
        body = response.text[:500].lower()
        if any(w in body for w in ("captcha", "challenge", "blocked", "cloudflare")):
            raise BlockedError(f"Blocked by anti-bot on {url}")
        raise BlockedError(f"403 Forbidden from {url}")

    if response.status_code >= 500:
        raise ServerError(f"{response.status_code} Server Error for {url}")

    response.raise_for_status()
    return response


def create_session() -> requests.Session:
    """Create a requests session with realistic browser headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
    })
    return session
