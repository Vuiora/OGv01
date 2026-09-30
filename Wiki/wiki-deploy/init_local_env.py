"""Generate local Wiki configuration without printing or overwriting secrets."""
from pathlib import Path
import secrets

def main():
    path = Path(__file__).resolve().parent / ".env"
    lines = [f"{key}={secrets.token_hex(32)}" for key in
             ["OGWIKI_DB_ROOT_PASSWORD", "OGWIKI_DB_PASSWORD", "OGWIKI_SECRET_KEY", "OGWIKI_UPGRADE_KEY"]]
    with path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("Created local .env; configure existing database credentials before reuse.")

if __name__ == "__main__":
    main()
