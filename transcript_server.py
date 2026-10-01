#!/usr/bin/env python3
"""POC simple: YouTube → Transcription (Groq Whisper, faster-whisper local ou Gemini), vidéos longues découpées"""

import base64
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # CDN xet de HuggingFace bloqué par le proxy (403)

try:  # TLS via le magasin de certificats Windows (proxy d'entreprise) plutôt que certifi
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

PORT = 8765
YDL_BASE = {"quiet": True, "noplaylist": True, "compat_opts": ["no-certifi"]}  # certificats système (proxy)
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GEMINI_MODELS = ["gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-2.5-flash", "gemini-flash-latest"]
LOCAL_MODEL = "small"  # faster-whisper : tiny | base | small | medium | large-v3 (plus gros = plus lent sur CPU)
ENGINES = {  # moteur → modèles essayés dans l'ordre
    "groq-turbo": ["whisper-large-v3-turbo", "whisper-large-v3"],
    "groq-v3": ["whisper-large-v3", "whisper-large-v3-turbo"],
    "local": [f"faster-whisper-{LOCAL_MODEL}"],
    "gemini": GEMINI_MODELS,
}
SEGMENT_SECONDS = 600  # 10 min par segment : la transcription tient largement dans la limite de sortie
MAX_WORKERS = 3        # segments transcrits en parallèle (reste sous les quotas du tier gratuit)
MAX_RETRIES = 3        # tentatives par modèle avant de passer au suivant
RETRY_ROUNDS = 3       # passes supplémentaires sur les segments en échec (modèles surchargés)
ROUND_PAUSE = 30       # pause (s) avant chaque passe supplémentaire

PROMPT = (
    "Transcris intégralement et fidèlement la parole de cet extrait audio, mot pour mot, "
    "dans sa langue d'origine. Il s'agit d'un segment d'un enregistrement plus long : "
    "ne résume pas, n'ajoute ni introduction ni commentaire. "
    "Retourne uniquement le texte transcrit (chaîne vide s'il n'y a pas de parole)."
)


def get_ffmpeg():
    """Chemin de ffmpeg : installation système ou binaire fourni par imageio-ffmpeg"""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("ffmpeg introuvable. pip install imageio-ffmpeg (ou installez ffmpeg)")


def split_audio(src, folder):
    """Convertit en MP3 mono 16 kHz 32 kbps et découpe en segments de SEGMENT_SECONDS"""
    pattern = os.path.join(folder, "seg_%04d.mp3")
    cmd = [
        get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
        pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg a échoué: {proc.stderr.strip()[:500]}")
    segments = sorted(glob.glob(os.path.join(folder, "seg_*.mp3")))
    if not segments:
        raise RuntimeError("Aucun segment audio produit")
    return segments


def parse_gemini_error(e):
    """Extrait message, délai conseillé (s) et type de quota d'une erreur HTTP Gemini"""
    raw = e.read().decode("utf-8", errors="replace")
    try:
        err = json.loads(raw).get("error", {})
    except ValueError:
        return raw[:200], None, ""
    delay, quota = None, ""
    for d in err.get("details", []):
        if d.get("@type", "").endswith("RetryInfo"):
            try:
                delay = float(d.get("retryDelay", "0s").rstrip("s"))
            except ValueError:
                pass
        for v in d.get("violations", []):
            quota = v.get("quotaId", quota)
    return err.get("message", raw[:200]).split("\n")[0][:200], delay, quota


exhausted_until = {}  # modèle → timestamp jusqu'auquel son quota est épuisé


def is_exhausted(model):
    return exhausted_until.get(model, 0) > time.time()


def gemini_generate(api_key, payload_for_model):
    """Appel generateContent avec repli de modèle et relances sur 429/5xx"""
    last_error = "quota épuisé sur tous les modèles"
    for model in GEMINI_MODELS:
        if is_exhausted(model):
            continue
        data = json.dumps(payload_for_model(model)).encode("utf-8")
        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(
                f"{API_BASE}/models/{model}:generateContent",
                data=data,
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            wait = 5 * 2 ** attempt
            try:
                with urllib.request.urlopen(req, timeout=600) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                message, delay, quota = parse_gemini_error(e)
                last_error = f"HTTP {e.code} ({model}): {message}"
                if e.code == 404:
                    break
                if e.code == 429 and "PerDay" in quota:
                    exhausted_until[model] = time.time() + max(delay or 0, 3600)
                    print(f"   … {model}: quota journalier épuisé")
                    break
                if e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(last_error)
                if delay:
                    wait = min(delay + 1, 60)
            except (urllib.error.URLError, TimeoutError) as e:
                last_error = f"{type(e).__name__} ({model}): {e}"
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)
        else:
            print(f"   … {model} indisponible, modèle suivant")
    raise RuntimeError(f"Échec Gemini: {last_error}")


def youtube_captions(info):
    """Sous-titres YouTube (manuels ou automatiques) sous forme [(début_s, texte)]"""
    import yt_dlp
    lang = info.get("language")
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    candidates = []
    if lang:
        candidates += [(manual, lang), (auto, f"{lang}-orig"), (auto, lang)]
    candidates += [(auto, k) for k in auto if k.endswith("-orig")]
    candidates += [(manual, k) for k in manual if k != "live_chat"]
    for tracks, key in candidates:
        fmt = next((f for f in tracks.get(key, []) if f.get("ext") == "json3"), None)
        if not fmt:
            continue
        try:
            with yt_dlp.YoutubeDL(YDL_BASE) as dl:
                data = json.loads(dl.urlopen(fmt["url"]).read().decode("utf-8"))
        except Exception as e:
            print(f"   ⚠ Sous-titres {key} illisibles: {e}")
            continue
        print(f"      Sous-titres YouTube utilisés: {key}")
        return [
            (ev.get("tStartMs", 0) / 1000, "".join(s.get("utf8", "") for s in ev["segs"]))
            for ev in data.get("events", []) if ev.get("segs")
        ]
    return []


def gemini_transcribe(api_key, path, index, total):
    """Transcrit un segment audio via Gemini (envoyé inline, ~2,4 Mo pour 10 min)"""
    with open(path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("ascii")

    def payload_for_model(model):
        config = {"maxOutputTokens": 65536}
        if model.startswith("gemini-2.5"):
            config["thinkingConfig"] = {"thinkingBudget": 0}  # le thinking consomme le budget de sortie
        return {
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}},
                ],
            }],
            "generationConfig": config,
        }

    result = gemini_generate(api_key, payload_for_model)
    candidates = result.get("candidates") or []
    if not candidates:
        reason = result.get("promptFeedback", {}).get("blockReason", "réponse vide")
        raise RuntimeError(f"Segment {index + 1}/{total} rejeté: {reason}")

    candidate = candidates[0]
    text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
    finish = candidate.get("finishReason")
    if finish not in (None, "STOP"):
        print(f"   ⚠ Segment {index + 1}/{total}: finishReason={finish}")
    return text


def groq_transcribe(api_key, path, models, lang):
    """Transcrit un segment via Whisper hébergé par Groq (API compatible OpenAI), avec repli entre modèles"""
    with open(path, "rb") as f:
        audio = f.read()
    last_error = "quota épuisé sur tous les modèles Groq"
    for model in models:
        if is_exhausted(model):
            continue
        fields = {"model": model, "response_format": "json", "temperature": "0"}
        if lang:
            fields["language"] = lang
        boundary = uuid.uuid4().hex
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items()
        ) + (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(path)}"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode() + audio + f"\r\n--{boundary}--\r\n".encode()

        for attempt in range(MAX_RETRIES):
            req = urllib.request.Request(GROQ_URL, data=body, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "arbitre-transcriber/1.0",  # l'UA par défaut de urllib est bloqué par Cloudflare
            })
            wait = 5 * 2 ** attempt
            try:
                with urllib.request.urlopen(req, timeout=600) as response:
                    return json.loads(response.read().decode("utf-8")).get("text", "")
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    message = json.loads(raw).get("error", {}).get("message", raw)
                except ValueError:
                    message = raw
                last_error = f"HTTP {e.code} ({model}): {message[:200]}"
                if e.code == 404:
                    break
                if e.code == 429:
                    try:
                        retry_after = float(e.headers.get("retry-after") or 0)
                    except ValueError:
                        retry_after = 0
                    if retry_after > 60:  # quota horaire/journalier : on passe au modèle suivant
                        exhausted_until[model] = time.time() + retry_after
                        print(f"   … {model}: quota épuisé (dispo dans {retry_after / 60:.0f} min)")
                        break
                    wait = max(retry_after, 1)
                elif e.code not in (500, 502, 503, 504):
                    raise RuntimeError(last_error)
            except (urllib.error.URLError, TimeoutError) as e:
                last_error = f"{type(e).__name__} ({model}): {e}"
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)
        else:
            print(f"   … {model} indisponible, modèle suivant")
    raise RuntimeError(f"Échec Groq: {last_error}")


_local_model = None
_local_lock = threading.Lock()


def _decode_pcm(path):
    """Décode en PCM float32 16 kHz mono via ffmpeg : contourne le décodeur PyAV de faster-whisper,
    incompatible avec PyAV >= 15 (argument metadata_errors supprimé)"""
    import numpy as np
    proc = subprocess.run(
        [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Décodage audio impossible : {proc.stderr.decode(errors='ignore').strip()[:200]}")
    return np.frombuffer(proc.stdout, np.int16).astype(np.float32) / 32768.0


def local_transcribe(path, lang):
    """Transcrit un segment en local via faster-whisper (CPU, int8) : ni clé ni quota"""
    global _local_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper manquant. pip install faster-whisper")
    audio = _decode_pcm(path)
    if not audio.size:
        return ""
    with _local_lock:
        if _local_model is None:
            print(f"      Chargement du modèle local '{LOCAL_MODEL}' (téléchargé au 1er lancement)...")
            _local_model = WhisperModel(LOCAL_MODEL, device="cpu", compute_type="int8")
        segments, _ = _local_model.transcribe(audio, language=lang, vad_filter=True)
        return " ".join(s.text.strip() for s in segments)


def transcribe_segment(engine, keys, path, index, total, lang):
    """Transcrit un segment avec le moteur choisi"""
    if engine == "local":
        text = local_transcribe(path, lang)
    elif engine == "gemini":
        text = gemini_transcribe(keys["gemini"], path, index, total)
    else:
        text = groq_transcribe(keys["groq"], path, ENGINES[engine], lang)
    return text.strip()


def transcribe_youtube(url, engine, keys, log=print):
    """Télécharge l'audio YouTube, le découpe et retourne la transcription complète (log : suivi de progression)"""
    if engine not in ENGINES:
        raise RuntimeError(f"Moteur inconnu: {engine}")
    if not url:
        raise RuntimeError("URL requise")
    if engine == "gemini" and not keys.get("gemini"):
        raise RuntimeError("Clé API Gemini requise")
    if engine.startswith("groq") and not keys.get("groq"):
        raise RuntimeError("Clé API Groq requise")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp manquant. pip install yt-dlp")

    with tempfile.TemporaryDirectory() as folder:
        output = os.path.join(folder, "source.%(ext)s")
        log("[1/4] Téléchargement audio...")
        with yt_dlp.YoutubeDL({**YDL_BASE, "format": "bestaudio/best", "outtmpl": output}) as dl:
            info = dl.extract_info(url, download=True)

        files = glob.glob(os.path.join(folder, "source.*"))
        if not files:
            raise RuntimeError("Aucun fichier téléchargé")
        path = files[0]
        log(f"      {os.path.basename(path)} ({os.path.getsize(path) / 1024 / 1024:.1f} Mo)")

        log(f"[2/4] Découpage en segments de {SEGMENT_SECONDS // 60} min...")
        seg_folder = os.path.join(folder, "segments")
        os.makedirs(seg_folder)
        segments = split_audio(path, seg_folder)
        total = len(segments)
        log(f"      {total} segment(s)")

        lang = (info.get("language") or "").split("-")[0].lower() or None
        workers = 1 if engine == "local" else MAX_WORKERS
        log(f"[3/4] Transcription {engine} (langue: {lang or 'auto'}, {workers} en parallèle)...")
        texts = [None] * total
        errors = {}

        def run(i):
            try:
                texts[i] = transcribe_segment(engine, keys, segments[i], i, total, lang)
                errors.pop(i, None)
                log(f"   ✓ Segment {i + 1}/{total} ({len(texts[i])} caractères)")
            except Exception as e:
                errors[i] = str(e)
                log(f"   ✗ Segment {i + 1}/{total}: {str(e)[:120]}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, range(total)))

        for round_no in range(1, RETRY_ROUNDS + 1):
            if not errors or engine == "local" or all(is_exhausted(m) for m in ENGINES[engine]):
                break
            if any(re.search(r"HTTP 40[13]", e) for e in errors.values()):  # clé refusée : inutile de réessayer
                break
            log(f"      Passe {round_no}/{RETRY_ROUNDS} sur {len(errors)} segment(s) en échec dans {ROUND_PAUSE}s...")
            time.sleep(ROUND_PAUSE)
            for i in sorted(errors):
                run(i)

        from_captions = []
        if errors:
            log(f"      Repli sur les sous-titres YouTube pour {len(errors)} segment(s)...")
            captions = youtube_captions(info)
            for i in sorted(errors):
                start, end = i * SEGMENT_SECONDS, (i + 1) * SEGMENT_SECONDS
                text = " ".join(" ".join(t for s, t in captions if start <= s < end).split())
                if text:
                    texts[i] = text
                    from_captions.append(i)
                    del errors[i]

        if len(errors) == total:
            raise RuntimeError(next(iter(errors.values())))

        parts = []
        for i, text in enumerate(texts):
            if i in errors:
                parts.append(f"[Segment {i + 1}/{total} non transcrit ({engine} indisponible)]")
            elif text:
                parts.append(text)
        transcript = "\n\n".join(parts)
        status = "✓ Complet" if not errors else f"⚠ Partiel ({len(errors)} segment(s) manquant(s))"
        log(f"[4/4] {status} ({len(transcript)} caractères, {len(from_captions)} segment(s) via sous-titres YouTube)")
        duration = info.get("duration")
        return {
            "title": info.get("title") or "",
            "parts": [  # segments transcrits avec leur position dans la vidéo (s)
                {
                    "start": i * SEGMENT_SECONDS,
                    "duration": min(SEGMENT_SECONDS, max(duration - i * SEGMENT_SECONDS, 1)) if duration else SEGMENT_SECONDS,
                    "text": text,
                }
                for i, text in enumerate(texts) if i not in errors and text
            ],
            "transcript": transcript,
            "segments": total,
            "failedSegments": [i + 1 for i in sorted(errors)],
            "captionSegments": [i + 1 for i in from_captions],
        }


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/transcribe":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))

            result = transcribe_youtube(
                url=payload.get("url", "").strip(),
                engine=payload.get("engine", "groq-turbo"),
                keys={
                    "gemini": payload.get("apiKey", "").strip(),
                    "groq": payload.get("groqKey", "").strip(),
                },
            )

            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
        except Exception as e:
            print(f"[ERR] {e}")
            body = json.dumps({"error": str(e)}, ensure_ascii=False).encode()
            self.send_response(500)

        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"http://localhost:{PORT}/transcript.html")
    server.serve_forever()
