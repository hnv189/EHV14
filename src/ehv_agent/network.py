from __future__ import annotations

import anthropic
import httpx

from .config import Config


def create_claude_client(
    config: Config | None = None, *, timeout: float, max_retries: int
) -> anthropic.Anthropic:
    """Use the same proxy policy for generation and account model scans."""
    options = {"timeout": timeout, "max_retries": max_retries}
    if config is None or config.proxy_mode == "system":
        return anthropic.Anthropic(**options)
    # Explicit modes override environment proxies and NO_PROXY consistently.
    http_client = anthropic.DefaultHttpxClient(
        # Supply a transport so SDK defaults cannot add environment proxy mounts.
        transport=httpx.HTTPTransport(
            proxy=config.proxy_url if config.proxy_mode == "custom" else None,
            trust_env=False,
        ),
        trust_env=False,
        timeout=timeout,
    )
    try:
        return anthropic.Anthropic(**options, http_client=http_client)
    except BaseException:
        http_client.close()
        raise
