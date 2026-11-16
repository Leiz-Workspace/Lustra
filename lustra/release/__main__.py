import sys

if not __package__ and not getattr(sys, 'frozen', False):
    import os.path
    path = os.path.abspath(__file__)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(path))))

import lustra.release

if __name__ == '__main__':
    lustra.release.main(sys.argv[1:])
