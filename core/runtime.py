"""How this copy of coding-harness-tracing is running.

Standard library only, and outside ``core.setup``: hook launchers import it
too, and they run with nothing pip-installed.
"""


def is_binary() -> bool:
    """True when running as a Nuitka-compiled binary.

    Nuitka defines ``__compiled__`` in the globals of every module it compiles;
    it is never defined under a plain interpreter. This is the only such check:
    everything that behaves differently on a binary install asks here.
    """
    return "__compiled__" in globals()


def installed_version() -> str:
    """The installed coding-harness-tracing version, or ``0.0.0`` when no distribution metadata is found."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("coding-harness-tracing")
    except PackageNotFoundError:
        return "0.0.0"
