#!/usr/bin/env python3

import os
import sys

_srcdir = '%s/' % os.path.dirname(os.path.realpath(__file__))
_filepath = os.path.dirname(sys.argv[0])
sys.path.insert(1, os.path.join(_filepath, _srcdir))
sys.path.insert(1, os.path.join(_filepath, '../lib/bs4_lib/'))
sys.path.insert(1, os.path.join(_filepath, '../lib/requests_lib/'))


if sys.version_info[0] == 3:
    import lulu
    if __name__ == '__main__':
        lulu.main()
else:  # Python 2
    from lulu.util import log
    log.e('[fatal] Python 3 is required!')
