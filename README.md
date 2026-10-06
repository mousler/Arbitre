# rhetora — l’arbitre de vos débats

**Analyseur intelligent de débats avec IA** - Extrait et analyse les arguments, sophismes et faits factuels directement depuis les vidéos YouTube, sans limite de durée.

Transformez vos débats vidéo en rapports structurés d'analyse argumentative, soutenus par Groq (transcription) et Gemini (analyse IA).

---

## ✨ Fonctionnalités principales

### 🎯 Analyse complète
- 📝 **Arguments** : Type, solidité (forte/moyenne/faible), prémisses
- ⚠️ **Sophismes** : Identification avec confiance et justification
- ✓ **Faits à vérifier** : Extraction des affirmations factuelles
- 👥 **Multi-orateurs** : Suivi indépendant par intervenant

### 🚀 Performance & Scalabilité
- 📹 **Vidéos longues** : Jusqu'à **2h** supportées (découpage automatique)
- ⚡ **Streaming** : Traitement par segments (30min chacun)
- 🔀 **Analyse fusionnée** : Consolidation intelligente des résultats
- 📊 **Export JSON** : Format structuré pour intégration

### 🌍 Langues
- 🇫🇷 **Français** : Support natif (Whisper détecte la langue)
- 🌐 **Extensible** : Grammaires personnalisables via Gemini

---

## 🚀 Démarrage rapide

### Prérequis

- **Python 3.8+**
- **ffmpeg** (obligatoire pour vidéos > 1h) - [installation](INSTALLATION.md)
- **Clés API** : Google AI Studio + Groq

### Installation

```bash
# 1. Cloner le repo
cd ~/Projets/Arbitre

# 2. Installer ffmpeg (IMPORTANT pour vidéos longues)
# Windows
winget install ffmpeg

# macOS
brew install ffmpeg

# Linux
sudo apt-get install ffmpeg

# 3. Installer dépendances Python
pip install yt-dlp

# 4. Lancer le serveur
python arbitre_server.py
```

### Premier test

1. Ouvrir http://localhost:8766/arbitre.html
2. Coller une URL YouTube :
   - **Court** : `https://www.youtube.com/watch?v=...` (< 30min)
   - **Long** : Tout contenu jusqu'à 2h ✅
3. Remplir la clé Google AI et clé Groq
4. Optionnel : Ajouter le sujet du débat
5. Cliquer "Analyser" et attendre

---

## 📊 Cas d'usage

| Cas | Exemple | Temps | Notes |
|-----|---------|-------|-------|
| **Court débat** | 20 min | 5-8 min | Traitement direct, pas de découpage |
| **Débat standard** | 1h | 15-20 min | 2 segments Groq, 1-2 chunks Gemini |
| **Long débat** | 1h30 | 25-35 min | 3 segments, 2-3 chunks |
| **Conférence** | 2h | 40-50 min | 4 segments max, consolidation complète |

---

## ⚙️ Architecture

```
┌─────────────────────┐
│   Lien YouTube      │
└──────────┬──────────┘
           ↓
┌─────────────────────────────────────┐
│  Téléchargement & Découpage         │
│  (ffmpeg → segments 30min)          │
└──────────┬──────────────────────────┘
           ↓
┌─────────────────────────────────────┐
│  Transcription (Groq/Whisper)       │
│  - 1 segment = ~5 min               │
│  - Résultats fusionnés              │
└──────────┬──────────────────────────┘
           ↓
┌─────────────────────────────────────┐
│  Découpage Transcription            │
│  (chunks 15k caractères)            │
└──────────┬──────────────────────────┘
           ↓
┌─────────────────────────────────────┐
│  Analyse IA (Gemini)                │
│  - 1 chunk = ~5-10 min              │
│  - Extraction args/sophismes        │
│  - Fusion automatique               │
└──────────┬──────────────────────────┘
           ↓
┌─────────────────────────────────────┐
│  Résultats JSON                     │
│  - Transcription complète           │
│  - Analysis fusionnée               │
│  - Synthèse consolidée              │
└─────────────────────────────────────┘
```

---

## 📈 Performances

### Durée estimée par étape

```
Vidéo 1h00 (~700 MB)
├─ Téléchargement : 2-3 min
├─ Transcription (2 segments) : 10 min
├─ Analyse (2 chunks) : 10 min
└─ Total : ~20-25 min

Vidéo 2h00 (~1.4 GB)
├─ Téléchargement : 4-5 min
├─ Transcription (4 segments) : 20 min
├─ Analyse (4 chunks) : 15-20 min
└─ Total : ~40-50 min
```

### Limites techniques

| Limite | Valeur | Contournement |
|--------|--------|---------------|
| **Taille Groq** | 25 MB | Découpage automatique en segments |
| **Durée Groq** | 900s (15 min) | Segments courtenent (30min max) |
| **Timeout Gemini** | 600s (10 min) | Chunking transcription (~15k car) |
| **Durée support** | 2h | Limitation API, pas de découpage > 4 segments |

---

## 📝 Format réponse

### Réussite
```json
{
  "transcript": "Transcription complète de la vidéo...",
  "analysis": {
    "orateurs": [
      {
        "nom": "Orateur A",
        "these_principale": "Thèse principale défendue",
        "arguments": [
          {
            "extrait": "Citation exacte du texte",
            "these": "Énoncé de l'argument",
            "premisses": ["Prémisse 1", "Prémisse 2"],
            "type": "déductif|inductif|analogie|exemple|autorité|conséquences|statistique",
            "solidite": "forte|moyenne|faible",
            "justification": "Courte explication"
          }
        ],
        "sophismes": [
          {
            "nom": "Ad hominem",
            "extrait": "Citation du sophisme",
            "explication": "Brève explication du sophisme",
            "confiance": "élevé|moyen|faible"
          }
        ]
      }
    ],
    "synthese": "Résumé consolidé des points clés du débat",
    "faits_a_verifier": [
      "Affirmation factuelle 1",
      "Affirmation factuelle 2"
    ],
    "chunks_analyzed": 2
  }
}
```

### Erreur
```json
{
  "error": "Message d'erreur descriptif"
}
```

Erreurs courantes :
- `"Fichier trop volumineux pour Groq"` → Vidéo > 2h ou format non supporté
- `"Clé Google AI... manquante"` → Vérifier les clés API
- `"Le traitement de la vidéo par Google prend trop de temps"` → Réessayer ou vidéo trop complexe

---

## 🔧 Configuration

### Variables d'environnement

Éditer `arbitre_server.py` :

```python
PORT = 8766                      # Port du serveur
MAX_SEGMENT_DURATION = 30        # Minutes par segment (Groq)
MAX_TRANSCRIPT_CHUNK = 15000     # Caractères max par chunk (Gemini)
```

### Ajuster selon votre utilisation

- **Vidéos très longues (> 2h)** : Réduire `MAX_SEGMENT_DURATION` à 20min
- **Analyses moins détaillées** : Augmenter `MAX_TRANSCRIPT_CHUNK` à 25000
- **Meilleure qualité** : Réduire à 10000 (plus lent)

---

## 🔐 Sécurité & Clés API

### Obtenir les clés

1. **Google AI Studio** (gratuit)
   - https://aistudio.google.com/app/apikey
   - Créer une nouvelle clé API
   - Ne pas partager, regénérer régulièrement

2. **Groq API** (gratuit avec limits)
   - https://console.groq.com/keys
   - S'inscrire avec email
   - Générer une clé de développement

### Bonnes pratiques

```bash
# ❌ Mauvais : clés en dur dans le code
const KEY = "sk-proj-abc123..."

# ✅ Bon : utiliser des variables d'environnement
# Créer .env.local (non versionné)
GOOGLE_AI_KEY=sk-proj-...
GROQ_KEY=gsk_...
```

> **Note** : Le fichier `.env` n'est pas versionné. Les clés restent confidentielles.

---

## 🛠️ Dépannage

### Erreurs courantes

#### "Module yt-dlp manque"
```bash
pip install --user yt-dlp
```

#### "ffmpeg/ffprobe non trouvés"
- **Windows** : `winget install ffmpeg` ou [ffmpeg.org](https://ffmpeg.org/download.html)
- **macOS** : `brew install ffmpeg`
- **Linux** : `sudo apt-get install ffmpeg`

Vérifier :
```bash
ffmpeg -version
ffprobe -version
```

#### "Transcription vide de Groq"
- Vidéo sans dialogue (musique/paysage)
- Format audio non supporté
- Réessayer avec une autre vidéo

#### "Timeout Google"
- Gemini traite le fichier mais lentement
- Réessayer (max 300 tentatives = 5 min)
- Si persiste : vidéo trop complexe pour chunk

#### "Clé API invalide"
- Vérifier format : commence par `sk-proj-` (Google) ou `gsk_` (Groq)
- Vérifier que la clé n'est pas expirée
- Regénérer si nécessaire

---

## 📚 Documentation avancée

- [INSTALLATION.md](INSTALLATION.md) - Guide détaillé d'installation
- Code source : [arbitre_server.py](arbitre_server.py)

### Fonctions clés

| Fonction | Rôle |
|----------|------|
| `split_audio()` | Découpe vidéo en segments ffmpeg |
| `transcribe_segments()` | Transcrit segments + fusion |
| `chunk_transcript()` | Découpe transcription pour Gemini |
| `analyze_chunks()` | Analyse + consolidation résultats |
| `gemini_text()` | Appel API Gemini avec retry |
| `groq_transcribe()` | Appel API Groq transcription |

---

## 🚀 Améliorations futures

- [ ] Support multilangue complet
- [ ] Export PDF rapport
- [ ] UI interactive pour validation des résultats
- [ ] Cache des transcriptions
- [ ] API REST complète
- [ ] Déploiement Docker

---

## 📄 Licence

Voir [LICENSE](LICENSE) - À ajouter

## 👨‍💻 Auteur

**Mousler** - Projet d'analyse de débats avec IA

---

## 📞 Support

- Issues GitHub : [Signaler un bug](https://github.com/mousler/Arbitre/issues)
- Documentation : [Wiki](https://github.com/mousler/Arbitre/wiki)
- Email : contact@arbitre.local (à adapter)

---

**Dernière mise à jour** : Septembre 2026 | **Version** : 2.0 (Support vidéos longues)
