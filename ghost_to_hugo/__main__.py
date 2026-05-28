"""Allow `python -m ghost_to_hugo ...`"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
