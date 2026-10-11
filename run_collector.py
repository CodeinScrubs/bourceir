# -*- coding: utf-8 -*-
"""
(سازگاری با نسخه‌های قبلی) — نقطه ورود جدید: brs.py

    python run_collector.py --mode test      →  python brs.py doctor
    python run_collector.py --mode history   →  python brs.py update
    python run_collector.py --mode snapshot  →  python brs.py live --force --cycles 1
"""

import argparse
import sys

from brs import main as brs_main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BRS (legacy) — از brs.py استفاده کنید")
    parser.add_argument("--mode", choices=["test", "history", "snapshot"], default="test")
    parser.add_argument("--symbols", default="")
    args = parser.parse_args()
    mapping = {
        "test": ["doctor"],
        "history": ["update"] + (["--symbols", args.symbols] if args.symbols else []),
        "snapshot": ["live", "--force", "--cycles", "1"] + (["--symbols", args.symbols] if args.symbols else []),
    }
    sys.exit(brs_main(mapping[args.mode]))
