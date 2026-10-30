#!/usr/bin/env python3
"""Gate: every SF Symbol used in gui/src must exist on macOS <= TARGET (12.0).

Why this exists (2026-08-01, supervised review of gui-macos12): two symbols
introduced in 2023 (arrow.triangle.2.circlepath.icloud, gauge.with.dots.
needle.bottom.50percent) need macOS 14 -- NSImage(systemSymbolName:) returns
nil on 12/13 and the menu-bar item rendered as a BLANK for the whole duration
of every sync. Compiles clean, render-smokes clean on a new dev machine, fails
only on the target OS. This gate closes that class permanently.

Method: scan all double-quoted lowercase dotted/plain literals; any literal
that IS a known symbol name (per the OS's own CoreGlyphs availability
database, the same source Xcode uses) must have been introduced in a release
whose macOS version is <= TARGET. Non-symbol strings never match the database
and are ignored, so no annotation convention is needed in the Swift sources.
"""
import glob
import os
import plistlib
import re
import sys

TARGET = (12, 0)
PLIST = ("/System/Library/CoreServices/CoreGlyphs.bundle"
         "/Contents/Resources/name_availability.plist")


def parse_ver(s):
    parts = str(s).split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except ValueError:
        return (99, 0)


def main():
    gui_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(PLIST, "rb") as handle:
            db = plistlib.load(handle)
        symbols = db["symbols"]              # name -> release year key
        years = db["year_to_release"]        # year key -> {"macOS": "11.0", ...}
    except (OSError, KeyError) as exc:
        print("SYMBOL GATE: cannot read %s (%s) -- failing closed" % (PLIST, exc))
        return 1

    bad = []
    for path in sorted(glob.glob(os.path.join(gui_root, "src", "*.swift"))):
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r'"([a-z0-9]+(?:\.[a-z0-9]+)*)"', text):
            name = m.group(1)
            year = symbols.get(name)
            if year is None:
                continue  # not an SF Symbol name at all
            macos = parse_ver(years.get(str(year), {}).get("macOS", "99.0"))
            if macos > TARGET:
                line = text[: m.start()].count("\n") + 1
                bad.append("%s:%d  \"%s\" needs macOS %d.%d (target %d.%d)"
                           % (os.path.basename(path), line, name,
                              macos[0], macos[1], TARGET[0], TARGET[1]))

    if bad:
        print("SYMBOL GATE FAIL:")
        for b in bad:
            print("   " + b)
        return 1
    print("   symbol gate: all SF Symbol literals available on macOS <= %d.%d"
          % TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())
