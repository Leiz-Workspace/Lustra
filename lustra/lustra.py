# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import sys

__all__ = ('lustra_main',)


def lustra_main(argv: list[str]) -> int:
    """Returns the process exit code (see lustra_cmd.run_cmd)"""
    if argv:
        from .lustra_cmd import run_cmd
        return run_cmd(argv)
    from .lustra_gui import run_gui  # tkinter is only needed here
    run_gui()
    return 0


if __name__ == '__main__':
    sys.exit(lustra_main(sys.argv[1:]))

#
#
#########################################
