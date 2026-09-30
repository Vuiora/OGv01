"""Create independent service secrets without printing them."""
import argparse
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    target = args.root / ".env"
    if target.exists():
        print(".env already exists; no changes made.")
        return
    template = (args.root / ".env.example").read_text(encoding="utf-8")
    lines = []
    for line in template.splitlines():
        key = line.split("=", 1)[0]
        if key in {"APP_API_KEY", "DOCLING_API_KEY", "N8N_ENCRYPTION_KEY"}:
            line = key + "=" + secrets.token_urlsafe(36)
        lines.append(line)
    with target.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("Created .env with independent service secrets. Configure GPT in the service UI.")


if __name__ == "__main__":
    main()
