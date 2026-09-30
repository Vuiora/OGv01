"""Reserve observation units across runs so reruns cannot reseal the same evidence."""
import sqlite3
from pathlib import Path

class EvidenceAlreadyRegistered(ValueError):
    pass

def reserve_units(root: Path, dataset_id, group_field, records, run_id, source_hash):
    with sqlite3.connect(root / "observation-registry.sqlite3", timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS units (dataset TEXT, field TEXT, unit TEXT, run_id TEXT, PRIMARY KEY(dataset,field,unit))")
        db.execute("CREATE TABLE IF NOT EXISTS sources (sha256 TEXT PRIMARY KEY, run_id TEXT)")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT INTO sources VALUES (?,?)", (source_hash, run_id))
            for group in sorted({str(r["group_ids"][group_field]) for r in records}):
                db.execute("INSERT INTO units VALUES (?,?,?,?)", (dataset_id, group_field, group, run_id))
        except sqlite3.IntegrityError:
            db.rollback()
            raise EvidenceAlreadyRegistered("Observation source or units are already registered; inspect/replay the previous run") from None

