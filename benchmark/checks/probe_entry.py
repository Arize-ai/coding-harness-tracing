"""Probe entry for checks 3 and 4 (NOT the product entry): core.cli has no `status` or setup
subcommand, so this adds them on top of it to see whether a frozen build *would* behave.

  <exe> status [--json]                    core.setup.status:main
  <exe> setup <harness> install [...]      tracing.<harness>.install:main
  <exe> probe-data                         where the __file__-based data lookups resolve
  <exe> hook ...                           core.cli:main, unchanged
"""
import importlib
import json
import sys

a = sys.argv[1:]
if a[:1] == ["status"]:
    from core.setup.status import main

    sys.exit(main(a[1:]))
if a[:1] == ["setup"] and len(a) >= 3:
    sys.argv = [f"{a[1]}/install.py", *a[2:]]
    mod = importlib.import_module(f"tracing.{a[1]}.install")
    if hasattr(mod, "main"):
        mod.main()
    elif a[2] == "install":  # devin/install.py has no main(): mirror its __main__ block
        mod.install(with_skills="--with-skills" in a[3:])
    else:
        mod.uninstall()
    sys.exit(0)
if a[:1] == ["probe-data"]:
    import tracing.devin.install as dv
    import tracing.omp.constants as oc
    import tracing.omp.install as oi
    import tracing.opencode.constants as pc
    import tracing.opencode.install as pi

    out = {"opencode.install._plugin_source": str(pi._plugin_source()), "omp.install._plugin_source": str(oi._plugin_source()),
           "opencode.constants.PLUGIN_SOURCE": str(pc.PLUGIN_SOURCE), "omp.constants.PLUGIN_SOURCE": str(oc.PLUGIN_SOURCE),
           "devin.install.__file__": dv.__file__}
    out = {k: [v, __import__("os").path.exists(v)] for k, v in out.items()}
    out["devin.install._skills_dir_exists()"] = dv._skills_dir_exists()
    print(json.dumps(out, indent=1))
    sys.exit(0)
from core.cli import main

sys.exit(main())
