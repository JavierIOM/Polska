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
FROM python:3.12-slim

# ca-certificates: the SDK's CLI makes real HTTPS calls to the Anthropic API.
# Debian slim images are not guaranteed to have an up-to-date CA bundle out of
# the box.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so editing application code does not bust this layer.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[runtime]"

# Defensive: pip should preserve the wheel's executable bit on the bundled CLI
# binary, but this costs nothing and turns a silent permission failure into
# nothing at all, rather than a confusing CLINotFoundError at first run.
RUN find / -path /proc -prune -o -type d -name _bundled -print 2>/dev/null \
    | xargs -I{} sh -c 'chmod +x {}/claude 2>/dev/null || true'

COPY migrations/ ./migrations/
COPY alembic.ini ./

# companies/, config/ and data/ are bind-mounted by docker-compose, not baked
# in here: editing a company profile or a ceiling should never need a rebuild.

# No CMD: docker-compose.yml sets the command per service (scheduler vs
# dashboard). Running this image with no command is deliberately not useful.
