# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import json
import os
import sys
import time
from multiprocessing.dummy import current_process
from threading import Thread

from .cmdargs import prepare_arglist
from .downloaders import get_new_downloader
from .logger import Logger
from .module import ProcModule
from .utils import ensure_compatibility

__all__ = ('run_cmd',)

ENV_IPC = 'LUSTRA_IPC'
ENV_SECRET_ARGS = 'LUSTRA_SECRET_ARGS'
'''JSON list of extra arguments (ex. ["-api_key", "KEY,USER"]), removed from the environment once read'''
'''desktop app mode: machine-readable progress lines on stdout, 'cancel' command on stdin'''
IPC_PROGRESS_PREFIX = '@@LUSTRA_PROGRESS '


def _pop_secret_args() -> list[str]:
    raw = os.environ.pop(ENV_SECRET_ARGS, '')
    if not raw:
        return []
    try:
        extra = json.loads(raw)
        assert isinstance(extra, list) and all(isinstance(x, str) for x in extra)
        return extra
    except Exception:
        print(f'ERROR: {ENV_SECRET_ARGS} must be a JSON list of strings', file=sys.stderr)
        return []


def _start_ipc_threads(dwn) -> None:
    root = current_process()

    def report() -> None:
        last = ''
        while True:
            state = {
                'state': int(dwn.current_state), 'stateName': dwn.current_state.name,
                'task': dwn.current_task_num, 'tasks': len(dwn.tags_str_arr),
                'total': dwn.total_count, 'totalAll': dwn.total_count_all, 'processed': dwn.processed_count,
                'success': dwn.success_count, 'failed': dwn.fail_count, 'pages': dwn.total_pages if dwn.maxpage else 0,
                'cfChallenge': dwn.cf_challenge_hit, 'genderFiltered': dwn.gender_filtered_count, 'blocked': dwn.safety_blocked_count,
            }
            line = json.dumps(state, separators=(',', ':'))
            if line != last:
                with Logger.print_lock:
                    print(f'{IPC_PROGRESS_PREFIX}{line}', flush=True)
                last = line
            time.sleep(0.4)

    def watch_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == 'cancel':
                root.killed = True
                break

    Thread(target=report, daemon=True, name='ipc-progress').start()
    Thread(target=watch_stdin, daemon=True, name='ipc-stdin').start()


def run_cmd(args: list[str]) -> None:
    Logger.init(True)
    ensure_compatibility()
    current_process().killed = False
    # API keys, cookies, headers, proxy credentials: passed by the desktop app in the environment, not in argv
    secret_args = _pop_secret_args()
    arglist = prepare_arglist([*secret_args, *args])  # also loads extensions
    ProcModule.set_cur_module_by_name(arglist.module)
    with get_new_downloader() as cdwn:
        cdwn.save_cmdline(args)
        if os.environ.get(ENV_IPC) == '1':
            _start_ipc_threads(cdwn)
        if arglist.get_maxid:
            cdwn.launch_get_max_id(arglist)
        else:
            cdwn.launch_download(arglist)
        if os.environ.get(ENV_IPC) == '1':
            final = {'state': 0, 'stateName': 'DONE', 'success': cdwn.success_count, 'failed': cdwn.fail_count,
                     'totalAll': cdwn.total_count_all, 'processed': cdwn.processed_count, 'failedItems': cdwn.failed_items[:200],
                     'cfChallenge': cdwn.cf_challenge_hit, 'genderStats': cdwn.gender_stats, 'genderFiltered': cdwn.gender_filtered_count,
                     'blocked': cdwn.safety_blocked_count}
            with Logger.print_lock:
                print(f'{IPC_PROGRESS_PREFIX}{json.dumps(final, separators=(",", ":"))}', flush=True)


if __name__ == '__main__':
    if len(sys.argv) >= 2:
        run_cmd(sys.argv[1:])
        sys.exit(0)
    else:
        print('ERROR: Lustra cmd arguments required. To run GUI use lustra_gui.py')
        sys.exit(-1)

#
#
#########################################
