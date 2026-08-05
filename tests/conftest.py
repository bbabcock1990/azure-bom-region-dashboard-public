import sys, os
# Make `_shared` importable from tests
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "api"))
