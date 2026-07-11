"""Entrypoint: `ewsmcp` (stdio by default, MCP_TRANSPORT=http to serve)."""

import asyncio
import logging
import sys


def main() -> None:
    from .config import get_settings

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        stream=sys.stderr,  # stdout belongs to MCP in stdio mode
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        if settings.mcp_transport == "http":
            from .http import serve_http
            asyncio.run(serve_http(settings))
        else:
            from .server import run_stdio
            asyncio.run(run_stdio(settings))
    except KeyboardInterrupt:
        print("shutting down", file=sys.stderr)


if __name__ == "__main__":
    main()
