#!/usr/bin/env python3
"""Convenience launcher for the library demo.

    python run.py          # runs example.py against the US marketplace
    python run.py uk       # ...or any marketplace key

The project is a library (see `scraper/` and `example.py`); this just runs the
example. For the optional command-line interface, use `python -m scraper.cli`.
"""
import sys

from example import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "us"))
