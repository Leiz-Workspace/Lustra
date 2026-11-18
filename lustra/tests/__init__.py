import os

# test runs never record their fake downloads in the user's download index (lustra/dlindex.py): test_dlindex turns it on
# itself, with a temporary data dir
os.environ.setdefault('LUSTRA_DLINDEX', '0')
# Lustra ships no shared rx key and refuses to run rx without one: the offline tests (fake sites) use a dummy key
os.environ.setdefault('LUSTRA_API_KEY_RX', 'offline-test-key-0000,0')
