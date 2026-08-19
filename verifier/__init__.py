"""7-layer email verification system.

Layers:
  1. Syntax / normalization
  2. Domain + MX resolution
  3. Disposable / role-account filter
  4. Catch-all detection
  5. SMTP RCPT handshake
  6. Greylist-aware retry
  7. Confidence scoring

Designed for a 5-IP, 10-identity VPS pool. See README_verifier.md.
"""

from .config import Config
from .pool import RotationPool
from .state import StateStore
from .runner import Verifier

# Multi-VPS coordination (optional): a shared work queue with domain leasing and
# failover. LeasedStore is what the coordinator process runs; RemoteStore is the
# HTTP client each worker VPS uses in its place. See coordinator.py.
from .leased_store import LeasedStore
from .remote_store import RemoteStore

__all__ = [
    "Config", "RotationPool", "StateStore", "Verifier",
    "LeasedStore", "RemoteStore",
]
