#!/bin/sh
# Runs as root, briefly, for exactly one reason: setting up the iptables rule
# that makes "the agent has no network access" a kernel fact instead of a
# sentence in a system prompt. Everything else in this image runs as an
# unprivileged user; this script's only job is to establish that rule (when
# asked to) and then get out of the way permanently.
#
# Two users exist in this image (see Dockerfile): `polska` (1000), which runs
# the long-lived scheduler/dashboard process and does the things that need
# real network access (git clone, npm ci, the Anthropic API), and `agent`
# (1001), which every Claude Agent SDK subprocess is dropped to before it
# ever runs a line of agent-controlled code (see runner.py's cli_path and the
# wrapper script the Dockerfile generates). This script's rule targets 1001
# specifically; 1000 is never touched by it.
set -eu

#: Must match the Dockerfile's own ENV of the same name exactly. Bumped
#: together whenever the enforcement scheme's shape changes, not just
#: whenever the Dockerfile changes -- see the Dockerfile's own comment on
#: this for the incident it closes: a build that failed partway through
#: (setcap missing) left the previous, unenforced image in place, and
#: `docker compose up` started it without complaint. A version mismatch
#: here means either that image predates this entirely, or its build
#: produced something incomplete; either way, refusing to start is the
#: correct response, not a false negative to work around.
_EXPECTED_ENFORCEMENT_VERSION=1

if [ "${POLSKA_ENFORCE_AGENT_EGRESS:-0}" = "1" ]; then
    if [ "${POLSKA_AGENT_ENFORCEMENT_VERSION:-}" != "$_EXPECTED_ENFORCEMENT_VERSION" ]; then
        echo "entrypoint: POLSKA_AGENT_ENFORCEMENT_VERSION is '${POLSKA_AGENT_ENFORCEMENT_VERSION:-unset}', expected '$_EXPECTED_ENFORCEMENT_VERSION'. This image either predates agent-egress enforcement or its build did not complete the steps it depends on. Refusing to start rather than run the agent unrestricted while believing otherwise; rebuild with 'docker compose build' and check it actually succeeds before 'up'." >&2
        exit 1
    fi

    # Resolved once, here, and pinned into /etc/hosts below: the one host the
    # agent may reach is known ahead of time, so it never needs to make a DNS
    # query of its own at all (see the reject rule's own comment for why that
    # matters, not just why it's convenient). Done through python3, not
    # getent/awk: this image guarantees python3, not those.
    anthropic_ip=$(python3 -c "import socket; print(socket.gethostbyname('api.anthropic.com'))" 2>/dev/null || true)
    if [ -z "$anthropic_ip" ]; then
        echo "entrypoint: could not resolve api.anthropic.com; refusing to start rather than run the agent unprotected" >&2
        exit 1
    fi
    if ! grep -q "api.anthropic.com" /etc/hosts; then
        echo "$anthropic_ip api.anthropic.com" >> /etc/hosts
    fi

    # Order matters: iptables evaluates OUTPUT top to bottom, first match
    # wins. The ACCEPT is appended before the catch-all REJECT, so it is the
    # one that fires for the one destination it names.
    #
    # Deliberately no loopback exception. Docker's own embedded DNS resolver
    # (typically 127.0.0.11) can be reached over what the kernel sees as the
    # loopback interface, so a blanket `-o lo -j ACCEPT` would quietly let a
    # DNS query through and undo the point of blocking DNS below -- exactly
    # the kind of control that looks like enforcement without being any.
    # Nothing the agent legitimately needs (the CLI's own IPC with the SDK is
    # over stdio pipes, not a socket) requires loopback access; if a task
    # ever genuinely needs to reach its own locally-bound process, that is a
    # deliberate, separate decision to make later, not a side effect of this
    # rule being convenient to write.
    iptables -A OUTPUT -m owner --uid-owner 1001 -d "$anthropic_ip" -p tcp --dport 443 -j ACCEPT
    # REJECT, not DROP: an immediate "connection refused" rather than a
    # timeout. A refused connection reads as deliberate; a hang reads as a
    # transient network blip worth retrying, which is exactly the wrong
    # signal to give an agent that is billed by the token for however long
    # it waits before giving up. This one rule also covers DNS (UDP/53):
    # the agent has no need for it at all, since the one host it may reach
    # is already pinned in /etc/hosts above, and leaving DNS open would be
    # its own exfiltration channel.
    iptables -A OUTPUT -m owner --uid-owner 1001 -j REJECT --reject-with icmp-port-unreachable

    echo "entrypoint: agent egress restricted to api.anthropic.com ($anthropic_ip:443) only" >&2
fi

# --no-new-privs on the way down: whatever runs from here on, including
# anything the agent's own Bash tool spawns, can never regain a privilege
# this process does not already have.
exec setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs -- "$@"
