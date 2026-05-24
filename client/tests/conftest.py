import os
import sys

# Make `cart_recovery` (lives at repo root) importable alongside the installed
# `mirofish` SDK package.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

CLIENT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if CLIENT_ROOT not in sys.path:
    sys.path.insert(0, CLIENT_ROOT)
