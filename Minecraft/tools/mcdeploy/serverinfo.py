"""Server tokens (__SERVER_NAME__): facts about the target itself, filled in at render time.

__SERVER_NAME__ is the Velocity server name, the <Name> of the mc-backend-<Name> environment (the
same name the proxy lists in velocity.toml). Plugins that need a per-server id use it, so every
backend gets its own. It is not secret and needs no schema entry or test value. The proxy has no
server name, so the token is backend only.
"""
from __future__ import annotations

from .config import Target
from .errors import McDeployError
from .placeholders import TokenUse

SERVER_KIND = "server"
SERVER_NAMES = {"NAME"}


def check_server_tokens(uses: list[TokenUse], profile: str) -> list[str]:
    """Problems with the server tokens among `uses` in a file of the given profile ("proxy"/"backend")."""
    problems: list[str] = []
    for u in uses:
        if u.kind != SERVER_KIND:
            continue
        if u.name not in SERVER_NAMES:
            problems.append(f"{u.path}:{u.line}: unknown __SERVER_{u.name}__ (known: {sorted(SERVER_NAMES)})")
        elif profile == "proxy":
            problems.append(f"{u.path}:{u.line}: __SERVER_{u.name}__ is backend only, the proxy has no server name")
    return problems


def server_values(target: Target, names) -> dict[str, str]:
    """Value of every server token in `names` for this target."""
    if not names:
        return {}
    if not target.server_name:
        raise McDeployError(f"{target.name}: __SERVER_NAME__ is used, but this target has no server name")
    return {n: target.server_name for n in names}
