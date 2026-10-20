"""Frozen-build entry point: the same thing the arize-harness console script runs."""

import sys

from core.cli import main

sys.exit(main())
