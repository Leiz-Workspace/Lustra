import sys


def main(argv=None) -> None:
    """Console script entry: CLI with arguments, legacy GUI without. GUI modules (tkinter) are imported lazily"""
    from .lustra import lustra_main
    sys.exit(lustra_main(sys.argv[1:] if argv is None else argv))


__all__ = ('main',)
