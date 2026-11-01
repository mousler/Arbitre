#!/usr/bin/env python3
"""Arbitre : vidéo YouTube (même longue) → transcription par segments → analyse argumentative par parties
(arguments, sophismes, erreurs logiques, faits à vérifier) → fusion par orateur et synthèse finale."""

import json
import base64
import hashlib
import hmac
import ipaddress
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import transcript_server as ts  # téléchargement/transcription, TLS via proxy (truststore), suivi des quotas
import community  # comptes, communauté, défis, administration (SQLite dans data/)

STATIC_RE = re.compile(r"^/[\w-]+\.html$")  # seules les pages HTML racine sont servies (jamais data/, .py, .venv…)
STATIC_PAGES = {"/arbitre.html", "/simulation.html"}  # transcript.html appartient au serveur de démonstration (8765)
# Le script inline de chaque page est autorisé par son empreinte : un script injecté ne s'exécute pas.
CSP_BASE = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; media-src 'self' blob: data:; "
            "font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
CSP_PAGES = {
    "/arbitre.html": "script-src 'self' blob: https://www.youtube.com https://s.ytimg.com {hashes}; "
                     "frame-src https://www.youtube.com https://www.youtube-nocookie.com; form-action 'self'",
    "/simulation.html": "script-src 'self' {hashes}; frame-src 'none'; form-action 'self' http://127.0.0.1:*",
}
PERMISSIONS_POLICY = "camera=(), geolocation=(), payment=(), usb=(), microphone=(self), autoplay=(self)"
INLINE_SCRIPT_RE = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_csp_cache = {}
MAX_RUNNING_JOBS = 4      # analyses exécutées en même temps ; les suivantes attendent leur tour
MAX_ACTIVE_JOBS = 40      # analyses en cours ou en attente sur tout le serveur
MAX_JOBS_PER_OWNER = 3    # par compte (ou adresse, sans compte)
if os.environ.get("RHETORA_SIMULATION_MODE") == "1":  # analyses déterministes instantanées : la sandbox teste jusqu'à 100 acteurs simultanés
    MAX_RUNNING_JOBS, MAX_ACTIVE_JOBS = 16, 200
EXPLORE_CACHE_MAX = 300
PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/(?:tmp|var|home|Users|usr|private)/)[^\s'\"]*")


def effective_client_ip(remote_ip, headers, simulation_mode=False):
    if simulation_mode:
        try:
            remote = ipaddress.ip_address(remote_ip)
            simulated = ipaddress.ip_address(headers.get("X-Rhetora-Simulated-IP", ""))
            if remote.is_loopback and simulated.is_loopback:
                return str(simulated)
        except ValueError:
            pass
    return remote_ip

PORT = int(os.environ.get("ARBITRE_PORT", "8766"))
SIMULATION_MODE = os.environ.get("RHETORA_SIMULATION_MODE") == "1"
SIMULATION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulation-results")
SIMULATION_STATUS_FILE = os.path.join(SIMULATION_DIR, "live.json")
SIMULATION_LOG_FILE = os.path.join(SIMULATION_DIR, "controller.log")
SIMULATION_CANCEL_FILE = os.path.join(SIMULATION_DIR, "cancel.requested")
SIMULATION_ACTIVITY_FILE = os.path.join(SIMULATION_DIR, "live-activity.jsonl")
SIMULATION_ACTORS_FILE = os.path.join(SIMULATION_DIR, "live-actors.json")
SIMULATION_VIEW_KEY = os.environ.get("RHETORA_SIMULATION_VIEW_KEY", "") if SIMULATION_MODE else ""
SIMULATION_VIEW_TARGET_RE = re.compile(r"#/(?:post/\d{1,12}|u/[^\s#]{1,120}|messages(?:/[^\s#]{1,120})?|communaute|admin)")
simulation_lock = threading.Lock()
simulation_process = None
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_CHAT_MODELS = [  # essayés dans l'ordre (repli sur quota épuisé / modèle indisponible)
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "llama-3.1-8b-instant",
]
ANALYSIS_ENGINES = ("groq", "gemini")
CHUNK_CHARS = 9000   # ≈ 2 500 tokens par partie : reste sous les limites/minute du tier gratuit Groq
ENTRY_CHARS = 700    # taille des blocs affichés dans la transcription
JOB_TTL = 6 * 3600   # conservation des tâches en mémoire (s)
EXPLORE_PAGE = 24    # résultats par page dans l'explorateur
EXPLORE_TTL = 900    # cache des recherches (s)
# Filtres de recherche YouTube (paramètre « sp ») : (tri, durée YouTube)
EXPLORE_FILTERS = {
    ("relevance", "any"): "", ("relevance", "long"): "EgIYAg%3D%3D", ("relevance", "medium"): "EgIYAw%3D%3D", ("relevance", "short"): "EgIYAQ%3D%3D",
    ("date", "any"): "CAI%3D", ("date", "long"): "CAISAhgC", ("date", "medium"): "CAISAhgD", ("date", "short"): "CAISAhgB",
    ("views", "any"): "CAM%3D", ("views", "long"): "CAMSAhgC", ("views", "medium"): "CAMSAhgD", ("views", "short"): "CAMSAhgB",
}
# Durées proposées : clé → (filtre YouTube, durée min en s, durée max en s) ; les bornes sont appliquées localement
EXPLORE_DURATIONS = {
    "any": ("any", 0, None), "short": ("short", 0, 240), "u10": ("any", 0, 600), "u20": ("any", 0, 1200),
    "u30": ("any", 0, 1800), "u60": ("any", 0, 3600), "medium": ("medium", 0, None), "long": ("long", 0, None),
    "o60": ("long", 3600, None),
}
EXPLORE_MAX_PAGE = 12   # pages YouTube parcourues au maximum pour une recherche
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

CHUNK_PROMPT = """Tu es un expert en logique, rhétorique et argumentation. Tu analyses UN EXTRAIT de la transcription automatique d'un débat, d'une interview ou d'une vidéo (les autres extraits sont analysés séparément puis fusionnés).
La transcription peut contenir des erreurs de reconnaissance vocale : ne les considère pas comme des fautes de raisonnement.

Identification des orateurs :
- Si les lignes sont préfixées par un nom (« Nom (heure) : texte »), utilise ce nom.
- Sinon, la transcription n'indique pas qui parle : déduis-le des indices (noms cités, formules d'adresse, présentations, changements de position).
- Réutilise EXACTEMENT les noms déjà identifiés qui te sont fournis. Sans nom identifiable, utilise « Orateur A », « Orateur B »… S'il n'y a qu'une voix, un seul orateur.

Pour chaque orateur présent dans l'extrait, identifie :
1. ARGUMENTS : thèse défendue, prémisses, type (déductif, inductif, analogie, exemple, autorité, conséquences, statistique), solidité (forte, moyenne, faible) avec une justification courte.
2. SOPHISMES, uniquement s'ils sont clairement présents. Catalogue de référence : attaque personnelle (ad hominem), homme de paille, faux dilemme, pente glissante, appel à l'autorité non pertinente, appel à la popularité, appel à l'émotion, généralisation hâtive, corrélation n'est pas causalité, raisonnement circulaire, diversion (hareng rouge), tu quoque, charge de la preuve inversée, appel à l'ignorance, appel à la tradition, appel à la nature, question piège, déplacement des critères, sophisme du juste milieu, sélection partiale des données (cherry picking).
3. ERREURS LOGIQUES (champ "incoherences") : contradiction interne de l'orateur, conclusion qui ne découle pas des prémisses (non sequitur), confusion condition nécessaire / suffisante, oubli du taux de base, extrapolation abusive, erreur évidente de calcul ou d'ordre de grandeur.
4. FAITS À VÉRIFIER : affirmations factuelles (chiffres, dates, études, événements), sans juger si elles sont vraies.

Règles :
- Impartialité : même niveau d'exigence pour tous les orateurs, quel que soit leur point de vue.
- En cas de doute, ne signale rien : mieux vaut rater un sophisme que d'en inventer un. Une affirmation forte ou une émotion n'est pas automatiquement un sophisme.
- « extrait » = citation EXACTE et courte (25 mots maximum) copiée de la transcription.
- Explications simples (2 phrases maximum), compréhensibles par un non-spécialiste.
- N'analyse que le contenu de l'extrait fourni.

Réponds UNIQUEMENT avec un objet JSON valide, sans texte autour, au format :
{"orateurs":[{"nom":"string","these_principale":"string","arguments":[{"extrait":"string","these":"string","premisses":["string"],"type":"string","solidite":"forte|moyenne|faible","justification":"string"}],"sophismes":[{"nom":"string","extrait":"string","explication":"string","confiance":"élevé|moyen|faible"}],"incoherences":[{"type":"string","extrait":"string","explication":"string"}],"faits_a_verifier":["string"]}],"resume":"string (3 phrases maximum : ce qui se dit dans cet extrait)"}"""

SYNTHESIS_PROMPT = """Tu es un arbitre de débat impartial. On te fournit le déroulé résumé d'un débat (analysé par parties successives) et le bilan argumentatif de chaque orateur (thèse, solidité des arguments, sophismes et erreurs logiques relevés).
Rédige la synthèse finale et reformule la thèse principale de chaque orateur sur l'ensemble du débat.
Réponds UNIQUEMENT avec un objet JSON valide :
{"synthese":"string (6 à 8 phrases : points de désaccord, qualité argumentative de chaque orateur, sophismes et erreurs logiques les plus marquants, points restant à vérifier)","orateurs":[{"nom":"string","these_principale":"string (1 à 2 phrases)"}]}"""

MAP_PROMPT = """Tu es un arbitre de débat expert en logique et en théorie de l'argumentation (modèle de Toulmin, cartographie argumentative).
On te fournit le sujet, la synthèse, le déroulé et les éléments extraits d'un débat (thèses, arguments avec prémisses et solidité, sophismes, erreurs logiques, faits à vérifier).
Construis l'ARBRE ARGUMENTATIF complet du débat :
- question : la question débattue, formulée comme une vraie question.
- these : la thèse principale en jeu (celle que défend la position POUR).
- pour / contre : les deux positions en présence (intitulé court + orateurs qui la portent). S'il n'y a qu'un orateur, la position CONTRE regroupe les objections évoquées ou qu'on peut raisonnablement lui opposer.
- arguments : 2 à 4 arguments par position, les plus importants (fusionne les doublons). Identifiants A1, A2… pour POUR et B1, B2… pour CONTRE. Décompose chacun finement : type (causal, exemple, analogie, autorité, statistique, valeurs, conséquences, définition…), premisses (2 à 3 énoncés sur lesquels il repose), hypotheses (présupposés non démontrés, souvent implicites), evidences (preuves, chiffres, exemples, études citées ; liste vide s'il n'y en a aucune), garant (la règle qui relie les preuves à la conclusion), conclusion (conclusion locale de l'argument), moment (horodatage approximatif s'il est connu, sinon "").
- sophismes : TOUS les sophismes et erreurs logiques relevés dans le débat (S1, S2…), rattachés au nœud où ils sont commis ("cible" = A1, B2, O1, R1…) ; "nom" = nom du sophisme, "extrait" = citation courte, "explication" = pourquoi le raisonnement est fautif, "gravite" = forte si le sophisme porte tout l'argument.
- objections : objections adressées aux arguments (O1, O2…), "cible" = identifiant de l'argument visé ; "explicite": true si elle est formulée dans le débat, false si c'est toi qui la relèves.
- refutations : réponses aux objections (R1, R2…), "cible" = identifiant de l'objection ; "contre_refutation" si une réponse à la réfutation existe ou s'impose, sinon null.
- hypotheses : hypothèses explicites et implicites du débat ("porteur" = identifiant de l'argument qui en dépend) et risques de raisonnement (sophismes, biais, erreurs logiques ; "concerne" = identifiants).
- evaluation : pour chaque critère, une force et un commentaire d'une phrase. Pour "risques", forte = risques bien maîtrisés.
- phases : le DÉROULÉ du débat en 3 à 6 phases chronologiques (ouverture, offensives, contre-attaques, tournants, clôture) : titre, moment, resume (ce qui s'y joue), "noeuds" = identifiants des arguments / objections / réfutations / sophismes apparus dans cette phase, "avantage" = camp qui domine la phase, "tournant": true si la phase fait basculer le débat.
- dynamique : comment le débat a fonctionné — "initiative" (camp qui a mené et imposé ses thèmes), "commentaire" (2 à 3 phrases : qui attaque, qui répond, qui esquive, qui change de terrain), "esquives" (questions ou objections restées sans réponse), "terrain" (les points de désaccord réels).
- conclusion : conclusion finale de l'arbitre et position la mieux étayée (pour, contre ou equilibre).
"force" vaut toujours forte, moyenne ou faible.
Relations (identifiants définis dans ta réponse uniquement, "T" = thèse principale) :
- supports : nœuds que ce nœud renforce ; attacks : nœuds qu'il contredit ou affaiblit ; depends_on : nœuds dont sa validité dépend.
En général un argument POUR supports ["T"] et un argument CONTRE attacks ["T"] ; ajoute les liens croisés (argument qui en contredit un autre, qui repose sur un autre).
Règles : impartialité stricte ; textes courts (25 mots maximum) compréhensibles par un non-spécialiste ; n'invente aucun fait ; reste fidèle à ce qui a été dit.

Réponds UNIQUEMENT avec un objet JSON valide :
{"question":"string","these":{"texte":"string","force":"forte|moyenne|faible"},
"pour":{"intitule":"string","orateurs":["string"]},"contre":{"intitule":"string","orateurs":["string"]},
"arguments":[{"id":"A1","camp":"pour|contre","titre":"string","orateur":"string","type":"string","moment":"string","force":"forte|moyenne|faible","premisses":["string"],"hypotheses":["string"],"evidences":[{"texte":"string","force":"forte|moyenne|faible"}],"garant":"string","conclusion":"string","supports":["T"],"attacks":[],"depends_on":[]}],
"sophismes":[{"id":"S1","cible":"B1","nom":"string","orateur":"string","extrait":"string","explication":"string","gravite":"forte|moyenne|faible"}],
"objections":[{"id":"O1","cible":"A1","texte":"string","orateur":"string","force":"forte|moyenne|faible","explicite":true}],
"refutations":[{"id":"R1","cible":"O1","texte":"string","orateur":"string","force":"forte|moyenne|faible","contre_refutation":{"texte":"string","orateur":"string","force":"forte|moyenne|faible"}}],
"hypotheses":{"explicites":[{"texte":"string","porteur":"A1"}],"implicites":[{"texte":"string","porteur":"B1"}],"risques":[{"texte":"string","concerne":["A1"]}]},
"evaluation":{"solidite_logique":{"force":"forte|moyenne|faible","commentaire":"string"},"qualite_preuves":{"force":"…","commentaire":"…"},"coherence_interne":{"force":"…","commentaire":"…"},"faisabilite":{"force":"…","commentaire":"…"},"risques":{"force":"…","commentaire":"…"}},
"phases":[{"titre":"string","moment":"string","resume":"string","noeuds":["A1","O1"],"avantage":"pour|contre|equilibre","tournant":false}],
"dynamique":{"initiative":"pour|contre|equilibre","commentaire":"string","esquives":["string"],"terrain":["string"]},
"conclusion":{"texte":"string (3 à 4 phrases)","force":"forte|moyenne|faible","avantage":"pour|contre|equilibre"}}"""
MAP_INPUT_CHARS = 12000  # ≈ 3 500 tokens d'entrée : reste sous les limites/minute du tier gratuit Groq
MAP_MAX_TOKENS = 7000
FORCES = ("forte", "moyenne", "faible")
EVAL_KEYS = ("solidite_logique", "qualite_preuves", "coherence_interne", "faisabilite", "risques")


class FatalError(RuntimeError):
    """Erreur non récupérable (clé refusée…) : inutile d'essayer les parties suivantes"""


def http_error_message(e):
    raw = e.read().decode("utf-8", errors="replace")
    try:
        return str(json.loads(raw).get("error", {}).get("message", raw))[:200]
    except (ValueError, AttributeError):
        return raw[:200]


def check_groq_key(key):
    """Vérifie la clé Groq en quelques secondes (liste des modèles) avant de lancer un long traitement"""
    hint = f"clé reçue : {key[:4]}…{key[-4:]}, {len(key)} caractères"
    if key.startswith("AIza"):
        raise ValueError(f"La clé saisie dans le champ Groq est une clé Google AI ({hint}). Une clé Groq commence par gsk_.")
    if not key.startswith("gsk_"):
        raise ValueError(f"Ce n'est pas une clé Groq : elle doit commencer par gsk_ ({hint}).")
    req = urllib.request.Request("https://api.groq.com/openai/v1/models", headers={
        "Authorization": f"Bearer {key}", "User-Agent": "arbitre/1.0",
    })
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError(f"Groq refuse la clé ({http_error_message(e)} ; {hint}). "
                             "L'administrateur doit la remplacer dans Administration › Modèle IA.")
    except (urllib.error.URLError, TimeoutError):
        pass  # réseau indisponible : l'erreur réelle apparaîtra pendant la tâche


def groq_chat(api_key, system, user, max_tokens=4096, models=None, temperature=0.2):
    """Chat completion Groq (API compatible OpenAI) en mode JSON, avec repli entre modèles"""
    last_error = "quota épuisé sur tous les modèles Groq"
    for model in models or GROQ_CHAT_MODELS:
        if ts.is_exhausted(model):
            continue
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"
        data = json.dumps(payload).encode("utf-8")
        for attempt in range(ts.MAX_RETRIES):
            req = urllib.request.Request(GROQ_CHAT_URL, data=data, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "arbitre/1.0",  # l'UA par défaut de urllib est bloqué par Cloudflare
            })
            wait = 5 * 2 ** attempt
            try:
                with urllib.request.urlopen(req, timeout=300) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return body["choices"][0]["message"].get("content") or ""
            except urllib.error.HTTPError as e:
                message = http_error_message(e)
                last_error = f"HTTP {e.code} ({model}): {message}"
                if e.code in (401, 403):
                    raise FatalError(f"Clé Groq refusée : {message}")
                if e.code in (404, 413):  # modèle retiré / requête trop grosse pour ce modèle
                    break
                if e.code == 429:
                    try:
                        retry_after = float(e.headers.get("retry-after") or 0)
                    except ValueError:
                        retry_after = 0
                    if retry_after > 60:  # quota horaire/journalier : modèle suivant
                        ts.exhausted_until[model] = time.time() + retry_after
                        print(f"   … {model}: quota épuisé (dispo dans {retry_after / 60:.0f} min)")
                        break
                    wait = max(retry_after, 2)
                elif e.code not in (400, 500, 502, 503, 504):  # 400 : souvent un JSON mal formé par le modèle
                    raise RuntimeError(last_error)
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError) as e:
                last_error = f"{type(e).__name__} ({model}): {e}"
            if attempt < ts.MAX_RETRIES - 1:
                time.sleep(wait)
        else:
            print(f"   … {model} indisponible, modèle suivant")
    raise RuntimeError(f"Échec Groq: {last_error}")


def gemini_chat(api_key, system, user):
    """Appel Gemini en mode JSON (repli de modèles et quotas gérés par transcript_server)"""
    def payload_for_model(model):
        config = {"temperature": 0.2, "maxOutputTokens": 8192, "responseMimeType": "application/json"}
        if model.startswith("gemini-2.5"):
            config["thinkingConfig"] = {"thinkingBudget": 0}
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }

    try:
        result = ts.gemini_generate(api_key, payload_for_model)
    except RuntimeError as e:
        if "API key" in str(e) or re.search(r"HTTP 40[13]", str(e)):
            raise FatalError(f"Clé Google AI refusée : {e}")
        raise
    candidates = result.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini : réponse vide")
    return "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))


def llm_json(engine, keys, system, user, max_tokens=4096):
    """Appelle le moteur d'analyse et retourne l'objet JSON produit (2 essais si la réponse est invalide)"""
    if SIMULATION_MODE:
        if system == CHUNK_PROMPT:
            return {
                "orateurs": [{
                    "nom": "Orateur A",
                    "these_principale": "Les essais isolés aident à repérer les régressions.",
                    "arguments": [{
                        "extrait": "Les essais isolés protègent les données réelles.",
                        "these": "Un environnement isolé réduit les risques des essais.",
                        "premisses": ["Les données réelles ne sont pas modifiées."],
                        "type": "exemple",
                        "solidite": "forte",
                        "justification": "Réponse déterministe réservée aux tests locaux.",
                    }],
                    "sophismes": [],
                    "incoherences": [],
                    "faits_a_verifier": [],
                }],
                "resume": "Analyse déterministe produite en mode simulation locale.",
            }
        if system == SYNTHESIS_PROMPT:
            return {
                "synthese": "Synthèse déterministe utilisée pour valider le parcours d'analyse.",
                "orateurs": [{
                    "nom": "Orateur A",
                    "these_principale": "Les essais isolés aident à repérer les régressions.",
                }],
            }
        if system == MAP_PROMPT:
            return {
                "question": "Le parcours d'analyse produit-il une carte argumentative ?",
                "these": {"texte": "Le parcours doit produire une carte exploitable.", "force": "moyenne"},
                "pour": {"intitule": "Parcours valide", "orateurs": ["Orateur A"]},
                "contre": {"intitule": "Parcours invalide", "orateurs": []},
                "arguments": [{
                    "id": "A1", "camp": "pour", "titre": "La carte est normalisee.",
                    "orateur": "Orateur A", "type": "exemple", "force": "moyenne",
                    "premisses": ["Le fournisseur simule le prompt de carte."],
                    "evidences": [], "garant": "Chaque prompt doit avoir une reponse compatible.",
                    "conclusion": "La carte peut etre normalisee.", "supports": ["T"],
                }],
                "conclusion": {"texte": "Le prompt de carte est pris en charge.", "force": "moyenne", "avantage": "pour"},
            }
        raise RuntimeError("Prompt non pris en charge par le moteur IA simulé.")

    problem = None
    for _ in range(2):
        raw = (groq_chat(keys["groq"], system, user, max_tokens, keys.get("groq_models"), keys.get("temperature", 0.2))
               if engine == "groq" else gemini_chat(keys["gemini"], system, user))
        start, end = raw.find("{"), raw.rfind("}")
        try:
            data = json.loads(raw[start:end + 1]) if 0 <= start < end else None
        except ValueError as e:
            data, problem = None, e
        if isinstance(data, dict):
            return data
        problem = problem or "aucun objet JSON"
    raise RuntimeError(f"Réponse inexploitable du modèle ({problem})")


def fmt_time(seconds):
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def split_units(blocks, size):
    """Découpe des blocs {start, duration, text} en unités ≤ size (phrases, sinon mots), avec horodatage estimé"""
    units = []
    for block in blocks:
        text = block["text"].strip()
        pieces = [text] if len(text) <= size else SENTENCE_END.split(" ".join(text.split()))
        offset = 0
        for piece in pieces:
            while piece:
                cut = len(piece) if len(piece) <= size else (piece.rfind(" ", 0, size) if piece.rfind(" ", 0, size) > 0 else size)
                part, piece = piece[:cut].strip(), piece[cut:].strip()
                start = None
                if block.get("start") is not None:
                    start = block["start"] + block.get("duration", 0) * offset / max(len(text), 1)
                if part:
                    units.append((start, part))
                offset += cut + 1
    return units


def group_units(units, size):
    """Regroupe les unités consécutives en paquets d'environ size caractères"""
    groups, current, length = [], [], 0
    for unit in units:
        if current and length + len(unit[1]) > size:
            groups.append(current)
            current, length = [], 0
        current.append(unit)
        length += len(unit[1]) + 1
    if current:
        groups.append(current)
    return groups


def merge_chunk(speakers, data, where):
    """Fusionne l'analyse d'une partie dans le bilan global, orateur par orateur"""
    for orateur in data.get("orateurs") or []:
        if not isinstance(orateur, dict):
            continue
        name = str(orateur.get("nom") or "Orateur").strip()
        target = speakers.setdefault(name.lower(), {
            "nom": name, "these_principale": "", "arguments": [], "sophismes": [], "incoherences": [], "faits_a_verifier": [],
        })
        if not target["these_principale"]:
            target["these_principale"] = str(orateur.get("these_principale") or "")
        for field in ("arguments", "sophismes", "incoherences"):
            for item in orateur.get(field) or []:
                if isinstance(item, dict):
                    target[field].append({**item, "horodatage": where})
        for fact in orateur.get("faits_a_verifier") or []:
            text = fact if isinstance(fact, str) else json.dumps(fact, ensure_ascii=False)
            if text.strip():
                target["faits_a_verifier"].append(f"[{where}] {text.strip()}")


def _text(value, limit=400):
    return " ".join(str(value or "").split())[:limit]


def _force(value):
    v = str(value or "").strip().lower()
    return next((f for f in FORCES if v.startswith(f[:4])), "moyenne")


def _list(value):
    return value if isinstance(value, list) else []


def map_input(analysis, topic):
    """Bilan compact du débat pour la construction de l'arbre (tronqué pour tenir dans le budget de tokens)"""
    rank = {"forte": 0, "moyenne": 1, "faible": 2}
    deroule = "\n".join(str(s) for s in _list(analysis.get("deroule")))[:3000]
    text = ""
    for cap in (12, 8, 5, 3, 2):
        bilan = []
        for o in _list(analysis.get("orateurs")):
            if not isinstance(o, dict):
                continue
            args = sorted((a for a in _list(o.get("arguments")) if isinstance(a, dict)), key=lambda a: rank.get(a.get("solidite"), 3))
            bilan.append({
                "nom": _text(o.get("nom"), 80),
                "these": _text(o.get("these_principale"), 300),
                "arguments": [{
                    "moment": _text(a.get("horodatage"), 20),
                    "these": _text(a.get("these") or a.get("extrait"), 220),
                    "premisses": [_text(p, 150) for p in _list(a.get("premisses"))[:3]],
                    "type": _text(a.get("type"), 40),
                    "solidite": _text(a.get("solidite"), 10),
                    "justification": _text(a.get("justification"), 150),
                } for a in args[:cap]],
                "sophismes": [f"[{_text(s.get('horodatage'), 20)}] {_text(s.get('nom'), 60)} : {_text(s.get('extrait'), 120)} — {_text(s.get('explication'), 120)}" for s in _list(o.get("sophismes"))[:cap] if isinstance(s, dict)],
                "erreurs_logiques": [f"{_text(x.get('type'), 60)} : {_text(x.get('explication'), 120)}" for x in _list(o.get("incoherences"))[:cap] if isinstance(x, dict)],
                "faits_a_verifier": [_text(f, 150) for f in _list(o.get("faits_a_verifier"))[:cap]],
            })
        text = "\n".join([
            f"Sujet : {topic or 'non précisé'}", "", "Synthèse :", _text(analysis.get("synthese"), 2000) or "—", "",
            "Déroulé :", deroule or "—", "", "Éléments par orateur :", json.dumps(bilan, ensure_ascii=False),
        ])
        if len(text) <= MAP_INPUT_CHARS:
            break
    return text[:MAP_INPUT_CHARS]


def normalize_map(data):
    """Valide l'arbre produit par le modèle : identifiants uniques, forces normalisées, relations vers des nœuds existants"""
    def key(v):
        return str(v or "").strip().upper()
    ids = {"T": "T", "THESE": "T", "THÈSE": "T"}
    raw_args = [a for a in _list(data.get("arguments")) if isinstance(a, dict)][:10]
    raw_obj = [o for o in _list(data.get("objections")) if isinstance(o, dict)][:12]
    raw_ref = [r for r in _list(data.get("refutations")) if isinstance(r, dict)][:12]
    raw_soph = [s for s in _list(data.get("sophismes")) if isinstance(s, dict) and _text(s.get("nom"))][:12]
    count = {"A": 0, "B": 0}
    for a in raw_args:
        prefix = "B" if str(a.get("camp", "")).strip().lower().startswith("contre") else "A"
        count[prefix] += 1
        a["_id"] = f"{prefix}{count[prefix]}"
    for prefix, group in (("O", raw_obj), ("R", raw_ref), ("S", raw_soph)):
        for i, x in enumerate(group, 1):
            x["_id"] = f"{prefix}{i}"
    for x in raw_args + raw_obj + raw_ref + raw_soph:  # renumérotation : on traduit les identifiants d'origine
        ids.setdefault(key(x.get("id")) or x["_id"], x["_id"])
        ids.setdefault(x["_id"], x["_id"])
    for r in raw_ref:
        ids.setdefault(f"{r['_id']}.CR", f"{r['_id']}.CR")

    def refs(values, self_id=""):
        out = []
        for v in values if isinstance(values, list) else [values]:
            r = ids.get(key(v))
            if r and r != self_id and r not in out:
                out.append(r)
        return out

    def evidence(e):
        text = _text(e.get("texte") if isinstance(e, dict) else e)
        return text and {"texte": text, "force": _force(e.get("force") if isinstance(e, dict) else None)}

    def texts(values):
        return [t for t in (_text(v.get("texte") if isinstance(v, dict) else v) for v in _list(values)[:6]) if t]

    arguments = []
    for a in raw_args:
        aid, camp = a["_id"], "contre" if a["_id"][0] == "B" else "pour"
        arg = {
            "id": aid, "camp": camp, "titre": _text(a.get("titre") or a.get("conclusion"), 300), "orateur": _text(a.get("orateur"), 80),
            "type": _text(a.get("type"), 40), "moment": _text(a.get("moment"), 20),
            "force": _force(a.get("force")), "premisses": texts(a.get("premisses")), "hypotheses": texts(a.get("hypotheses")),
            "evidences": [e for e in map(evidence, _list(a.get("evidences"))[:6]) if e], "garant": _text(a.get("garant"), 300),
            "conclusion": _text(a.get("conclusion"), 300),
            "supports": refs(a.get("supports"), aid), "attacks": refs(a.get("attacks"), aid), "depends_on": refs(a.get("depends_on"), aid),
        }
        if not (arg["supports"] or arg["attacks"]):
            arg["supports" if camp == "pour" else "attacks"] = ["T"]
        arguments.append(arg)
    if not arguments:
        raise RuntimeError("Arbre argumentatif vide")

    objections = [{
        "id": o["_id"], "cible": (refs(o.get("cible"), o["_id"]) or [""])[0], "texte": _text(o.get("texte") or o.get("titre"), 400),
        "orateur": _text(o.get("orateur"), 80), "force": _force(o.get("force")), "explicite": o.get("explicite") is not False,
    } for o in raw_obj]
    refutations = []
    for r in raw_ref:
        cr = r.get("contre_refutation")
        cr = isinstance(cr, dict) and _text(cr.get("texte")) and {"texte": _text(cr.get("texte")), "orateur": _text(cr.get("orateur"), 80), "force": _force(cr.get("force"))}
        refutations.append({
            "id": r["_id"], "cible": next((x for x in refs(r.get("cible"), r["_id"]) if x[0] == "O"), ""), "texte": _text(r.get("texte"), 400),
            "orateur": _text(r.get("orateur"), 80), "force": _force(r.get("force")), "contre_refutation": cr or None,
        })

    sophismes = [{
        "id": s["_id"], "cible": (refs(s.get("cible"), s["_id"]) or [""])[0], "nom": _text(s.get("nom"), 80), "orateur": _text(s.get("orateur"), 80),
        "extrait": _text(s.get("extrait"), 300), "explication": _text(s.get("explication"), 400), "gravite": _force(s.get("gravite") or s.get("force")),
    } for s in raw_soph]
    def camp(value):
        v = str(value or "").strip().lower()
        return v if v in ("pour", "contre") else "equilibre"
    phases = [{
        "titre": _text(p.get("titre"), 120), "moment": _text(p.get("moment"), 30), "resume": _text(p.get("resume"), 500),
        "noeuds": refs(p.get("noeuds")), "avantage": camp(p.get("avantage")), "tournant": p.get("tournant") is True,
    } for p in _list(data.get("phases"))[:8] if isinstance(p, dict) and (_text(p.get("titre")) or _text(p.get("resume")))]
    dyn = data.get("dynamique") if isinstance(data.get("dynamique"), dict) else {}
    dynamique = {
        "initiative": camp(dyn.get("initiative")), "commentaire": _text(dyn.get("commentaire"), 800),
        "esquives": [t for t in (_text(x, 300) for x in _list(dyn.get("esquives"))[:6]) if t],
        "terrain": [t for t in (_text(x, 300) for x in _list(dyn.get("terrain"))[:6]) if t],
    }

    hyp = data.get("hypotheses") if isinstance(data.get("hypotheses"), dict) else {}
    def assumptions(values):
        return [{"texte": _text(h.get("texte") if isinstance(h, dict) else h), "porteur": (refs(h.get("porteur")) or [""])[0] if isinstance(h, dict) else ""}
                for h in _list(values)[:8] if _text(h.get("texte") if isinstance(h, dict) else h)]
    ev = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else {}
    concl = data.get("conclusion") if isinstance(data.get("conclusion"), dict) else {"texte": data.get("conclusion")}
    these = data.get("these") if isinstance(data.get("these"), dict) else {"texte": data.get("these")}
    def position(p):
        p = p if isinstance(p, dict) else {}
        return {"intitule": _text(p.get("intitule"), 300), "orateurs": [_text(n, 80) for n in _list(p.get("orateurs"))[:6] if _text(n)]}
    avantage = str(concl.get("avantage") or "").strip().lower()
    return {
        "question": _text(data.get("question"), 300),
        "these": {"texte": _text(these.get("texte"), 400), "force": _force(these.get("force"))},
        "pour": position(data.get("pour")), "contre": position(data.get("contre")),
        "arguments": arguments, "objections": objections, "refutations": refutations, "sophismes": sophismes,
        "phases": phases, "dynamique": dynamique,
        "hypotheses": {
            "explicites": assumptions(hyp.get("explicites")), "implicites": assumptions(hyp.get("implicites")),
            "risques": [{"texte": _text(r.get("texte") if isinstance(r, dict) else r), "concerne": refs(r.get("concerne")) if isinstance(r, dict) else []}
                        for r in _list(hyp.get("risques"))[:8] if _text(r.get("texte") if isinstance(r, dict) else r)],
        },
        "evaluation": {k: {"force": _force((ev.get(k) or {}).get("force") if isinstance(ev.get(k), dict) else None),
                           "commentaire": _text((ev.get(k) or {}).get("commentaire") if isinstance(ev.get(k), dict) else ev.get(k), 400)} for k in EVAL_KEYS},
        "conclusion": {"texte": _text(concl.get("texte"), 1200), "force": _force(concl.get("force")),
                       "avantage": avantage if avantage in ("pour", "contre") else "equilibre"},
    }


def build_debate_map(analysis, topic, engine, keys, log):
    """Arbre argumentatif : question → thèse → positions → arguments → objections → réfutations → évaluation"""
    log("Construction de l'arbre argumentatif...")
    carte = normalize_map(llm_json(engine, keys, MAP_PROMPT, map_input(analysis, topic), MAP_MAX_TOKENS))
    if not carte["sophismes"]:  # le modèle les a omis : on reprend ceux de l'analyse, rattachés à leur orateur
        carte["sophismes"] = [{
            "id": f"S{i}", "cible": "", "nom": _text(s.get("nom"), 80), "orateur": _text(o.get("nom"), 80), "extrait": _text(s.get("extrait"), 300),
            "explication": _text(s.get("explication"), 400), "gravite": "moyenne",
        } for i, (o, s) in enumerate(((o, s) for o in _list(analysis.get("orateurs")) if isinstance(o, dict)
                                      for s in _list(o.get("sophismes")) if isinstance(s, dict) and _text(s.get("nom"))), 1) if i <= 12]
    return carte


def analyze_blocks(blocks, engine, keys, topic, user_speakers, context, log, build_map=True):
    """Analyse une transcription longue partie par partie, puis fusionne et synthétise"""
    chunks = group_units(split_units(blocks, CHUNK_CHARS), CHUNK_CHARS)
    if not chunks:
        raise RuntimeError("Transcription vide : rien à analyser")
    total = len(chunks)
    speakers, summaries, failed = {}, [], []
    log(f"Analyse argumentative ({engine}) : {total} partie(s)")
    for i, chunk in enumerate(chunks):
        start = chunk[0][0]
        where = f"≈{fmt_time(start)}" if start is not None else f"partie {i + 1}/{total}"
        log(f"Analyse de la partie {i + 1}/{total} ({where})...")
        user = "\n".join([
            f"Sujet : {topic or 'non précisé'}",
            f"Noms proposés par l'utilisateur (peuvent être génériques) : {', '.join(user_speakers) or 'aucun'}",
            f"Orateurs déjà identifiés : {', '.join(s['nom'] for s in speakers.values()) or 'aucun'}",
            f"Contexte précédent : {(summaries[-1] if summaries else context) or 'début du débat'}",
            "",
            f"Extrait {i + 1}/{total}" + (f" (à partir de {fmt_time(start)})" if start is not None else "") + " :",
            "\n".join(text for _, text in chunk),
        ])
        try:
            data = llm_json(engine, keys, CHUNK_PROMPT, user)
        except FatalError:
            raise
        except Exception as e:
            failed.append(i + 1)
            log(f"⚠ Partie {i + 1}/{total} non analysée : {str(e)[:160]}")
            continue
        merge_chunk(speakers, data, where)
        if data.get("resume"):
            summaries.append(f"[{where}] {data['resume']}")
    if len(failed) == total:
        raise RuntimeError("Aucune partie n'a pu être analysée (quota ou modèle indisponible). Réessayez plus tard ou changez de moteur d'analyse.")

    orateurs = list(speakers.values())
    synthese = " ".join(s.split("] ", 1)[-1] for s in summaries)
    log("Synthèse finale...")
    bilan = [{
        "nom": o["nom"],
        "these_principale": o["these_principale"],
        "arguments": {level: sum(1 for a in o["arguments"] if a.get("solidite") == level) for level in ("forte", "moyenne", "faible")},
        "sophismes": [f.get("nom") for f in o["sophismes"]],
        "erreurs_logiques": [x.get("type") for x in o["incoherences"]],
        "faits_a_verifier": len(o["faits_a_verifier"]),
    } for o in orateurs]
    try:
        final = llm_json(engine, keys, SYNTHESIS_PROMPT, "\n".join([
            f"Sujet : {topic or 'non précisé'}", "", "Déroulé :", *summaries, "",
            "Bilan par orateur :", json.dumps(bilan, ensure_ascii=False),
        ]))
        synthese = str(final.get("synthese") or synthese)
        theses = {str(o.get("nom", "")).strip().lower(): o.get("these_principale")
                  for o in final.get("orateurs") or [] if isinstance(o, dict)}
        for o in orateurs:
            if theses.get(o["nom"].lower()):
                o["these_principale"] = str(theses[o["nom"].lower()])
    except Exception as e:
        log(f"⚠ Synthèse finale indisponible ({str(e)[:120]}) : résumés des parties utilisés")
    result = {"orateurs": orateurs, "synthese": synthese, "deroule": summaries, "parties": total, "parties_en_echec": failed}
    if build_map:
        try:
            result["carte"] = build_debate_map(result, topic, engine, keys, log)
        except JobCanceled:
            raise
        except Exception as e:
            log(f"⚠ Arbre argumentatif indisponible ({str(e)[:120]}) : il pourra être construit depuis l'onglet Structure")
    return result


def read_settings(payload, with_transcription, ctx, label):
    """Vérifie l'accès (compte, droits, quota) et récupère la configuration IA définie par l'administrateur"""
    headers, ip = ctx
    cfg = community.llm_access(headers, ip, "analyze", label, with_transcription)
    if not SIMULATION_MODE and (cfg["analysis_engine"] == "groq" or (with_transcription and cfg["transcribe_engine"].startswith("groq"))):
        try:
            check_groq_key(cfg["keys"]["groq"])
        except ValueError:
            raise community.ApiError(503, "Le modèle IA est indisponible (clé refusée) : prévenez l’administrateur.")
    raw_speakers = payload.get("speakers") if isinstance(payload.get("speakers"), list) else []
    return cfg | {
        "transcribe_engine": cfg["transcribe_engine"] if with_transcription else None,
        "topic": str(payload.get("topic") or "").strip()[:300],
        "speakers": [str(s).strip()[:80] for s in raw_speakers[:10] if str(s).strip()],
    }


def youtube_work(payload, ctx):
    """Tâche : YouTube → transcription → analyse"""
    url = ts.youtube_url(payload.get("url"))
    if not url:
        raise ValueError("Lien YouTube invalide : seules les vidéos YouTube peuvent être analysées.")
    cfg = read_settings(payload, True, ctx, "youtube")

    def work(log):
        tr = ts.transcribe_youtube(url, cfg["transcribe_engine"], cfg["keys"], log=log)
        blocks = tr["parts"]
        entries = [{"time": fmt_time(g[0][0]), "text": " ".join(text for _, text in g)}
                   for g in group_units(split_units(blocks, ENTRY_CHARS), ENTRY_CHARS)]
        analysis = analyze_blocks(blocks, cfg["analysis_engine"], cfg["keys"], cfg["topic"] or tr["title"],
                                  cfg["speakers"], "", log)
        return {"title": tr["title"], "entries": entries, "analysis": analysis,
                "failedSegments": tr["failedSegments"], "captionSegments": tr["captionSegments"]}
    return work


def text_work(payload, ctx):
    """Tâche : transcription déjà disponible (micro, collage, re-analyse) → analyse"""
    transcript = str(payload.get("transcript") or "")
    if not transcript.strip():
        raise ValueError("Transcription vide.")
    cfg = read_settings(payload, False, ctx, "text")
    context = str(payload.get("context") or "")[:2000]
    blocks = [{"start": None, "text": line} for line in transcript.splitlines() if line.strip()]
    build_map = not payload.get("skipMap")
    return lambda log: analyze_blocks(blocks, cfg["analysis_engine"], cfg["keys"], cfg["topic"],
                                      cfg["speakers"], context, log, build_map)


def map_work(payload, ctx):
    """Tâche : (re)construit l'arbre argumentatif d'un débat déjà analysé"""
    analysis = payload.get("analysis")
    if not isinstance(analysis, dict) or not _list(analysis.get("orateurs")):
        raise ValueError("Analyse absente : lancez d'abord l'analyse du débat.")
    cfg = read_settings(payload, False, ctx, "map")
    return lambda log: build_debate_map(analysis, cfg["topic"], cfg["analysis_engine"], cfg["keys"], log)


FILE_MAX_BYTES = 300 * 1024 * 1024


def file_work(path, title, cfg):
    """Tâche : fichier audio/vidéo envoyé → découpage → transcription (moteur de l'administrateur) → analyse"""
    def work(log):
        engine, keys = cfg["transcribe_engine"], cfg["keys"]
        try:
            with tempfile.TemporaryDirectory(prefix="rhetora-file-") as folder:
                log(f"[1/4] Fichier reçu ({os.path.getsize(path) / 1024 / 1024:.1f} Mo)")
                log(f"[2/4] Découpage en segments de {ts.SEGMENT_SECONDS // 60} min...")
                segments = ts.split_audio(path, folder)
                total = len(segments)
                workers = 1 if engine == "local" else ts.MAX_WORKERS
                log(f"[3/4] Transcription {engine} ({total} segment(s), {workers} en parallèle)...")
                texts, errors = [None] * total, {}

                def run(i):
                    try:
                        texts[i] = ts.transcribe_segment(engine, keys, segments[i], i, total, None)
                        errors.pop(i, None)
                        log(f"   ✓ Segment {i + 1}/{total} ({len(texts[i])} caractères)")
                    except JobCanceled:
                        raise
                    except Exception as e:
                        errors[i] = str(e)
                        log(f"   ✗ Segment {i + 1}/{total}: {str(e)[:120]}")

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(run, range(total)))
                if errors and engine != "local" and not any(re.search(r"HTTP 40[13]", e) for e in errors.values()):
                    log(f"      Nouvelle passe sur {len(errors)} segment(s) en échec dans {ts.ROUND_PAUSE}s...")
                    time.sleep(ts.ROUND_PAUSE)
                    for i in sorted(errors):
                        run(i)
                if len(errors) == total:
                    raise RuntimeError(next(iter(errors.values())))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        blocks = [{"start": i * ts.SEGMENT_SECONDS, "duration": ts.SEGMENT_SECONDS, "text": t}
                  for i, t in enumerate(texts) if i not in errors and t]
        if not blocks:
            raise RuntimeError("Aucune parole détectée dans le fichier.")
        log(f"[4/4] {'✓ Complet' if not errors else f'⚠ Partiel ({len(errors)} segment(s) manquant(s))'}")
        entries = [{"time": fmt_time(g[0][0]), "text": " ".join(text for _, text in g)}
                   for g in group_units(split_units(blocks, ENTRY_CHARS), ENTRY_CHARS)]
        analysis = analyze_blocks(blocks, cfg["analysis_engine"], keys, cfg["topic"] or title, cfg["speakers"], "", log)
        return {"title": title, "entries": entries, "analysis": analysis,
                "failedSegments": [i + 1 for i in sorted(errors)], "captionSegments": []}
    return work


def hook_chat(cfg, system, user, max_tokens):
    return llm_json(cfg["analysis_engine"], cfg["keys"], system, user, max_tokens)


def hook_test(cfg):
    """Test du modèle configuré par l'administrateur : modèles disponibles + court échange"""
    started, models = time.time(), []
    if cfg["analysis_engine"] == "groq":
        req = urllib.request.Request("https://api.groq.com/openai/v1/models", headers={
            "Authorization": f"Bearer {cfg['keys']['groq']}", "User-Agent": "arbitre/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8")).get("data") or []
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Groq HTTP {e.code} : {http_error_message(e)}")
        models = sorted(str(m.get("id")) for m in data if m.get("id") and not re.search(r"whisper|tts|guard|orpheus", str(m.get("id")), re.I))
    reply = llm_json(cfg["analysis_engine"], cfg["keys"], 'Réponds uniquement en JSON : {"message":"string"}',
                     "Dis bonjour en cinq mots maximum.", 300)
    return {"ok": True, "engine": cfg["analysis_engine"], "models": models, "latency": round(time.time() - started, 1),
            "reply": str(reply.get("message") or "")[:200]}


jobs = {}
jobs_lock = threading.Lock()
job_slots = threading.BoundedSemaphore(MAX_RUNNING_JOBS)


class JobCanceled(Exception):
    pass


def public_error(error):
    """Message d'erreur montré à l'utilisateur : première ligne, sans chemins de fichiers internes"""
    text = str(error).strip().splitlines()[0] if str(error).strip() else "Erreur inattendue."
    return PATH_RE.sub("[fichier]", text)[:300]


def job_owner(headers, ip):
    """Propriétaire d'une tâche : le compte connecté, sinon l'adresse (si l'IA est ouverte sans compte)"""
    uid = community.session_user_id(headers)
    return f"user:{uid}" if uid else f"ip:{ip}"


def ensure_job_capacity(owner):
    with jobs_lock:
        active = [j for j in jobs.values() if j["status"] in ("queued", "running")]
    if len(active) >= MAX_ACTIVE_JOBS:
        raise community.ApiError(503, "Le serveur traite déjà beaucoup d’analyses : réessayez dans quelques minutes.")
    if sum(j["owner"] == owner for j in active) >= MAX_JOBS_PER_OWNER:
        raise community.ApiError(429, f"{MAX_JOBS_PER_OWNER} analyses sont déjà en cours pour vous : attendez qu’une se termine.")


def start_job(work, owner):
    """Exécute work(log) en arrière-plan ; la progression n'est consultable que par son propriétaire (GET /api/jobs/<id>)"""
    job_id = uuid.uuid4().hex
    job = {"status": "queued", "log": [], "result": None, "error": None, "created": time.time(), "cancel": False, "owner": owner}

    def log(message):
        if job["cancel"]:
            raise JobCanceled("Analyse annulée.")  # interrompt le travail au prochain point d'étape
        message = str(message).strip()
        print(f"[{job_id[:8]}] {message}")
        with jobs_lock:
            job["log"].append(message)
            del job["log"][:-100]

    def runner():
        try:
            if not job_slots.acquire(blocking=False):
                log(f"En attente d’une place : {MAX_RUNNING_JOBS} analyses sont déjà en cours sur le serveur…")
                while not job_slots.acquire(timeout=1):
                    if job["cancel"]:
                        raise JobCanceled("Analyse annulée.")
        except JobCanceled:
            with jobs_lock:
                job.update(status="canceled", error="Analyse annulée.")
            return
        try:
            with jobs_lock:
                job["status"] = "running"
            result = work(log)
            log("✓ Terminé")
            with jobs_lock:
                job.update(status="done", result=result)
        except Exception as e:
            if job["cancel"]:
                print(f"[{job_id[:8]}] ✗ Annulée")
                with jobs_lock:
                    job.update(status="canceled", error="Analyse annulée.")
                return
            traceback.print_exc()
            message = public_error(e)
            with jobs_lock:
                job["log"].append(f"✗ {message}")
                job.update(status="error", error=message)
        finally:
            job_slots.release()

    with jobs_lock:
        for old in [k for k, j in jobs.items() if time.time() - j["created"] > JOB_TTL]:
            del jobs[old]
        jobs[job_id] = job
    threading.Thread(target=runner, daemon=True).start()
    return job_id


def find_job(job_id, owner):
    """Tâche de ce propriétaire, sinon None : l'identifiant seul ne suffit pas à lire ou annuler l'analyse d'autrui"""
    job = jobs.get(job_id)
    return job if job and hmac.compare_digest(job["owner"], owner) else None

explore_cache = {}
explore_lock = threading.Lock()


def explore_page(query, sort, yt_duration, page):
    """Une page brute de résultats YouTube (mise en cache) : (vidéos, page pleine ?)"""
    import yt_dlp
    key = (query.lower(), sort, yt_duration, page)
    with explore_lock:
        cached = explore_cache.get(key)
        if cached and time.time() - cached[0] < EXPLORE_TTL:
            return cached[1]
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
    if EXPLORE_FILTERS[(sort, yt_duration)]:
        url += f"&sp={EXPLORE_FILTERS[(sort, yt_duration)]}"
    opts = {**ts.YDL_BASE, "extract_flat": True, "skip_download": True,
            "playliststart": (page - 1) * EXPLORE_PAGE + 1, "playlistend": page * EXPLORE_PAGE}
    opts.pop("noplaylist", None)
    with yt_dlp.YoutubeDL(opts) as dl:
        info = dl.extract_info(url, download=False) or {}
    raw = list(info.get("entries") or [])
    videos = []
    for e in raw:
        vid = str(e.get("id") or "")
        if e.get("ie_key") != "Youtube" or not re.fullmatch(r"[\w-]{11}", vid) or "/shorts/" in str(e.get("url") or ""):
            continue
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        videos.append({
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": str(e.get("title") or ""),
            "channel": str(e.get("channel") or e.get("uploader") or ""),
            "channelUrl": channel_url(e.get("channel_url") or e.get("uploader_url")),
            "duration": e.get("duration"),
            "views": e.get("view_count"),
            "description": str(e.get("description") or "")[:300],
            "verified": bool(e.get("channel_is_verified")),
        })
    result = (videos, len(raw) >= EXPLORE_PAGE)
    with explore_lock:
        for old in [k for k, (t, _) in explore_cache.items() if time.time() - t > EXPLORE_TTL]:
            del explore_cache[old]
        while len(explore_cache) >= EXPLORE_CACHE_MAX:
            del explore_cache[min(explore_cache, key=lambda k: explore_cache[k][0])]
        explore_cache[key] = (time.time(), result)
    return result


def channel_url(value):
    """Lien de chaîne affiché tel quel dans la page : uniquement https://www.youtube.com/… (jamais javascript: ni autre site)"""
    url = str(value or "")
    return url if re.fullmatch(r"https://(www\.)?youtube\.com/[\w@%./-]{1,200}", url) else ""


def explore_youtube(params):
    """Recherche de vidéos YouTube (sans clé API) : métadonnées uniquement, via yt-dlp"""
    query = " ".join(str(params.get("q", [""])[0]).split())[:150]
    if not query:
        raise ValueError("Saisissez une recherche.")
    sort = params.get("sort", ["relevance"])[0]
    duration = params.get("duration", ["long"])[0]
    if duration not in EXPLORE_DURATIONS or (sort, "any") not in EXPLORE_FILTERS:
        raise ValueError("Filtre de recherche invalide.")
    yt_duration, min_s, max_s = EXPLORE_DURATIONS[duration]
    try:
        page = min(max(int(params.get("page", ["1"])[0]), 1), EXPLORE_MAX_PAGE)
    except ValueError:
        raise ValueError("Page invalide.")

    def fits(v):
        if not min_s and max_s is None:
            return True
        d = v["duration"]
        return isinstance(d, (int, float)) and d >= min_s and (max_s is None or d <= max_s)

    # Avec une borne locale, on enchaîne les pages YouTube jusqu'à avoir assez de vidéos qui correspondent
    videos, seen, full = [], set(), True
    while True:
        batch, full = explore_page(query, sort, yt_duration, page)
        for v in batch:
            if fits(v) and v["id"] not in seen:
                seen.add(v["id"])
                videos.append(v)
        page += 1
        if not full or page > EXPLORE_MAX_PAGE or len(videos) >= 12 or (min_s == 0 and max_s is None):
            break
    has_more = full and page <= EXPLORE_MAX_PAGE
    return {"query": query, "videos": videos, "hasMore": has_more, "nextPage": page if has_more else None}


def youtube_oembed(params):
    """Titre et chaîne d'une vidéo (oEmbed YouTube, instantané, sans clé)"""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", str(params.get("url", [""])[0])[:500])
    if not m:
        raise ValueError("Lien YouTube invalide.")
    video = urllib.parse.quote(f"https://www.youtube.com/watch?v={m.group(1)}", safe="")
    with urllib.request.urlopen(f"https://www.youtube.com/oembed?format=json&url={video}", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return {"title": str(data.get("title") or ""), "channel": str(data.get("author_name") or "")}


LIVE_MAX_BYTES = 15 * 1024 * 1024  # un segment de 30 s en Opus pèse ~150 Ko
# Phrases que Whisper invente sur du silence ou du bruit (génériques de sous-titres YouTube)
LIVE_HALLUCINATION = re.compile(
    r"sous-titr|amara\.org|merci d'avoir regard|abonnez-vous|thanks? (you )?for watching|subtitles? by|^\W*(merci|thank you)\W*$",
    re.I,
)


def transcribe_live(headers, audio, ip):
    """Transcrit un segment enregistré au micro (WebM/Ogg/MP4) → {text} avec le moteur choisi par l'administrateur"""
    cfg = community.llm_access(headers, ip, "transcribe", "live")
    engine, keys = cfg["transcribe_engine"], cfg["keys"]
    lang = (headers.get("X-Lang") or "").strip().lower()
    lang = lang if re.fullmatch(r"[a-z]{2}", lang) else None
    if not audio:
        raise ValueError("Segment audio vide.")
    with tempfile.TemporaryDirectory(prefix="ergo-live-") as tmp:
        src, dst = os.path.join(tmp, "chunk.bin"), os.path.join(tmp, "chunk.mp3")
        with open(src, "wb") as f:
            f.write(audio)
        proc = subprocess.run(
            [ts.get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", dst],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode or not os.path.exists(dst):
            raise ValueError(f"Segment audio illisible : {(proc.stderr or '').strip()[-200:]}")
        text = ts.transcribe_segment(engine, keys, dst, 0, 1, lang)
    text = " ".join(text.split())
    if not re.search(r"\w", text) or (len(text) < 120 and LIVE_HALLUCINATION.search(text)):
        text = ""
    return {"text": text}


class Handler(SimpleHTTPRequestHandler):
    routes = {"/api/analyze-youtube": youtube_work, "/api/analyze-text": text_work, "/api/debate-map": map_work}

    def send_json(self, status, data, headers=()):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        page = self.__dict__.pop("html_page", None)
        if page:
            self.send_header("Content-Security-Policy", page_csp(page))
            self.send_header("Permissions-Policy", PERMISSIONS_POLICY)
        super().end_headers()

    def community_api(self, method):
        """Routes comptes / communauté / administration. Renvoie True si la requête a été traitée."""
        parsed = urllib.parse.urlsplit(self.path)
        if not community.handles(parsed.path):
            return False
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= community.MAX_BODY:
            self.send_json(413, {"error": "Requête trop volumineuse."})
            return True
        body = self.rfile.read(length) if length else b""
        client_ip = effective_client_ip(self.client_address[0], self.headers, SIMULATION_MODE)
        status, data, headers = community.handle(method, parsed.path, parsed.query, self.headers, body, client_ip)
        if isinstance(data, community.Binary):  # image : identifiant aléatoire immuable, servie sans exécution possible
            self.send_response(status)
            self.send_header("Content-Type", data.mime)
            self.send_header("Content-Length", str(len(data.data)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.end_headers()
            self.wfile.write(data.data)
        elif data is None:
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_json(status, data, headers)
        return True

    def static_allowed(self):
        path = posixpath.normpath(urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).replace("\\", "/"))
        if path in ("/", "."):
            self.send_response(302)
            self.send_header("Location", "/arbitre.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if not STATIC_RE.match(path) or path not in STATIC_PAGES:
            self.send_error(404)
            return False
        self.html_page = path
        return True

    def do_HEAD(self):
        if self.static_allowed():
            super().do_HEAD()

    def do_PUT(self):
        if not self.community_api("PUT"):
            self.send_error(404)

    def do_PATCH(self):
        if not self.community_api("PATCH"):
            self.send_error(404)

    def do_GET(self):
        if self.community_api("GET"):
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/simulation/state":
            if not self.simulation_is_local():
                self.send_json(403, {"error": "Console réservée à cette machine."})
                return
            self.send_json(200, self.simulation_state())
            return
        if parsed.path == "/api/simulation/report":
            if not self.simulation_is_local():
                self.send_json(403, {"error": "Console réservée à cette machine."})
                return
            self.send_simulation_report()
            return
        if parsed.path == "/api/simulation/activity":
            if not self.simulation_is_local():
                self.send_json(403, {"error": "Console réservée à cette machine."})
                return
            self.send_simulation_activity(parsed.query)
            return
        if parsed.path == "/api/simulation/actors":
            if not self.simulation_is_local():
                self.send_json(403, {"error": "Console réservée à cette machine."})
                return
            self.send_simulation_actors(parsed.query)
            return
        api = {"/api/explore": explore_youtube, "/api/oembed": youtube_oembed}.get(parsed.path)
        if api:
            try:
                limit = (30, "explore") if parsed.path == "/api/explore" else (120, "oembed")
                client_ip = effective_client_ip(self.client_address[0], self.headers, SIMULATION_MODE)
                community._limit(f"yt-{limit[1]}:{client_ip}", limit[0], 60, "Trop de recherches : patientez un instant.")
                self.send_json(200, api(urllib.parse.parse_qs(parsed.query)))
            except community.ApiError as e:
                self.send_json(e.status, {"error": str(e)})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception:
                traceback.print_exc()
                self.send_json(502, {"error": "Requête YouTube impossible pour le moment : réessayez plus tard."})
            return
        if not self.path.startswith("/api/jobs/"):
            if self.static_allowed():
                super().do_GET()
            return
        client_ip = effective_client_ip(self.client_address[0], self.headers, SIMULATION_MODE)
        owner = job_owner(self.headers, client_ip)
        with jobs_lock:
            job = find_job(self.path[len("/api/jobs/"):], owner)
            snapshot = job and {k: job[k] for k in ("status", "result", "error")} | {"log": job["log"][-20:]}
        if snapshot:
            if snapshot["status"] == "queued":
                snapshot["status"] = "running"  # côté navigateur, une tâche en attente se suit comme une tâche en cours
            self.send_json(200, snapshot)
        else:
            self.send_json(404, {"error": "Tâche inconnue ou expirée (serveur redémarré ?)."})

    def do_POST(self):
        simulation_path = urllib.parse.urlsplit(self.path).path
        if simulation_path == "/api/simulation/run":
            self.start_simulation()
            return
        if simulation_path == "/api/simulation/stop":
            self.stop_simulation()
            return
        if simulation_path == "/api/simulation/view-as":
            self.simulation_view_as()
            return
        if self.community_api("POST"):
            return
        client_ip = effective_client_ip(self.client_address[0], self.headers, SIMULATION_MODE)
        ctx = (self.headers, client_ip)
        if self.path == "/api/analyze-file":
            self.analyze_file(ctx)
            return
        if self.path == "/api/transcribe":
            try:
                if self.headers.get("X-Rhetora") != "1":
                    raise community.ApiError(403, "Requête refusée.")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= LIVE_MAX_BYTES:
                    raise ValueError("Segment audio vide ou trop volumineux.")
                audio = self.rfile.read(length)
                self.send_json(200, transcribe_live(self.headers, audio, ctx[1]))
            except community.ApiError as e:
                self.send_json(e.status, {"error": str(e)})
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self.send_json(502, {"error": public_error(e)})
            return
        make_work = self.routes.get(self.path)
        if not make_work:
            self.send_error(404)
            return
        try:
            if self.headers.get("X-Rhetora") != "1":
                raise community.ApiError(403, "Requête refusée.")
            owner = job_owner(self.headers, ctx[1])
            ensure_job_capacity(owner)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 20 * 1024 * 1024:
                raise ValueError("Requête trop volumineuse.")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Requête invalide.")
            work = make_work(payload, ctx)
        except community.ApiError as e:
            self.send_json(e.status, {"error": str(e)})
            return
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
            return
        self.send_json(202, {"jobId": start_job(work, owner)})

    def simulation_is_local(self):
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def simulation_state(self):
        try:
            with open(SIMULATION_STATUS_FILE, encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, ValueError):
            state = {"status": "idle", "phase": "Prêt à démarrer", "request_count": 0,
                     "actions": {}, "recent_events": [], "errors": []}
        with simulation_lock:
            process = simulation_process
            if (state.get("sandbox") or {}).get("open") and (process is None or process.poll() is not None):
                state["sandbox"]["open"] = False
            cancel_requested = state.get("status") == "running" and os.path.isfile(SIMULATION_CANCEL_FILE)
            if cancel_requested:
                state["cancel_requested"] = True
                state["phase"] = "Arrêt demandé · fin de l’action en cours"
            interrupted = state.get("status") == "running" and (process is None or process.poll() is not None)
            if interrupted:
                state["status"] = "failed"
                state["phase"] = "La simulation a été interrompue avant son rapport final"
                try:
                    with open(SIMULATION_LOG_FILE, encoding="utf-8", errors="replace") as log_file:
                        detail = log_file.read()[-1200:].strip()
                except OSError:
                    detail = ""
                return_code = process.returncode if process else "serveur redémarré"
                state["errors"] = state.get("errors", []) + [{
                    "action": "simulation",
                    "status": return_code,
                    "message": detail or "Aucun processus de simulation actif n'est associé à cet état.",
                }]
                temporary = SIMULATION_STATUS_FILE + ".tmp"
                with open(temporary, "w", encoding="utf-8") as state_file:
                    json.dump(state, state_file)
                os.replace(temporary, SIMULATION_STATUS_FILE)
        return state

    def send_simulation_report(self):
        state = self.simulation_state()
        report_path = os.path.realpath(state.get("report_path") or "")
        try:
            inside_results = os.path.commonpath((os.path.realpath(SIMULATION_DIR), report_path)) == os.path.realpath(SIMULATION_DIR)
        except ValueError:
            inside_results = False
        if not inside_results or not os.path.isfile(report_path):
            self.send_json(404, {"error": "Aucun rapport terminé pour le moment."})
            return
        try:
            with open(report_path, encoding="utf-8") as report_file:
                report = json.load(report_file)
        except (OSError, ValueError):
            self.send_json(500, {"error": "Le rapport est illisible."})
            return
        self.send_json(200, report)

    def send_simulation_activity(self, query):
        params = urllib.parse.parse_qs(query)
        actor = params.get("actor", [""])[0][:120]
        kind = params.get("kind", [""])[0][:30]
        try:
            limit = min(300, max(1, int(params.get("limit", ["150"])[0])))
        except ValueError:
            limit = 150
        events, total = deque(maxlen=limit), 0
        try:
            with open(SIMULATION_ACTIVITY_FILE, encoding="utf-8") as journal:
                for line in journal:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if actor and actor not in (event.get("username"), event.get("peer")):
                        continue
                    if kind and event.get("kind") != kind:
                        continue
                    total += 1
                    events.append(event)
        except FileNotFoundError:
            pass
        self.send_json(200, {"events": list(events)[::-1], "total": total})

    def send_simulation_actors(self, query):
        params = urllib.parse.parse_qs(query)
        needle = params.get("q", [""])[0][:80].casefold()
        try:
            limit = min(200, max(1, int(params.get("limit", ["100"])[0])))
        except ValueError:
            limit = 100
        try:
            with open(SIMULATION_ACTORS_FILE, encoding="utf-8") as actors_file:
                actors = json.load(actors_file)
        except (OSError, ValueError):
            actors = []
        matched = [actor for actor in actors if not needle or needle in " ".join(
            str(actor.get(key, "")) for key in ("username", "persona", "status", "joined", "last_action")).casefold()]
        matched.sort(key=lambda actor: (actor.get("persona") != "company", actor.get("status") != "En ligne", -actor.get("actions", 0)))
        self.send_json(200, {"actors": matched[:limit], "matched": len(matched), "total": len(actors)})

    def simulation_view_as(self):
        """Sandbox uniquement : ouvre une session navigateur pour un compte simulé, avec la clé propre à l'exécution."""
        if not (SIMULATION_VIEW_KEY and self.simulation_is_local()):
            self.send_json(403, {"error": "Consultation réservée à la sandbox locale."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= 2048:
            self.send_json(400, {"error": "Requête invalide."})
            return
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        key, username = form.get("key", [""])[0], form.get("user", [""])[0][:40]
        target = form.get("next", [""])[0]
        if not hmac.compare_digest(key.encode(), SIMULATION_VIEW_KEY.encode()):
            self.send_json(403, {"error": "Clé de consultation invalide."})
            return
        if not SIMULATION_VIEW_TARGET_RE.fullmatch(target):
            target = "#/communaute"
        with community.tx() as conn:
            row = conn.execute("SELECT id FROM users WHERE username = ? AND status = 'active'", (username,)).fetchone()
            if not row:
                self.send_json(404, {"error": "Compte simulé introuvable."})
                return
            client_ip = effective_client_ip(self.client_address[0], self.headers, SIMULATION_MODE)
            req = community.Req(conn, "", self.headers, {}, client_ip)
            community._start_session(req, row["id"])
        self.send_response(303)
        for header, value in req.out_headers:
            self.send_header(header, value)
        self.send_header("Location", "/arbitre.html" + target)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def start_simulation(self):
        global simulation_process
        if SIMULATION_MODE or not self.simulation_is_local():
            self.send_json(403, {"error": "Console réservée à cette machine."})
            return
        origin = self.headers.get("Origin")
        if self.headers.get("X-Rhetora") != "1" or not origin or urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.send_json(403, {"error": "Origine de la requête refusée."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 4096:
                raise ValueError("Requête invalide.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Requête invalide.")
            users, concurrency = payload.get("users"), payload.get("concurrency")
            duration_seconds, mode = payload.get("duration_seconds"), payload.get("mode")
            seed, pace = payload.get("seed", 42), payload.get("pace", "realistic")
            ramp_seconds, p95_budget_ms = payload.get("ramp_seconds", 10), payload.get("p95_budget_ms", 2000)
            keep_open_seconds = payload.get("keep_open_seconds", 0)
            signups_per_minute = payload.get("signups_per_minute", 0)
            visit_percent = payload.get("visit_percent", 100)
            if type(visit_percent) is not int or not 1 <= visit_percent <= 100:
                raise ValueError("La part des comptes qui se connectent doit être entre 1 et 100 %.")
            if type(users) is not int or not 1 <= users <= 10000:
                raise ValueError("Le nombre d'acteurs doit être entre 1 et 10000.")
            if type(concurrency) is not int or not 1 <= concurrency <= min(users, 100):
                raise ValueError("La concurrence doit être entre 1 et 100 et ne pas dépasser le nombre d'acteurs.")
            if type(duration_seconds) is not int or not 30 <= duration_seconds <= 1200:
                raise ValueError("La durée doit être entre 30 secondes et 20 minutes.")
            if mode not in ("balanced", "complete", "community", "messages", "reader", "creator"):
                raise ValueError("Profil de simulation inconnu.")
            if type(signups_per_minute) is not int or not 0 <= signups_per_minute <= 600:
                raise ValueError("Les nouvelles inscriptions doivent être entre 0 et 600 par minute.")
            if type(seed) is not int or not 0 <= seed <= 2147483647:
                raise ValueError("La graine doit être un entier entre 0 et 2147483647.")
            if pace not in ("realistic", "load"):
                raise ValueError("Rythme inconnu.")
            if type(ramp_seconds) is not int or not 0 <= ramp_seconds <= 300:
                raise ValueError("La montée en charge doit être entre 0 et 300 secondes.")
            if type(p95_budget_ms) is not int or not 1 <= p95_budget_ms <= 60000:
                raise ValueError("Le budget p95 doit être entre 1 et 60000 millisecondes.")
            if type(keep_open_seconds) is not int or not 0 <= keep_open_seconds <= 3600:
                raise ValueError("La sandbox peut rester ouverte de 0 à 3600 secondes.")
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error) or "Requête invalide."})
            return

        with simulation_lock:
            if simulation_process and simulation_process.poll() is None:
                try:
                    with open(SIMULATION_STATUS_FILE, encoding="utf-8") as state_file:
                        previous_status = json.load(state_file).get("status")
                except (OSError, ValueError):
                    previous_status = "running"
                if previous_status == "running":
                    self.send_json(409, {"error": "Une simulation est déjà en cours."})
                    return
                os.makedirs(SIMULATION_DIR, exist_ok=True)
                with open(SIMULATION_CANCEL_FILE, "w", encoding="utf-8") as cancel_file:
                    cancel_file.write("close\n")
                try:
                    simulation_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.send_json(409, {"error": "La sandbox précédente se ferme encore; réessayez."})
                    return
            os.makedirs(SIMULATION_DIR, exist_ok=True)
            for stale in (SIMULATION_CANCEL_FILE, SIMULATION_ACTIVITY_FILE, SIMULATION_ACTORS_FILE):
                try:
                    os.remove(stale)
                except FileNotFoundError:
                    pass
            initial = {"status": "running", "phase": "Démarrage du serveur isolé", "request_count": 0,
                       "configuration": {"users": users, "concurrency": concurrency,
                                         "duration_seconds": duration_seconds, "mode": mode,
                                         "seed": seed, "pace": pace, "ramp_seconds": ramp_seconds,
                                         "p95_budget_ms": p95_budget_ms, "keep_open_seconds": keep_open_seconds,
                                         "signups_per_minute": signups_per_minute, "visit_percent": visit_percent},
                       "actions": {}, "recent_events": [], "errors": [], "updated_at": time.time()}
            temporary = SIMULATION_STATUS_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as state_file:
                json.dump(initial, state_file)
            os.replace(temporary, SIMULATION_STATUS_FILE)
            environment = os.environ.copy()
            for key in list(environment):
                upper = key.upper()
                if "API_KEY" in upper or "APIKEY" in upper or "SECRET" in upper or "PASSWORD" in upper:
                    environment.pop(key, None)
            try:
                with open(SIMULATION_LOG_FILE, "w", encoding="utf-8") as simulation_log:
                    simulation_process = subprocess.Popen(
                        [sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulate.py"),
                         "--users", str(users), "--concurrency", str(concurrency), "--output", SIMULATION_DIR,
                         "--duration-seconds", str(duration_seconds), "--mode", mode,
                         "--seed", str(seed), "--pace", pace, "--ramp-seconds", str(ramp_seconds),
                         "--p95-budget-ms", str(p95_budget_ms), "--keep-open-seconds", str(keep_open_seconds),
                         "--signups-per-minute", str(signups_per_minute), "--visit-percent", str(visit_percent),
                         "--status-file", SIMULATION_STATUS_FILE, "--cancel-file", SIMULATION_CANCEL_FILE],
                        cwd=os.path.dirname(os.path.abspath(__file__)),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=simulation_log,
                        stderr=subprocess.STDOUT,
                    )
            except OSError as error:
                initial.update(status="failed", phase="Impossible de lancer le scénario")
                initial["errors"] = [{"action": "simulation", "status": 0, "message": str(error)[:240]}]
                with open(temporary, "w", encoding="utf-8") as state_file:
                    json.dump(initial, state_file)
                os.replace(temporary, SIMULATION_STATUS_FILE)
                self.send_json(500, {"error": "Le processus de simulation n'a pas pu démarrer."})
                return
        self.send_json(202, {"status": "started", "users": users, "concurrency": concurrency})

    def stop_simulation(self):
        if not self.simulation_is_local():
            self.send_json(403, {"error": "Console réservée à cette machine."})
            return
        origin = self.headers.get("Origin")
        if self.headers.get("X-Rhetora") != "1" or not origin or urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            self.send_json(403, {"error": "Origine de la requête refusée."})
            return
        with simulation_lock:
            if not simulation_process or simulation_process.poll() is not None:
                self.send_json(409, {"error": "Aucune simulation active à arrêter."})
                return
            os.makedirs(SIMULATION_DIR, exist_ok=True)
            with open(SIMULATION_CANCEL_FILE, "w", encoding="utf-8") as cancel_file:
                cancel_file.write("stop\n")
        self.send_json(202, {"status": "stopping"})

    def analyze_file(self, ctx):
        """Reçoit un fichier audio/vidéo (corps brut, en flux vers un fichier temporaire) et lance son analyse"""
        path = None
        try:
            if self.headers.get("X-Rhetora") != "1":
                raise community.ApiError(403, "Requête refusée.")
            owner = job_owner(self.headers, ctx[1])
            ensure_job_capacity(owner)
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= FILE_MAX_BYTES:
                raise ValueError(f"Fichier vide ou trop volumineux ({FILE_MAX_BYTES // 1024 // 1024} Mo maximum).")
            title = " ".join(urllib.parse.unquote(self.headers.get("X-Filename") or "").split())[:200] or "Fichier audio"
            title = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", title) or title
            topic = " ".join(urllib.parse.unquote(self.headers.get("X-Topic") or "").split())[:300]
            cfg = read_settings({"topic": topic}, True, ctx, "file")
            fd, path = tempfile.mkstemp(prefix="rhetora-upload-")
            remaining = length
            with os.fdopen(fd, "wb") as f:
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                raise ValueError("Envoi du fichier interrompu.")
        except (community.ApiError, ValueError) as e:
            if path:
                os.remove(path)
            self.send_json(getattr(e, "status", 400), {"error": str(e)})
            return
        self.send_json(202, {"jobId": start_job(file_work(path, title, cfg), owner)})

    def do_DELETE(self):
        if self.community_api("DELETE"):
            return
        if not self.path.startswith("/api/jobs/"):
            self.send_error(404)
            return
        if self.headers.get("X-Rhetora") != "1":
            self.send_json(403, {"error": "Requête refusée."})
            return
        client_ip = effective_client_ip(self.client_address[0], self.headers, SIMULATION_MODE)
        owner = job_owner(self.headers, client_ip)
        with jobs_lock:
            job = find_job(self.path[len("/api/jobs/"):], owner)
            if job and job["status"] in ("queued", "running"):
                job["cancel"] = True
        self.send_json(200 if job else 404, {"canceled": bool(job)})

    def log_message(self, format, *args):
        pass  # Pas de logs HTTP (polling)


class RhetoraHTTPServer(ThreadingHTTPServer):
    # La file par défaut (5) refuse des connexions lors des pics de requêtes simultanées.
    request_queue_size = 256


def page_csp(page):
    """CSP de la page, avec l'empreinte SHA-256 de ses scripts inline (recalculée si le fichier change)"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), page.lstrip("/"))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return CSP_BASE
    cached = _csp_cache.get(page)
    if not cached or cached[0] != mtime:
        with open(path, "rb") as f:
            # Le navigateur convertit les fins de ligne Windows avant de calculer l'empreinte du script.
            hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(s.replace(b"\r\n", b"\n").replace(b"\r", b"\n")).digest()).decode()
                              for s in INLINE_SCRIPT_RE.findall(f.read()))
        policy = CSP_PAGES.get(page, "script-src 'self'").format(hashes=hashes)
        cached = _csp_cache[page] = (mtime, f"{CSP_BASE}; {policy}")
    return cached[1]

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if SIMULATION_MODE and os.environ.get("ARBITRE_HOST", "") not in ("", "127.0.0.1"):
        raise SystemExit("Le mode simulation ne peut écouter que sur 127.0.0.1.")
    community.init()
    community.HOOKS.update(chat=hook_chat, test=hook_test)
    print(f"http://localhost:{PORT}/arbitre.html")
    host = os.environ.get("ARBITRE_HOST", "")  # ex. IP Wi-Fi pour un accès depuis le téléphone (en plus de localhost)
    if host and host != "127.0.0.1":
        lan = RhetoraHTTPServer((host, PORT), Handler)
        threading.Thread(target=lan.serve_forever, daemon=True).start()
        print(f"Réseau local : http://{host}:{PORT}/arbitre.html")
        if not community.SECURE_COOKIE:
            print("⚠ Accès réseau en HTTP : les sessions circulent en clair. Placez un proxy HTTPS devant le serveur "
                  "et définissez RHETORA_SECURE_COOKIE=1.")
    RhetoraHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
