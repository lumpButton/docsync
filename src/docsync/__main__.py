"""``python -m docsync`` runs the command line."""

import sys

from docsync.cli import main

if __name__ == "__main__":
    sys.exit(main())
