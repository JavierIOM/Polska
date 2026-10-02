#!/bin/sh
# Confirms the fix landed, from outside the Python source: watches for the
# real `claude` CLI process during a live tick and prints its actual
# command-line flags, straight from /proc, rather than trusting anything
# this project's own code claims about what it passed. Run this while a
# tick is in progress (trigger one in another shell with
# `docker compose exec --user polska scheduler python -m polska.cli tick <slug>`;
# plain `exec` runs as root, which is not how the scheduler runs), as root so it
# can read another uid's /proc/<pid>/cmdline:
#
#   docker compose exec --user root scheduler sh scripts/verify_configured_tools.sh
#
# Compare the printed --tools value against config/default.yaml's
# agents.<name>.tools list by eye. They should match exactly, for whichever
# agent happened to be running when this caught it.
set -eu

if [ "$(id -u)" != "0" ]; then
    echo "FAIL: running as uid $(id -u), not root. Re-run with:" >&2
    echo "  docker compose exec --user root scheduler sh scripts/verify_configured_tools.sh" >&2
    exit 1
fi

echo "Watching for a claude CLI process (uid 1001) for up to 60s..."
i=0
while [ "$i" -lt 60 ]; do
    for pid in /proc/[0-9]*; do
        pid_num=${pid#/proc/}
        owner=$(awk '/^Uid:/{print $2}' "$pid/status" 2>/dev/null || true)
        if [ "$owner" = "1001" ] && grep -aq "claude" "$pid/cmdline" 2>/dev/null; then
            echo
            echo "Found pid $pid_num, uid 1001:"
            tr '\0' ' ' < "$pid/cmdline"
            echo
            echo
            echo "Look for --tools and --allowedTools above. If --tools is missing"
            echo "entirely, the fix has regressed: that is the exact bug this closes."
            exit 0
        fi
    done
    i=$((i + 1))
    sleep 1
done

echo "FAIL: no claude CLI process (uid 1001) seen in 60s." >&2
echo "Trigger a tick in another shell first: docker compose exec --user polska scheduler python -m polska.cli tick <slug>" >&2
exit 1
