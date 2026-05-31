# Veille IA

Agrégateur de flux RSS IA avec résumés Groq (Llama 3).

## Architecture

```
backend/   → FastAPI sur Railway
frontend/  → HTML/JS statique sur Amen
```

## Déploiement backend (Railway)

1. Crée un nouveau service dans ton projet Railway
2. Connecte le dossier `backend/` (ou push sur une branche dédiée)
3. Ajoute la variable d'environnement :
   ```
   GROQ_API_KEY=gsk_xxxxxxxxxxxx
   ```
4. Note l'URL générée par Railway (ex: `https://veille-ia-backend.railway.app`)

### Obtenir une clé Groq gratuite
→ https://console.groq.com → Sign up → API Keys → Create

## Déploiement frontend (Amen)

1. Dans `frontend/index.html`, ligne 170, remplace :
   ```js
   const API = 'https://TON-SERVICE.railway.app';
   ```
   par ton URL Railway réelle.

2. Upload `index.html` sur ton hébergement Amen via FTP ou le gestionnaire de fichiers.

## URLs API

| Route | Description |
|-------|-------------|
| `GET /api/feed` | Articles (params: `tag`, `q`, `limit`) |
| `GET /api/sources` | Liste des sources configurées |
| `GET /api/refresh` | Force le rechargement des flux |
| `GET /health` | Statut du service |

## Ajouter/retirer des sources

Édite la liste `SOURCES` dans `backend/main.py`.

## Variables d'environnement

| Variable | Description | Obligatoire |
|----------|-------------|-------------|
| `GROQ_API_KEY` | Clé API Groq pour les résumés | Non (résumés désactivés si absent) |
| `PORT` | Port Railway (auto-injecté) | Oui (auto) |
