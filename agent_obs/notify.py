"""Best-effort push notifications for deploy/rollback events (PP29, T3.6.5).

Slack is the primary channel (``SLACK_WEBHOOK_URL`` — the same receiver the
Alertmanager config already uses); Telegram is optional and only fires when
both ``AGENT_OBS_TELEGRAM_BOT_TOKEN`` and ``AGENT_OBS_TELEGRAM_CHAT_ID`` are
set.

INVARIANT: notifications are a side effect of an operational action, never a
precondition for it. Every failure (network, HTTP status, missing driver) is
swallowed and reported as ``False`` — a dead webhook must not block or fail a
rollback. This mirrors how ``scripts/sampler/policy_engine.py`` treats audit
writes: best-effort, never raises.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Environment variables this module reads (documented in AGENTS.md).
SLACK_WEBHOOK_ENV = "SLACK_WEBHOOK_URL"
TELEGRAM_TOKEN_ENV = "AGENT_OBS_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ENV = "AGENT_OBS_TELEGRAM_CHAT_ID"

DEFAULT_TIMEOUT_SECONDS = 5.0


def slack_webhook_url(env: dict[str, str] | None = None) -> str:
    """Slack incoming-webhook URL, or "" when not configured."""
    source = os.environ if env is None else env
    return source.get(SLACK_WEBHOOK_ENV, "").strip()


def telegram_config(env: dict[str, str] | None = None) -> tuple[str, str]:
    """(bot_token, chat_id) for Telegram — ("", "") when not configured."""
    source = os.environ if env is None else env
    return (
        source.get(TELEGRAM_TOKEN_ENV, "").strip(),
        source.get(TELEGRAM_CHAT_ENV, "").strip(),
    )


def send_slack(
    text: str,
    client: httpx.Client | None = None,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Post ``text`` to the configured Slack webhook. Returns success.

    Never raises: an unconfigured webhook or any transport/HTTP error logs a
    warning and returns False.
    """
    url = slack_webhook_url(env)
    if not url:
        logger.info("notify: SLACK_WEBHOOK_URL not set, skipping Slack")
        return False

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        response = client.post(url, json={"text": text})
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — notifications never propagate
        logger.warning("notify: Slack post failed: %s", exc)
        return False
    finally:
        if owns_client:
            client.close()


def send_telegram(
    text: str,
    client: httpx.Client | None = None,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bool:
    """Post ``text`` to the configured Telegram bot chat. Returns success.

    Skips silently (returns False) unless BOTH token and chat id are set.
    Never raises.
    """
    token, chat_id = telegram_config(env)
    if not token or not chat_id:
        return False

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — notifications never propagate
        logger.warning("notify: Telegram post failed: %s", exc)
        return False
    finally:
        if owns_client:
            client.close()


def notify(
    text: str,
    client: httpx.Client | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fan ``text`` out to every configured channel.

    Returns a per-channel result map so callers (and tests) can assert delivery
    without parsing logs::

        {"slack": True, "telegram": False}
    """
    return {
        "slack": send_slack(text, client=client, env=env),
        "telegram": send_telegram(text, client=client, env=env),
    }


def format_rollback_message(
    new_version: str,
    old_version: str,
    burn_rates: dict[str, float],
    threshold: float,
    window: str = "30m",
    error: str | None = None,
) -> str:
    """One-line rollback message shared by Slack and Telegram.

    Kept here (not in ``canary.py``) so the payload format is unit-testable
    without importing the CLI.
    """
    rates = ", ".join(f"{name}={value:.2f}" for name, value in sorted(burn_rates.items()))
    message = (
        f"🚨 CANARY ROLLBACK: {new_version} → {old_version} | "
        f"burn rate over {window} exceeded {threshold} ({rates or 'no data'})"
    )
    if error:
        message = f"{message} | error: {error}"
    return message
