#!/bin/sh
# Proves the agent-UID network restriction actually works, from that UID's
# own perspective, rather than trusting that the iptables rule is present in
# the table. Run this before trusting the restriction for anything real:
#
#   docker compose exec --user agent scheduler sh scripts/verify_agent_egress.sh
#
# --user agent matters: this has to run as the UID the rule targets (1001),
# not as polska or root, or it proves nothing about what the agent itself
# can reach.
set -eu

if [ "$(id -u)" != "1001" ]; then
    echo "FAIL: running as uid $(id -u), not 1001 (agent). Re-run with:" >&2
    echo "  docker compose exec --user agent scheduler sh scripts/verify_agent_egress.sh" >&2
    exit 1
fi

python3 - <<'PYEOF'
import socket
import sys

failures = []


def check(label, host, port, *, expect):
    """expect is "refused", "succeeds", or "blocked" (DNS itself refused)."""
    try:
        with socket.create_connection((host, port), timeout=5):
            outcome = "succeeded"
    except ConnectionRefusedError:
        outcome = "refused"
    except (TimeoutError, socket.timeout):
        outcome = "timed_out"
    except OSError as exc:
        outcome = f"error: {exc}"

    ok = {
        "refused": outcome == "refused",
        "succeeds": outcome == "succeeded",
    }[expect]
    print(f"{'PASS' if ok else 'FAIL'}: {label} -> {outcome} (wanted {expect})")
    if not ok:
        failures.append(label)


# An arbitrary host the agent has no legitimate reason to reach. Must be
# refused immediately (REJECT), not time out (which would mean DROP, or no
# rule at all and a real, slow network attempt).
check("arbitrary host (1.1.1.1:443)", "1.1.1.1", 443, expect="refused")

# The one destination the rule allows, pinned in /etc/hosts at container
# start so this never needs a DNS query. Must succeed.
check("api.anthropic.com:443", "api.anthropic.com", 443, expect="succeeds")

# A DNS lookup for a hostname NOT already pinned in /etc/hosts needs a real
# UDP/53 query, which the catch-all reject rule also covers. This is a
# different code path (getaddrinfo, not create_connection), so it is checked
# separately rather than assumed to follow from the two above.
try:
    socket.getaddrinfo("example.com", 443)
    print("FAIL: DNS lookup for example.com succeeded; it should have been refused")
    failures.append("dns lookup")
except OSError as exc:
    print(f"PASS: DNS lookup for example.com -> refused ({exc})")

if failures:
    print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
    sys.exit(1)
print("\nAll checks passed: the agent UID can reach api.anthropic.com and nothing else.")
PYEOF
