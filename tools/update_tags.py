#!/usr/bin/env python3
# coding=UTF-8
"""
Lustra tag lists updater (cron / systemd timer / manual)
Usage: python tools/update_tags.py [--modules rx,db] [--out 2tags] [--min-posts 1] [--delay 0.5] [--api-key KEY,USER] [--force]
Exit code: 0 = all requested lists updated (or skipped by sanity check), 1 = at least one module failed
"""
import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

from lustra.tags_updater import SUPPORTED_MODULES, update_tag_list  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description='Update Lustra tag lists used for autocompletion and the A-Z tag browser')
    ap.add_argument('--modules', default='rx,db', help=f'comma separated, supported: {",".join(SUPPORTED_MODULES)}')
    ap.add_argument('--out', default=(ROOT / '2tags').as_posix(), help='output folder (default: repo 2tags/)')
    ap.add_argument('--min-posts', type=int, default=1, help='skip tags with fewer posts (default 1)')
    ap.add_argument('--delay', type=float, default=0.5, help='delay between requests, seconds (be polite, default 0.5)')
    ap.add_argument('--max-pages', type=int, default=0, help='debug: stop after N pages (partial lists are not written without --force)')
    ap.add_argument('--api-key', default='', help='KEY,USER_ID (rx) or API_KEY,LOGIN (db)')
    ap.add_argument('--force', action='store_true', help='write even if the sanity check fails')
    args = ap.parse_args()

    failed = 0
    for module in [m.strip() for m in args.modules.split(',') if m.strip()]:
        started = time.monotonic()
        print(f'[{time.strftime("%F %T")}] {module}: updating ({SUPPORTED_MODULES.get(module, "?")})...', flush=True)
        try:
            res = update_tag_list(module, pathlib.Path(args.out), min_posts=args.min_posts, delay=args.delay,
                                  max_pages=args.max_pages, api_key=args.api_key, force=args.force,
                                  progress=lambda msg, _n: print(f'  {msg}', flush=True))
            print(f'[{time.strftime("%F %T")}] {module}: {res.message} in {time.monotonic() - started:.0f}s -> {res.path}', flush=True)
        except Exception as err:
            failed += 1
            print(f'[{time.strftime("%F %T")}] {module}: FAILED: {err}', file=sys.stderr, flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
