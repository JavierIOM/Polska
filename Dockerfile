# One image, two services (see docker-compose.yml): the scheduler and the
# dashboard share the exact same code and dependencies, so there is nothing to
# gain from building them separately, only two things to keep in sync.
#
# Debian-based (not Alpine): claude-agent-sdk ships a platform-specific wheel
# with a bundled Claude Code CLI binary per platform (confirmed against PyPI:
# manylinux_2_17_x86_64 among them), and manylinux wheels assume glibc, which
# Alpine's musl libc is not. A fresh `pip install` inside this image is what
# makes pip resolve that wheel; do not copy a host-built .venv in instead, a
# Windows dev machine's venv bundles claude.exe, not the Linux binary this
# needs.

# Node 22, copied from the official image rather than added via apt or
# NodeSource: avoids managing a second package repository's signing key for
# one runtime, and pins an exact, known-good version rather than whatever
# Debian's own slow-moving apt repos happen to carry. 22 is within the range
# CarScratch's own installed `astro` package declares in its `engines` field
# ("18.20.8 || ^20.3.0 || >=22.0.0", checked directly rather than assumed),
# and matches this project's standing convention for Astro projects.
FROM node:22-slim AS node_source

FROM python:3.12-slim

# ca-certificates: the SDK's CLI makes real HTTPS calls to the Anthropic API.
# git: workspace.py shells out to a real `git clone` for the engineer/analyst
# task workspace; python:3.12-slim does not include it.
# iptables, util-linux (setpriv): what makes "the agent has no network
# access" a kernel-enforced fact instead of a sentence in a system prompt --
# see docker/entrypoint.sh for the rule itself and why it exists at all.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git iptables util-linux \
    && rm -rf /var/lib/apt/lists/*

# Copied wholesale rather than symlinked piecemeal: npm and npx are
# themselves JS files that resolve `node` and npm's own library code
# relative to this same tree.
COPY --from=node_source /usr/local/bin/node /usr/local/bin/node
COPY --from=node_source /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Two non-root users, fixed at these UIDs rather than left to useradd's own
# choice, so the host-side bind mount's ownership (data/) can be matched
# deliberately -- see README's deployment section.
#
# `polska` (1000) runs the long-lived scheduler and dashboard processes: the
# things that need real, unrestricted network access (git clone, npm ci, the
# Anthropic API itself) and write access to data/.
#
# `agent` (1001) is who every single Claude Agent SDK subprocess is dropped
# to before it ever executes a line of agent-controlled code -- see
# runner.py's cli_path and the wrapper generated below. This is the identity
# docker/entrypoint.sh's iptables rule targets; polska's own traffic is never
# touched by it. The Claude Code CLI itself is also *why* two users exist at
# all: it refuses to run with --dangerously-skip-permissions (what
# permission_mode="bypassPermissions" becomes at the CLI level) as root, by
# its own design, and running the agent under a second, even more
# restricted, non-root identity than the orchestrator itself is the correct
# extension of that, not a workaround for it.
RUN groupadd --gid 1000 polska \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash polska \
    && groupadd --gid 1001 agent \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash agent

# The one capability polska needs at runtime, and the reason this image is
# not simply `USER polska` for everything from here on: dropping a spawned
# child to a different, unprivileged UID (see runner.py's cli_path) needs
# CAP_SETUID and CAP_SETGID, which a non-root process only ever gets by
# executing a binary that itself carries the capability on its file --
# exactly what this grants setpriv, and only setpriv. The resulting agent
# process inherits none of it: capabilities attach to the file being
# executed, and the wrapper below execs into a different binary entirely
# (the real CLI, which carries no capabilities of its own), so the agent
# never runs with anything beyond what UID 1001 has on its own.
RUN setcap cap_setuid,cap_setgid+ep /usr/bin/setpriv

WORKDIR /app

# Dependencies first, so editing application code does not bust this layer.
# Still as root: installing into site-packages needs it, and no non-root
# user ever needs to write there at runtime.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[runtime]"

# Resolve the SDK's bundled CLI binary once, at build time, and bake a
# wrapper script around it that drops privileges to the `agent` UID before
# ever executing a line of agent-controlled code. runner.py points
# ClaudeAgentOptions.cli_path at this wrapper, never at the real binary
# directly, so every agent invocation goes through the privilege drop
# unconditionally, not by each call site remembering to ask for it.
RUN REAL_CLI=$(find / -path /proc -prune -o -type f -name claude -path '*/_bundled/*' -print 2>/dev/null | head -1) \
    && test -n "$REAL_CLI" \
    && chmod +x "$REAL_CLI" \
    && printf '#!/bin/sh\nset -eu\nexec setpriv --reuid=1001 --regid=1001 --clear-groups --no-new-privs -- %s "$@"\n' "$REAL_CLI" > /usr/local/bin/polska-agent-cli \
    && chmod 755 /usr/local/bin/polska-agent-cli

COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# companies/, config/ and data/ are bind-mounted by docker-compose, not baked
# in here: editing a company profile or a ceiling should never need a rebuild.
# Their in-image ownership below is irrelevant once a bind mount replaces
# them at `docker compose up` time -- the mount's write access is decided
# entirely by the *host-side* directory's ownership, not anything set here.
# This chown only matters for the parts of /app that stay baked into the
# image (src/, migrations/, alembic.ini) and for running this image without
# compose at all.
RUN mkdir -p data companies config && chown -R polska:polska /app

# No USER directive: the container starts as root so entrypoint.sh can set up
# the agent-egress iptables rule (needs CAP_NET_ADMIN, granted to the
# scheduler service only in docker-compose.yml), then permanently drops to
# `polska` before running anything else. Root never runs anything beyond
# that one setup step.
ENTRYPOINT ["/entrypoint.sh"]

# No CMD: docker-compose.yml sets the command per service (scheduler vs
# dashboard). Running this image with no command is deliberately not useful.
