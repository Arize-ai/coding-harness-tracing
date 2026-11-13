"""Let a test subprocess run setup from this checkout, which setup otherwise refuses.

Patches ``core.setup.runner`` on import, so the hook path still imports nothing extra.
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
