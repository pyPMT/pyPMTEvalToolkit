"""Program entry point.

The CLI itself lives in :mod:`pypmt_eval_toolkit.cli`; this module stays so that
``python pypmt_eval_toolkit/main.py <command>`` and the ``pypmtevalcli``
console script both keep working.
"""

import os
import sys

if __package__:
    from .cli import main
else:                                    # run as a script: no package context
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from pypmt_eval_toolkit.cli import main

__all__ = ['main']

if __name__ == '__main__':
    sys.exit(main())
