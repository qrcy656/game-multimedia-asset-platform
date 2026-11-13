from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .common import redact
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Fantasy Main MCP adapter (stdio)")
    parser.add_argument("--config", required=True, help="Local JSON configuration; contains paths, never tool-supplied credentials")
    parser.add_argument("--check", action="store_true", help="Authenticate and report read-only capabilities, then exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        settings = Settings.load(args.config)
        if args.check:
            if settings.edition == "community":
                from .community import CommunityBackend as Backend
            else:
                from .private import PrivateBackend as Backend
            async def check():
                backend = Backend(settings)
                try:
                    return redact(await backend.capabilities())
                finally:
                    await backend.close()
            print(json.dumps(asyncio.run(check()), ensure_ascii=False, indent=2))
        else:
            from .server import build_server
            build_server(settings).run(transport="stdio")
    except Exception as exc:
        from .common import AdapterError
        message = str(exc) if isinstance(exc, AdapterError) else f"Invalid configuration or startup failure ({type(exc).__name__})"
        print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

