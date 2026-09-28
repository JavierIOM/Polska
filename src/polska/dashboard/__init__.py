"""The dashboard: a FastAPI app for approving/rejecting actions, clearing budget
halts, writing off orphaned runs, and reading the goals/activity feed/runs a
company has accumulated. Plain server-rendered HTML via Jinja2, no build step.

Two entry points: ``polska.dashboard.app.create_app`` builds the app against an
already-constructed session factory (what tests use, and what
``polska.dashboard.server`` uses in production); ``polska.dashboard.server.run``
is the process entrypoint (``python -m polska.dashboard.server``).
"""
