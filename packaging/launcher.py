"""Entry point for the PyInstaller build (double-click ResinPilot.exe)."""

import sys

from resinpilot.cli import main

if __name__ == "__main__":
    sys.exit(main())
