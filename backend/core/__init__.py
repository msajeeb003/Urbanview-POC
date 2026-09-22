"""Core: configuration, infrastructure clients, errors and middleware.

Nothing in ``core`` may import from ``api`` or ``jobs``. Municipality-specific values
(terminology, data sources, bounds, CRS) come from ``core.municipality`` profiles loaded from
``municipalities/<id>.toml`` and must never be hard-coded here.
"""
