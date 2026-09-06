"""Build identity, so it is always obvious which code is actually running.

Three deploys in a row silently served a stale image: the fix had been
merged, but the running container was still on older code, and the only
way to tell was noticing that an error message's exact wording hadn't
changed. That is a terrible diagnostic loop.

BUILD_MARKER is bumped whenever a change needs to be confirmed live. It
is logged at startup and served from /health and /status, so checking
what is deployed is one request:

    curl https://<your-app>/health

If the marker in the response doesn't match the one in this file on
main, the deployment is stale and the build almost certainly failed -
look at the platform's BUILD log, not the runtime log.
"""
from __future__ import annotations

# Bump this on any change whose deployment you need to verify.
BUILD_MARKER = "2026-09-06-gmgn-http-direct"

# One line on what this build changed, for the same diagnostic purpose.
BUILD_NOTE = (
    "GMGN over direct HTTP (openapi.gmgn.ai, X-APIKEY); gmgn-cli/Node "
    "dependency removed along with nixpacks.toml"
)
