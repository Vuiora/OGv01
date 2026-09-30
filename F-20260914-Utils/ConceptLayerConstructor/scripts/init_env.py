"""Create a private .env once, with random service keys. Never prints keys."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
text = (root / ".env.example").read_text(encoding="utf-8")
text = text.replace("CLC_API_KEY=\n", "CLC_API_KEY=" + secrets.token_urlsafe(32) + "\n")
text = text.replace("N8N_ENCRYPTION_KEY=\n", "N8N_ENCRYPTION_KEY=" + secrets.token_hex(32) + "\n")
try:
    with target.open("x", encoding="utf-8") as stream:
        stream.write(text)
    print("Created .env. Configure CLC_LLM_BASE_URL, CLC_LLM_MODEL and CLC_LLM_API_KEY.")
except FileExistsError:
    print(".env already exists; preserved without changes.")

