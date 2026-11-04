"""Generate credentials once. Never overwrite existing credentials."""
import os
import secrets
from pathlib import Path

path = Path(".env")
with path.open("x", encoding="utf-8") as stream:
    for key in ("DB_ROOT_PASSWORD", "DB_PASSWORD", "EXPORTER_PASSWORD", "GRAFANA_PASSWORD"):
        stream.write(f"{key}={secrets.token_hex(24)}\n")
try:
    os.chmod(path, 0o600)
except PermissionError:
    print("Credentials created, but chmod failed. Copy the project into ~/uptime-platform in Ubuntu and run chmod 600 .env.")
print("Created .env; keep it private and back it up alongside volumes.")
