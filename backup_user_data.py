# backup_user_data.py
"""Utility to backup user database files without being removed on site updates.
Creates a 'backup' directory under the project root and copies the main SQLite
files (`app.db`, `bnpparibas.db`) with a timestamped filename.
Run this script manually or import it in `app.py` to perform a backup on startup.
"""
import os
import shutil
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
BACKUP_DIR = os.path.join(PROJECT_ROOT, "backup")

# Ensure backup directory exists
os.makedirs(BACKUP_DIR, exist_ok=True)

def backup_db(db_path: str):
    if not os.path.isfile(db_path):
        print(f"[Backup] No database file found at {db_path}")
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_name = os.path.basename(db_path)
    backup_name = f"{db_name}_{timestamp}.bak"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    shutil.copy2(db_path, backup_path)
    print(f"[Backup] {db_name} backed up to {backup_path}")

if __name__ == "__main__":
    # List of database files to backup
    db_files = [
        os.path.join(PROJECT_ROOT, "app.db"),
        os.path.join(PROJECT_ROOT, "instance", "bnpparibas.db"),
    ]
    for db in db_files:
        backup_db(db)
