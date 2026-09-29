#!/bin/sh
# Answers a specific question by observation, not documentation: does the
# CLI's WebFetch tool make its own local network call (in which case the
# agent-UID iptables jail in docker/entrypoint.sh covers it), or is it served
# through Anthropic's own hosted infrastructure (in which case the jail
# cannot see it at all, and never has)?
#
# Run as root, so this script can read the real iptables packet counters --
# the CLI subprocess itself still drops to uid 1001 through the normal
# cli_path wrapper (polska-agent-cli), exactly as a real dispatched run does.
# Nothing about the boundary under test is bypassed by running the harness
# itself as root; only the counter-reading needs the privilege.
#
#   docker compose exec --user root scheduler sh scripts/verify_webfetch_channel.sh
#
# Reasoning: the REJECT rule matches ANY destination for uid 1001 with no
# -d filter (see entrypoint.sh). icanhazip.com does not resolve to the one
# pinned Anthropic IP the ACCEPT rule allows, so a real local connection
# attempt from uid 1001 to it -- DNS lookup included, since the REJECT rule
# has no -p filter either -- is guaranteed to hit REJECT and increment its
# packet counter, whether or not the attempt eventually "succeeds" from the
# calling process's point of view. If WebFetch is instead served server-side
# by Anthropic, uid 1001 never makes that extra local connection at all: the
# REJECT counter stays flat, and the content still comes back over the
# existing, already-permitted Anthropic API channel.
set -eu

if [ "$(id -u)" != "0" ]; then
    echo "FAIL: running as uid $(id -u), not root. Re-run with:" >&2
    echo "  docker compose exec --user root scheduler sh scripts/verify_webfetch_channel.sh" >&2
    exit 1
fi

reject_packets() {
    iptables -L OUTPUT -v -n -x | awk '/REJECT/ && /owner UID match 1001/ {print $1; exit}'
}

before=$(reject_packets)
if [ -z "$before" ]; then
    echo "FAIL: could not find the uid-1001 REJECT rule in iptables OUTPUT at all." >&2
    echo "Has docker/entrypoint.sh's egress setup actually run in this container?" >&2
    exit 1
fi
echo "REJECT packet counter before: $before"

echo
echo "--- triggering a real WebFetch call, as the real agent-cli wrapper would run it ---"
python3 - <<'PYEOF'
import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)


async def main() -> None:
    options = ClaudeAgentOptions(
        system_prompt=(
            "Use WebFetch on exactly the URL given, nothing else, then report "
            "back only the raw text it returned."
        ),
        tools=["WebFetch"],
        allowed_tools=["WebFetch"],
        permission_mode="bypassPermissions",
        cli_path="/usr/local/bin/polska-agent-cli",
        max_turns=3,
        setting_sources=[],
    )
    async for message in query(
        prompt="Fetch https://icanhazip.com/ and report exactly what it returned.",
        options=options,
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(f"TEXT: {block.text}")
                elif isinstance(block, ToolUseBlock):
                    print(f"TOOL CALL: {block.name} {block.input}")
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    print(f"TOOL RESULT (is_error={block.is_error}): {block.content}")


asyncio.run(main())
PYEOF

echo
after=$(reject_packets)
echo "REJECT packet counter after:  $after"

echo
if [ "$after" -gt "$before" ]; then
    echo "RESULT: the REJECT counter moved ($before -> $after)."
    echo "WebFetch made its own local connection attempt and the jail caught it."
    echo "Covered by the existing egress restriction."
else
    echo "RESULT: the REJECT counter did NOT move ($before -> $after)."
    echo "If the TOOL RESULT above still shows real content from icanhazip.com,"
    echo "WebFetch was served through Anthropic's own infrastructure and never"
    echo "touched this container's network stack. NOT covered by the iptables"
    echo "rule -- the egress restriction has never seen this traffic."
    echo "If the TOOL RESULT above shows an error instead, WebFetch may simply"
    echo "not have been invoked at all (check the TOOL CALL lines above); this"
    echo "script proves nothing in that case and should be re-run."
fi
