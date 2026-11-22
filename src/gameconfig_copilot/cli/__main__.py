"""``python -m gameconfig_copilot.cli`` entry point.

The package is not directly executable without this module (Python looks for
``__main__.py``), and the ``gcc`` console script only exists after an editable
install. Keeping ``__main__.py`` means the CLI is runnable straight from a
checkout: ``PYTHONPATH=src python -m gameconfig_copilot.cli schema dump``.
"""

from __future__ import annotations

from . import main

if __name__ == "__main__":
    main()
