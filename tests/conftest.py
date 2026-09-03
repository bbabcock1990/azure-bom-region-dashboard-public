import sys, os
# Make both `_shared` (as `from _shared import ...`) and the `api` package
# (as `from api._shared import ...`) importable from tests under a bare
# `pytest` invocation, which — unlike `python -m pytest` — does not add the
# repo root to sys.path.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "api"))
sys.path.insert(0, HERE)
