"""Script simple pour copier `README.md` vers `instance/README_deploy.md`.
Peut être invoqué au démarrage de l'application ou via un hook de déploiement
sur Render. ATTENTION: les conteneurs Render peuvent être éphémères —
préférer un disque persistant ou un stockage externe pour une vraie persistance.
"""
from pathlib import Path
import shutil

BASE = Path(__file__).parent
SRC = BASE / "README.md"
DST = BASE / "instance" / "README_deploy.md"

def copy_readme():
    if not SRC.exists():
        print("README.md non trouvé dans le répertoire du projet.")
        return False
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC, DST)
    print(f"README copié vers {DST}")
    return True

if __name__ == '__main__':
    copy_readme()
