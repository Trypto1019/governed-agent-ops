"""Deliberately misbehaving script jobs for the demo.

  silent  exits 0 and causes nothing   -> the dispatcher marks it `suspicious`
  broken  exits 1                      -> two in a row and the job is suspended
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    kind = argv[0] if argv else "silent"
    if kind == "silent":
        print("scan complete")          # the classic lie: "ran fine", wrote nothing
        return 0
    if kind == "broken":
        print("upstream mail API refused the connection", file=sys.stderr)
        return 1
    raise SystemExit(f"unknown demo job {kind}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
