"""PC21 — hourly cleanup of PII vault entries whose TTL has expired.

Runs out of process (see ``scripts/cron/scheduler.py`` for the scheduled
variant). The detection and audit logic lives in
``agent_obs.storage.maintenance.cleanup_vault_expired``.

Run manually::

    python scripts/cron/cleanup_vault.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from agent_obs.storage.maintenance import (
    DEFAULT_MAX_EVENTS,
    JOB_CLEANUP_VAULT,
    VAULT_PREFIX,
    build_hot_store,
    cleanup_vault_expired,
    run_cron_job,
)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_obs.cron.cleanup_vault")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record an audit event for every PII vault entry past its TTL."
    )
    parser.add_argument("--prefix", default=VAULT_PREFIX, help="KV v2 sub-path to scan")
    parser.add_argument(
        "--max-events",
        type=int,
        default=int(os.environ.get("AGENT_OBS_CRON_MAX_EVENTS", DEFAULT_MAX_EVENTS)),
        help="Cap on entries examined per run",
    )
    parser.add_argument(
        "--delete-expired",
        action="store_true",
        default=os.environ.get("AGENT_OBS_CRON_VAULT_DELETE_EXPIRED", "false").lower()
        in ("1", "true", "yes"),
        help="Also purge KV metadata of expired entries (off by default: Vault reclaims them)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Detect only, write nothing")
    parser.add_argument("--verify-tls", action="store_true", help="Verify the Vault TLS certificate")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_cron_job(
        JOB_CLEANUP_VAULT,
        cleanup_vault_expired,
        hot_store=build_hot_store(),
        prefix=args.prefix,
        max_events=args.max_events,
        delete_expired=args.delete_expired,
        dry_run=args.dry_run,
        verify_tls=args.verify_tls,
    )
    if result is None:
        logger.error("cleanup_vault failed")
        return 1
    print(
        f"cleanup_vault: scanned={result.scanned} expired={result.expired} "
        f"audited={result.audited} deleted={result.deleted} errors={result.errors}"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
