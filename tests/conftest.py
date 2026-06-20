"""Shared pytest configuration.

Adds the repository root to sys.path so the tests can `import brand_finder`
without installing the project as a package.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
