"""Move the single shop JSON stores under data/shops/<shop id>/.

Run once, before the first start with a registry file: the router
reads each shop stores from its own directory, so the current files
have to move first. A backup of every moved file is kept under
data/backup-<stamp>/, and the record counts before and after must
match, or the move is undone.

Usage: python3 scripts/migrate_multishop.py <shop id> [--data-dir data]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whatsapp_studio.multishop import STORE_FILES, migrate_stores  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shop", help="Shop id for the current single shop")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    args = parser.parse_args()
    try:
        counts = migrate_stores(args.data_dir, args.shop)
    except (ValueError, OSError) as exc:
        print(f"Migration refused: {exc}")
        return 1
    print(f"Moved {len(counts)} store files to data/shops/{args.shop}/:")
    for name in STORE_FILES:
        if name in counts:
            print(f"  {name}: {counts[name]} records")
    print("Backup kept under data/backup-<stamp>/. Start the server to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
