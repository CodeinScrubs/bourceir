# -*- coding: utf-8 -*-
"""
(سازگاری با نسخه‌های قبلی) — نقطه ورود جدید: python brs.py live
"""

import sys

from brs import main as brs_main

if __name__ == "__main__":
    sys.exit(brs_main(["live"] + sys.argv[1:]))
