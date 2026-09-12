"""Delete exactly one directional toll pair from the local debug cache.

This utility deliberately requires both official station IDs and --yes.
It never deletes the OSM index, station dictionary, or the reverse direction.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.tolls.schema import connect_database, initialize_schema
from config import TOLL_INDEX_DB


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear one directional official toll pair from SQLite."
    )
    parser.add_argument("--entry-id", required=True, help="official entry station ID")
    parser.add_argument("--exit-id", required=True, help="official exit station ID")
    parser.add_argument(
        "--database",
        type=Path,
        default=TOLL_INDEX_DB,
        help="SQLite database path (defaults to the app index)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm deletion of the exact directional pair",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("add --yes to delete the selected directional pair")

    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    connection = connect_database(str(database))
    try:
        initialize_schema(connection)
        rows = connection.execute(
            """
            SELECT cache_key, entry_name, exit_name
            FROM toll_rates_cache
            WHERE entry_official_id = ? AND exit_official_id = ?
            """,
            (args.entry_id, args.exit_id),
        ).fetchall()
        if not rows:
            print(
                f"[MISS] no cache row for official pair "
                f"{args.entry_id}->{args.exit_id}"
            )
            return 0
        connection.execute(
            """
            DELETE FROM toll_rates_cache
            WHERE entry_official_id = ? AND exit_official_id = ?
            """,
            (args.entry_id, args.exit_id),
        )
        connection.commit()
        print(
            f"[OK] removed {len(rows)} row(s) for official pair "
            f"{args.entry_id}->{args.exit_id}: "
            f"{rows[0][1]} -> {rows[0][2]}"
        )
        return 0
    except sqlite3.Error as error:
        connection.rollback()
        print(f"[FAIL] {error}")
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
