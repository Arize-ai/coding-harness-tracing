"""``python -m core.setup preflight <harness>``: install.sh/install.bat's check before they migrate config.yaml."""

import sys

from core import install_table
from core.setup import err, preflight_backend_replacement


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "preflight":
        err("usage: python -m core.setup preflight <harness>")
        return 2
    key = install_table.resolve(argv[1])
    if key is None:
        err(f"unknown harness '{argv[1]}'")
        return 2
    preflight_backend_replacement(key, quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
