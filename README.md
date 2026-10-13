# BNP Paribas — Application web

Projet de banque en ligne simulée pour BNP Paribas, avec interface client et administration.

## Description

- Application Flask avec gestion de clients, comptes, transactions et notifications.
- Authentification via `Flask-Login`.
- Base de données PostgreSQL sur Render, fallback SQLite locale en `instance/bnpparibas.db`.
- Interface responsive pour desktop, tablette et mobile.
- Sauvegarde automatique du `README.md` dans `instance/README_deploy.md` au démarrage sur Render.
- Stockage utilisateur de secours dans `instance/users.json`.

## Dépendances

Installez les dépendances :

```bash
pip install -r requirements.txt
```

## Exécution locale

```bash
cd "BNP PARIBAS"
python app.py
```

Ensuite, ouvrez :

```text
http://127.0.0.1:5001
```

## Déploiement sur Render

1. Liez Render au dépôt GitHub : `https://github.com/kouakoucedric305-crypto/connexion-mabanque.bnpparibas`
2. Utilisez `python` comme environnement.
3. Build command :

```bash
pip install -r requirements.txt
```

4. Start command :

```bash
python post_deploy_copy_readme.py && gunicorn app:app
```

5. Ajoutez une variable d'environnement `DATABASE_URL` si nécessaire.

## Notes

- Le dossier `instance/` contient des fichiers de persistance locaux et de déploiement.
- Les données utilisateur sont sauvegardées en JSON dans `instance/users.json`.
- Les backups de base de données sont créés dans `backup/`.
