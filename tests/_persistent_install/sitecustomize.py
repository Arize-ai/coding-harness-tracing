"""Make a test subprocess's ``arize-harness`` count as a persistent install.

Tests run setup from this checkout, which ``require_setup_writes()`` refuses
(a hook registered from a dev checkout would point at something that goes
away). Put this directory first on PYTHONPATH (``tests._install_type.PERSISTENT_PYTHONPATH``)
and setup writes as it would from ``uv tool install``. Tests of the refusal
itself run without it.

Patches ``core.setup.runner`` only when something imports it, so a process
that never runs setup (the hook path) imports nothing extra.
"""

import importlib.abc
import importlib.machinery
import sys


class _AllowSetup(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != "core.setup.runner":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def _exec(module):
            exec_module(module)
            module.setup_write_refusal = lambda: None

        spec.loader.exec_module = _exec
        return spec


sys.meta_path.insert(0, _AllowSetup())
