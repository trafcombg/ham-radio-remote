"""Shared debug-logging control for the admin panel and the client's
Debug dialog: pick a root level, plus which specific loggers (modules)
should be forced to DEBUG regardless of it — so you can, say, keep
everything at WARNING except "audio_io" while chasing one specific bug.

QUIET_BY_DEFAULT loggers (third-party internals that are pure noise at
DEBUG) stay quiet unless explicitly selected in module_levels."""

import logging

QUIET_BY_DEFAULT = {"comtypes": logging.WARNING, "uvicorn.access": logging.WARNING}


def list_loggers() -> list:
    """Every logger created so far (via logging.getLogger(name) anywhere
    in the process) — grows as modules get imported/used, so call this
    fresh each time rather than caching it."""
    return sorted(name for name in logging.root.manager.loggerDict if name)


def current_state() -> dict:
    return {
        "root_level": logging.getLevelName(logging.getLogger().level),
        "loggers": {
            name: logging.getLevelName(logging.getLogger(name).level)
            for name in list_loggers()
            if logging.getLogger(name).level != logging.NOTSET
        },
    }


def apply(root_level: str, module_levels: dict):
    """module_levels: {logger_name: level_name}. Replaces whatever was
    set before — every logger not listed here goes back to NOTSET
    (inherit root), so this never leaves stale overrides behind."""
    logging.getLogger().setLevel(getattr(logging, root_level.upper(), logging.INFO))
    for name in list_loggers():
        logging.getLogger(name).setLevel(logging.NOTSET)
    for name, level_name in QUIET_BY_DEFAULT.items():
        logging.getLogger(name).setLevel(level_name)
    for name, level_name in module_levels.items():
        logging.getLogger(name).setLevel(getattr(logging, level_name.upper(), logging.DEBUG))


if __name__ == "__main__":
    logging.getLogger("test_a").setLevel(logging.INFO)  # stale state from before apply()

    apply("WARNING", {"test_a": "DEBUG"})
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("test_a").level == logging.DEBUG
    assert logging.getLogger("comtypes").level == logging.WARNING, "quiet-by-default not applied"

    state = current_state()
    assert state["root_level"] == "WARNING"
    assert state["loggers"]["test_a"] == "DEBUG"
    assert "comtypes" in state["loggers"]

    apply("INFO", {})  # module_levels empty — test_a's earlier override must be gone
    assert logging.getLogger("test_a").level == logging.NOTSET
    assert logging.getLogger("comtypes").level == logging.WARNING, "quiet-by-default must survive a reset"

    print("debug_logging.py: ok")
