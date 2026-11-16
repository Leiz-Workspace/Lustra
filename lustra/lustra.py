# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import sys

from .lustra_cmd import run_cmd
from .lustra_gui import run_gui

__all__ = ('lustra_main',)


def lustra_main(argv: list[str]) -> None:
    if argv:
        run_cmd(argv)
    else:
        run_gui()


if __name__ == '__main__':
    lustra_main(sys.argv[1:])
    sys.exit(0)

#
#
#########################################
