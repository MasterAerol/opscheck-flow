"""Allow `python -m opscheck` without installing the package."""

from .cli import main

raise SystemExit(main())
