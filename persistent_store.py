"""persistent_store.py
Module minimal pour sauvegarder/charger des données utilisateurs dans
`instance/users.json`. Utile comme mécanisme de secours si la base de données
est re-créée lors d'un déploiement. Recommande d'utiliser une base de données
ou un stockage externe (S3, disque persistant Render) pour la production.
"""
from pathlib import Path
import json
from typing import List, Dict, Optional

BASE = Path(__file__).parent
INSTANCE_DIR = BASE / "instance"
USERS_FILE = INSTANCE_DIR / "users.json"

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
if not USERS_FILE.exists():
    USERS_FILE.write_text("[]", encoding="utf-8")

def load_users() -> List[Dict]:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_users(users: List[Dict]) -> None:
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")

def add_user(user: Dict) -> None:
    users = load_users()
    users.append(user)
    save_users(users)

def find_user_by_num(num_client: str) -> Optional[Dict]:
    users = load_users()
    for u in users:
        if u.get("num_client") == num_client:
            return u
    return None

if __name__ == "__main__":
    print(f"Users file: {USERS_FILE} — {len(load_users())} users loaded")
