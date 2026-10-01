#!/usr/bin/env python3
"""Arbitre : vidéo YouTube (même longue) → transcription par segments → analyse argumentative par parties
(arguments, sophismes, erreurs logiques, faits à vérifier) → fusion par orateur et synthèse finale."""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import transcript_server as ts  # téléchargement/transcription, TLS via proxy (truststore), suivi des quotas

PORT = 8766
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
                             "Elle a peut-être été révoquée ou mal copiée : recréez-en une sur console.groq.com/keys, "
                             "collez-la dans Configuration puis Enregistrer.")
    except (urllib.error.URLError, TimeoutError):
        pass  # réseau indisponible : l'erreur réelle apparaîtra pendant la tâche


def groq_chat(api_key, system, user):
    """Chat completion Groq (API compatible OpenAI) en mode JSON, avec repli entre modèles"""
    last_error = "quota épuisé sur tous les modèles Groq"
    for model in GROQ_CHAT_MODELS:
        if ts.is_exhausted(model):
            continue
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": 4096,
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


def llm_json(engine, keys, system, user):
    """Appelle le moteur d'analyse et retourne l'objet JSON produit (2 essais si la réponse est invalide)"""
    problem = None
    for _ in range(2):
        raw = groq_chat(keys["groq"], system, user) if engine == "groq" else gemini_chat(keys["gemini"], system, user)
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


def analyze_blocks(blocks, engine, keys, topic, user_speakers, context, log):
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
    return {"orateurs": orateurs, "synthese": synthese, "deroule": summaries, "parties": total, "parties_en_echec": failed}


def read_settings(payload, with_transcription):
    """Valide moteurs et clés avant de lancer une tâche"""
    keys = {"gemini": str(payload.get("apiKey") or "").strip(), "groq": str(payload.get("groqKey") or "").strip()}
    analysis_engine = payload.get("analysisEngine") or ("groq" if keys["groq"] else "gemini")
    if analysis_engine not in ANALYSIS_ENGINES:
        raise ValueError(f"Moteur d'analyse inconnu : {analysis_engine}")
    needed = {analysis_engine}
    transcribe_engine = None
    if with_transcription:
        transcribe_engine = payload.get("transcribeEngine") or "groq-turbo"
        if transcribe_engine not in ts.ENGINES:
            raise ValueError(f"Moteur de transcription inconnu : {transcribe_engine}")
        if transcribe_engine.startswith("groq"):
            needed.add("groq")
        elif transcribe_engine == "gemini":
            needed.add("gemini")
    labels = {"groq": "clé API Groq", "gemini": "clé API Google AI"}
    missing = [labels[k] for k in sorted(needed) if not keys[k]]
    if missing:
        raise ValueError(f"Il manque : {', '.join(missing)} (voir Configuration).")
    if "groq" in needed:
        check_groq_key(keys["groq"])
    raw_speakers = payload.get("speakers") if isinstance(payload.get("speakers"), list) else []
    return {
        "keys": keys,
        "analysis_engine": analysis_engine,
        "transcribe_engine": transcribe_engine,
        "topic": str(payload.get("topic") or "").strip()[:300],
        "speakers": [str(s).strip()[:80] for s in raw_speakers[:10] if str(s).strip()],
    }


def youtube_work(payload):
    """Tâche : YouTube → transcription → analyse"""
    url = str(payload.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("URL YouTube invalide.")
    cfg = read_settings(payload, with_transcription=True)

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


def text_work(payload):
    """Tâche : transcription déjà disponible (micro, collage, re-analyse) → analyse"""
    transcript = str(payload.get("transcript") or "")
    if not transcript.strip():
        raise ValueError("Transcription vide.")
    cfg = read_settings(payload, with_transcription=False)
    context = str(payload.get("context") or "")[:2000]
    blocks = [{"start": None, "text": line} for line in transcript.splitlines() if line.strip()]
    return lambda log: analyze_blocks(blocks, cfg["analysis_engine"], cfg["keys"], cfg["topic"],
                                      cfg["speakers"], context, log)


jobs = {}
jobs_lock = threading.Lock()


class JobCanceled(Exception):
    pass


def start_job(work):
    """Exécute work(log) en arrière-plan ; la progression est consultable via GET /api/jobs/<id>"""
    job_id = uuid.uuid4().hex
    job = {"status": "running", "log": [], "result": None, "error": None, "created": time.time(), "cancel": False}

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
            log(f"✗ {e}")
            with jobs_lock:
                job.update(status="error", error=str(e))

    with jobs_lock:
        for old in [k for k, j in jobs.items() if time.time() - j["created"] > JOB_TTL]:
            del jobs[old]
        jobs[job_id] = job
    threading.Thread(target=runner, daemon=True).start()
    return job_id

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
            "channelUrl": str(e.get("channel_url") or e.get("uploader_url") or ""),
            "duration": e.get("duration"),
            "views": e.get("view_count"),
            "description": str(e.get("description") or "")[:300],
            "verified": bool(e.get("channel_is_verified")),
        })
    result = (videos, len(raw) >= EXPLORE_PAGE)
    with explore_lock:
        for old in [k for k, (t, _) in explore_cache.items() if time.time() - t > EXPLORE_TTL]:
            del explore_cache[old]
        explore_cache[key] = (time.time(), result)
    return result


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


def transcribe_live(headers, audio):
    """Transcrit un segment enregistré au micro (WebM/Ogg/MP4) → {text}"""
    engine = (headers.get("X-Engine") or "groq-turbo").strip()
    if engine not in ts.ENGINES:
        raise ValueError(f"Moteur de transcription inconnu : {engine}")
    keys = {"groq": (headers.get("X-Groq-Key") or "").strip(), "gemini": (headers.get("X-Gemini-Key") or "").strip()}
    if engine.startswith("groq") and not keys["groq"]:
        raise ValueError("Clé Groq manquante : ajoutez-la dans Configuration ou choisissez « Whisper local ».")
    if engine == "gemini" and not keys["gemini"]:
        raise ValueError("Clé Google AI manquante : ajoutez-la dans Configuration ou choisissez « Whisper local ».")
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
    routes = {"/api/analyze-youtube": youtube_work, "/api/analyze-text": text_work}

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        api = {"/api/explore": explore_youtube, "/api/oembed": youtube_oembed}.get(parsed.path)
        if api:
            try:
                self.send_json(200, api(urllib.parse.parse_qs(parsed.query)))
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self.send_json(502, {"error": f"Requête YouTube impossible : {e}"})
            return
        if not self.path.startswith("/api/jobs/"):
            super().do_GET()
            return
        with jobs_lock:
            job = jobs.get(self.path[len("/api/jobs/"):])
            snapshot = job and {k: job[k] for k in ("status", "result", "error")} | {"log": job["log"][-20:]}
        if snapshot:
            self.send_json(200, snapshot)
        else:
            self.send_json(404, {"error": "Tâche inconnue ou expirée (serveur redémarré ?)."})

    def do_POST(self):
        if self.path == "/api/transcribe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= LIVE_MAX_BYTES:
                    raise ValueError("Segment audio vide ou trop volumineux.")
                self.send_json(200, transcribe_live(self.headers, self.rfile.read(length)))
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self.send_json(502, {"error": str(e)[:300]})
            return
        make_work = self.routes.get(self.path)
        if not make_work:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Requête invalide.")
            work = make_work(payload)
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
            return
        self.send_json(202, {"jobId": start_job(work)})

    def do_DELETE(self):
        if not self.path.startswith("/api/jobs/"):
            self.send_error(404)
            return
        with jobs_lock:
            job = jobs.get(self.path[len("/api/jobs/"):])
            if job and job["status"] == "running":
                job["cancel"] = True
        self.send_json(200 if job else 404, {"canceled": bool(job)})

    def log_message(self, format, *args):
        pass  # Pas de logs HTTP (polling)

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"http://localhost:{PORT}/arbitre.html")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
