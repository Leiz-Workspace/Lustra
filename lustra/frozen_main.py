# coding=UTF-8
"""
Entry point of the frozen core shipped with the desktop app (PyInstaller one-dir build, see tools/build_core.py):
  lustra-core server        JSON-RPC sidecar over stdio (lustra.server)
  lustra-core cli ARGS...   command line downloader (lustra CLI; exit codes of lustra_cmd.run_cmd)
The legacy Tk GUI is not bundled.
"""
#########################################
#
#

import multiprocessing
import sys

__all__ = ('main',)

USAGE = 'usage: lustra-core server | lustra-core cli ARGS...'


def _setup_streams() -> None:
    # PyInstaller ignores PYTHONUNBUFFERED / PYTHONIOENCODING: the app reads progress and RPC lines as they are printed
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    _setup_streams()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    if command == 'server':
        from lustra.server import main as server_main
        return server_main()
    if command == 'cli':
        if not rest:
            print('cli: arguments required (the legacy GUI is not bundled)', file=sys.stderr)
            return 2
        from lustra.lustra import lustra_main
        return lustra_main(rest)
    print(f'unknown command \'{command}\'\n{USAGE}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())

#
#
#########################################
