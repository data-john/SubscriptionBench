"""Standalone runner for Egress Allowlist Proxy."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path so subbench can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from subbench.provision.egress_proxy import (  # noqa: E402
    EgressProxyServer,
    is_ip_literal,
    load_allowlist,
    main,
    parse_args,
)

__all__ = ["EgressProxyServer", "is_ip_literal", "load_allowlist", "main", "parse_args"]

if __name__ == "__main__":
    asyncio.run(main())
