"""Shared install, config and span-export code for every harness.

Hooks import this package, so it stays on the standard library.
"""

FALLBACK_VERSION = "0.0.0"


def version() -> str:
    """Return the package version. Never raises.

    The only version reader: everything that reports a version calls this.
    Tries, in order:

    1. ``core/_version.py``, which setuptools-scm writes at build time. A Nuitka
       binary has no installed metadata, so this is its only source.
    2. The installed distribution's metadata.
    3. ``FALLBACK_VERSION``, e.g. on the Claude plugin path, where nothing is
       installed and no build has run.
    """
    try:
        from core._version import version as built
    except ImportError:
        pass
    else:
        return built
    # Imported here, not at module level, to keep hook start-up cheap.
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    try:
        return dist_version("coding-harness-tracing")
    except PackageNotFoundError:
        return FALLBACK_VERSION
