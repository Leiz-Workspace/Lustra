#!/usr/bin/env python3
# coding=UTF-8
"""
Reads the report of the Electron app's self-test (LUSTRA_SELFTEST_DIR=<dir> <app>, see docs/operations.md) and fails when
a step did not pass. 'contract.notAllowed' is ok: false on purpose (the renderer must be refused those RPCs).

usage: python tools/check_selftest.py <dir>/report.json
"""
#########################################
#
#

import json
import pathlib
import sys

EXPECTED_FALSE = {'contract.notAllowed'}


def failed_steps(report: dict) -> list[str]:
    out: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            if node.get('ok') is False and path not in EXPECTED_FALSE:
                out.append(path or '(report)')
            for k, v in node.items():
                walk(v, f'{path}.{k}' if path else k)
    walk(report, '')
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = pathlib.Path(argv[0])
    if not path.is_file():
        print(f'no self-test report at {path}: the app did not finish its self-test', file=sys.stderr)
        return 1
    report = json.loads(path.read_text(encoding='utf-8'))
    failed = failed_steps(report)
    print(f'self-test steps: {", ".join(report)}')
    if failed:
        print(f'self-test failed: {", ".join(failed)}', file=sys.stderr)
        for name in failed:
            node = report
            for part in name.split('.'):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            print(f'  {name}: {json.dumps(node, ensure_ascii=False)[:600]}', file=sys.stderr)
        return 1
    print('self-test passed')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

#
#
#########################################
