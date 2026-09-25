#!/usr/bin/env python3
# Serveur local Arbitre : récupère l'audio YouTube et le transmet à Gemini.
import json
import base64
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import uuid

PORT = 8766
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-pro", "gemini-flash-lite-latest"]
GEMINI_VERSIONS = ["v1beta", "v1"]
SYSTEM_PROMPT = """Tu es un expert en logique, rhétorique et argumentation. Tu analyses la transcription d'un débat oral en français. La transcription est automatique : elle peut contenir des erreurs de reconnaissance vocale, ne les considère pas comme des fautes de raisonnement.

Pour chaque intervention, identifie :
1. Les ARGUMENTS : la thèse défendue, les prémisses, le type d'argument (déductif, inductif, par analogie, par l'exemple, par l'autorité, par les conséquences, statistique), et sa solidité (forte, moyenne, faible) avec une justification courte.
2. Les SOPHISMES : uniquement s'ils sont clairement présents. Catalogue de référence : attaque personnelle (ad hominem), homme de paille, fausse alternative, pente glissante, appel à l'autorité non pertinente, appel à la popularité, appel à l'émotion, généralisation hâtive, corrélation n'est pas causalité, raisonnement circulaire, diversion (hareng rouge), tu quoque (toi aussi), charge de la preuve inversée, appel à la tradition, appel à la nature, faux dilemme, question piège, déplacement des critères, sophisme du juste milieu.
Pour chaque sophisme : nom, citation exacte de l'extrait concerné, explication simple (2 phrases max), niveau de confiance (élevé, moyen, faible).
3. Les AFFIRMATIONS FACTUELLES à vérifier (chiffres, dates, faits), sans juger si elles sont vraies.

Règles : sois impartial, ne signale pas un sophisme en cas de doute, et une émotion n'est pas automatiquement un sophisme.

Réponds UNIQUEMENT avec un objet JSON valide selon ce format :
{"orateurs":[{"nom":"string","these_principale":"string","arguments":[{"extrait":"string","these":"string","premisses":["string"],"type":"string","solidite":"forte|moyenne|faible","justification":"string"}],"sophismes":[{"nom":"string","extrait":"string","explication":"string","confiance":"élevé|moyen|faible"}],"faits_a_verifier":["string"]}],"synthese":"string"}"""


def request_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def groq_transcribe(key, path, mime_type):
    boundary = "----Arbitre" + uuid.uuid4().hex
    with open(path, "rb") as media_file:
        media = media_file.read()
    boundary_bytes = boundary.encode()
    body = b"--" + boundary_bytes + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"debat.mp4\"\r\nContent-Type: " + mime_type.encode() + b"\r\n\r\n" + media + b"\r\n--" + boundary_bytes + b"\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-large-v3-turbo\r\n--" + boundary_bytes + b"\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nfr\r\n--" + boundary_bytes + b"--\r\n"
    request = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8")).get("text", "")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq transcription : {detail[:300]}") from error


def gemini_text(key, contents, system=None):
    payload = {"contents": contents, "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000}}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    last_not_found = ""
    for version in GEMINI_VERSIONS:
        for model in GEMINI_MODELS:
            for attempt in range(3):
                try:
                    data = request_json(f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent?key={urllib.parse.quote(key)}", payload)
                    return "".join(part.get("text", "") for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []))
                except urllib.error.HTTPError as error:
                    if error.code == 503:
                        if attempt < 2:
                            time.sleep(4)
                            continue
                        last_not_found = error.read().decode("utf-8", errors="replace")
                        break
                    if error.code != 404:
                        raise
                    last_not_found = error.read().decode("utf-8", errors="replace")
                    break
    try:
        available = []
        for version in GEMINI_VERSIONS:
            with urllib.request.urlopen(f"https://generativelanguage.googleapis.com/{version}/models?key={urllib.parse.quote(key)}", timeout=30) as response:
                available.extend(item.get("name", "").replace("models/", "") for item in json.loads(response.read().decode()).get("models", []) if "generateContent" in item.get("supportedGenerationMethods", []))
    except Exception:
        available = []
    suffix = f" Modèles disponibles : {', '.join(available[:8])}." if available else " Vérifiez que cette clé vient de Google AI Studio et qu’elle est active."
    detail = f" Détail Google : {last_not_found[:300]}" if last_not_found else ""
    raise RuntimeError("Le modèle Gemini ou le fichier multimédia est introuvable." + suffix + detail)


def upload_gemini(key, path, mime_type):
    size = os.path.getsize(path)
    request = urllib.request.Request(f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={urllib.parse.quote(key)}", data=json.dumps({"file": {"display_name": os.path.basename(path)}}).encode(), headers={"Content-Type": "application/json", "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": mime_type})
    with urllib.request.urlopen(request, timeout=60) as response:
        upload_url = response.headers.get("X-Goog-Upload-URL")
    with open(path, "rb") as audio:
        request = urllib.request.Request(upload_url, data=audio.read(), method="POST", headers={"Content-Type": mime_type, "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize", "Content-Length": str(size)})
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode()).get("file", {})


def wait_for_file(key, media):
    name = media.get("name")
    if not name:
        return media
    for _ in range(60):
        with urllib.request.urlopen(f"https://generativelanguage.googleapis.com/v1beta/{name}?key={urllib.parse.quote(key)}", timeout=30) as response:
            current = json.loads(response.read().decode())
        state = current.get("state", "ACTIVE")
        if state == "ACTIVE":
            return current
        if state == "FAILED":
            raise RuntimeError("Google n’a pas pu traiter le fichier vidéo.")
        time.sleep(1)
    raise RuntimeError("Le traitement de la vidéo par Google prend trop de temps.")


def analyze_youtube(payload):
    key = payload.get("apiKey", "").strip()
    groq_key = payload.get("groqKey", "").strip()
    url = payload.get("url", "").strip()
    topic = payload.get("topic", "")
    if not key or not groq_key or not url:
        raise ValueError("Clé Google AI, clé Groq ou URL YouTube manquante.")
    try:
        import yt_dlp
    except ImportError as error:
        raise RuntimeError("Le module yt-dlp manque. Lancez : python3 -m pip install --user yt-dlp") from error
    with tempfile.TemporaryDirectory() as folder:
        output = os.path.join(folder, "debate.%(ext)s")
        options = {"format": "18", "outtmpl": output, "quiet": True, "noplaylist": True, "extractor_args": {"youtube": {"player_client": ["android"]}}}
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])
        files = [os.path.join(folder, name) for name in os.listdir(folder)]
        if not files:
            raise RuntimeError("Aucun audio n'a pu être récupéré depuis cette vidéo.")
        path = files[0]
        mime_type = mimetypes.guess_type(path)[0] or "audio/mp4"
        transcript = groq_transcribe(groq_key, path, mime_type)
        if not transcript:
            raise RuntimeError("Groq n’a renvoyé aucune transcription.")
        analysis_text = gemini_text(key, [{"role": "user", "parts": [{"text": f"Sujet : {topic or 'Non précisé'}\n\nTranscription :\n{transcript}"}]}], SYSTEM_PROMPT)
        cleaned = analysis_text.replace("```json", "").replace("```", "").strip()
        return {"transcript": transcript, "analysis": json.loads(cleaned)}


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/analyze-youtube":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            result = analyze_youtube(json.loads(self.rfile.read(length)))
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
        except Exception as error:
            print(f"Erreur analyse YouTube : {error}", file=sys.stderr)
            traceback.print_exc()
            body = json.dumps({"error": str(error)}, ensure_ascii=False).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f"Arbitre ouvert sur http://localhost:{PORT}/arbitre.html")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
