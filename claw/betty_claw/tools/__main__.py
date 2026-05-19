"""Entry point for `python -m betty_claw.tools`.

Runs the registry self-test. Lives in __main__.py because Python
requires this for packages to be executable via -m, unlike modules
which can use `if __name__ == "__main__"` directly.
"""

from betty_claw.tools import _self_test


if __name__ == "__main__":
    _self_test()
