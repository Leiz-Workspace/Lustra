import sys

from .lustra import lustra_main


def main(argv=None) -> None:
    sys.exit(lustra_main(argv))


__all__ = ('main',)
