import argparse
import os
from pathlib import Path
from typing import Literal

import pandas as pd
from mediascan.utils.db.mediascan_db_loader import load_db


def list_table(table: Literal["artist", "album", "mediafile", "track", "trackstreaming"]) -> None:
    parser = argparse.ArgumentParser(
        description=f"Lists all contents of the {table} table in the active collection's mediascan.db."
    )
    parser.parse_args()

    collection_root = os.environ.get("MOONGAS_COLLECTION_ROOTDIR")
    if not collection_root:
        parser.error("MOONGAS_COLLECTION_ROOTDIR must be set to the active collection directory")
    db_path = (Path(collection_root) / "mediascan.db").resolve()
    if not db_path.is_file():
        parser.error(f"Moongas database file not found: {db_path}")

    # mediascan's loader validates relative SQLite URLs, but misreads absolute ones.
    db_url = f"sqlite:///{Path(os.path.relpath(db_path)).as_posix()}"
    engine = load_db(db_url)
    try:
        with engine.connect() as connection:
            contents = pd.read_sql_table(table, connection)
        if contents.empty:
            print(contents.to_string(index=False))
        else:
            rows = [[str(column) for column in contents.columns]]
            rows.extend([str(value) for value in row] for row in contents.itertuples(index=False, name=None))
            widths = [max(len(value) for value in column) for column in zip(*rows)]
            for row in rows:
                print(" ".join(value.ljust(width) for value, width in zip(row, widths)))
    finally:
        engine.dispose()
