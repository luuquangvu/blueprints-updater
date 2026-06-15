"""Conftest for benchmarks — skip collection when pytest-benchmark is unavailable.

This prevents collection failures in CI environments (e.g. the compatibility
matrix job) that don't install pytest-benchmark.
"""

from importlib.util import find_spec

collect_ignore: list[str] = []
if find_spec("pytest_benchmark") is None:
    collect_ignore = ["test_performance.py"]
