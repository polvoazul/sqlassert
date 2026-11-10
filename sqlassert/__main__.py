"""Run the command line with `python -m sqlassert`."""

import sys

from sqlassert_cli import main as main


if __name__ == "__main__":
    sys.exit(main())
