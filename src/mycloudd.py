#!/usr/bin/env python3
"""MyCloud daemon entry point.

Run as: /usr/bin/python3 <APP>/mycloudd.py [--config PATH] [--once]

This script adds its own directory to sys.path so that "import mycloud" works
when it is copied alongside the mycloud/ package into <APP>. The daemon module
is imported lazily inside main() so this file imports cleanly even before
daemon.py exists.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    # type: () -> int
    from mycloud import paths

    parser = argparse.ArgumentParser(prog="mycloudd", description="MyCloud backup daemon")
    parser.add_argument("--config", default=str(paths.CONFIG), help="Path to config.ini")
    parser.add_argument("--once", action="store_true",
                        help="Run a single sync cycle and exit")
    args = parser.parse_args()

    from mycloud.daemon import run_daemon
    return run_daemon(args.config, args.once)


if __name__ == "__main__":
    sys.exit(main())
