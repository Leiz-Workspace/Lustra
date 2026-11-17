import os

# test runs never record their fake downloads in the user's download index (lustra/dlindex.py): test_dlindex turns it on
# itself, with a temporary data dir
os.environ.setdefault('LUSTRA_DLINDEX', '0')
