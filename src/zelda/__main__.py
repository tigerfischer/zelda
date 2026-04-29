"""Make `python -m zelda ...` work."""

import sys

from zelda.cli import main


if __name__ == "__main__":
    sys.exit(main())
