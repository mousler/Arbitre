#!/usr/bin/env python3
"""Run bounded community-user simulations against an isolated local server."""

import argparse
import concurrent.futures
import heapq
import http.cookiejar
import importlib
import itertools
import json
import math
import os
import random
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "arbitre_server.py"
PERSONAS = ("creator", "community", "messages", "reader", "complete")
MODES = ("balanced", "complete", "community", "messages", "reader", "creator")
MAX_USERS = 10000
MAX_CONCURRENCY = 100
COHORT_SIZE = 100
ACTIONS = ("browse", "view_post", "comment", "reply", "react", "react_comment", "verdict", "follow", "unfollow",
           "send", "inbox", "thread", "notifications", "search", "explore", "leaderboard", "profile_view",
           "profile", "analyze", "publish", "report", "leave")
# Chaque compte peut tout faire; le persona ne change que la fréquence relative des actions.
PERSONA_WEIGHTS = {
    "complete": {},
    "creator": {"analyze": 5, "publish": 5, "browse": 3, "view_post": 3, "reply": 2, "notifications": 2},
    "community": {"comment": 5, "reply": 4, "react": 5, "react_comment": 3, "verdict": 3, "follow": 3,
                  "view_post": 4, "browse": 3, "report": 2},
    "messages": {"send": 6, "thread": 4, "inbox": 4, "notifications": 2, "follow": 2, "profile_view": 2},
    "reader": {"browse": 6, "view_post": 6, "react": 3, "verdict": 2, "explore": 2, "leaderboard": 2, "search": 2},
}
# Délais minimaux par famille d'actions, sous les limites anti-abus de l'application.
COOLDOWNS = {"comment": 35, "react": 6, "follow": 15, "send": 5, "publish": 420, "analyze": 60, "report": 400,
             "profile": 30, "verdict": 3, "search": 1, "explore": 1, "leave": 30}
COOLDOWN_GROUPS = {"reply": "comment", "react_comment": "react", "unfollow": "follow"}
REPORT_REASONS = (
    "Propos hors sujet répétés.",
    "Affirmation trompeuse sans source.",
    "Ton agressif envers un autre membre.",
    "Contenu publicitaire déguisé.",
)
SEARCH_TERMS = ("sim", "new", "analyse", "débat", "source", "IA", "transport", "école")
TOPICS = (
    ("Semaine de quatre jours",
     "Orateur A : La semaine de quatre jours améliore la productivité et la santé des équipes.\n"
     "Orateur B : Les hôpitaux et les commerces ne peuvent pas réduire leurs heures sans embaucher."),
    ("Transport en commun gratuit",
     "Orateur A : La gratuité du transport en commun réduit la congestion et les émissions.\n"
     "Orateur B : Sans financement stable, la gratuité dégrade la fréquence du service."),
    ("Encadrement de l'IA générative",
     "Orateur A : Une loi claire sur l'IA générative protège les citoyens.\n"
     "Orateur B : Une réglementation trop rapide freine l'innovation locale."),
    ("Nucléaire et transition énergétique",
     "Orateur A : Le nucléaire fournit une production stable et faible en carbone.\n"
     "Orateur B : Les coûts et les délais des centrales retardent la transition."),
    ("Téléphones à l'école",
     "Orateur A : Interdire les téléphones en classe améliore la concentration.\n"
     "Orateur B : Les élèves doivent apprendre à utiliser ces outils de façon responsable."),
    ("Télétravail choisi",
     "Orateur A : Le télétravail choisi retient les talents et réduit les déplacements.\n"
     "Orateur B : La collaboration et l'intégration des nouveaux souffrent à distance."),
)
COMMENTS = (
    "Quel élément pourrait confirmer cette conclusion ?",
    "Je vois une autre interprétation possible de cet argument.",
    "La prémisse principale me semble solide, mais la source manque.",
    "L'orateur B répond-il vraiment à l'objection de l'orateur A ?",
    "Bonne synthèse, j'aurais ajouté le coût de la mesure.",
    "Ce point mérite une vérification supplémentaire.",
)
MESSAGES = (
    "As-tu vu le dernier argument publié ?",
    "Je t'envoie une question sur ton analyse : quelles sources as-tu utilisées ?",
    "Merci pour cet échange, je vais relire les sources.",
    "Ton commentaire m'a fait changer d'avis sur la deuxième prémisse.",
    "On pourrait comparer nos analyses sur ce débat ?",
)
SQL_PROBES = ("' OR '1'='1", "%' OR 1=1 --", '") OR ("1"="1', "' UNION SELECT name, sql FROM sqlite_master --")
PROTECTED_PATHS = ("/data/rhetora.db", "/data/", "/arbitre_server.py", "/community.py", "/simulate.py", "/README.md",
                   "/simulation-results/live.json", "/.git/config", "/..%2farbitre_server.py",
                   "/%2e%2e/%2e%2e/Windows/win.ini", "/arbitre.html/../community.py", "/data%2frhetora.db")
# Configuration qui rend deux exécutions comparables pour les latences.
COMPARABLE_KEYS = ("users", "concurrency", "duration_seconds", "mode", "pace", "seed", "ramp_seconds",
                   "signups_per_minute", "visit_percent")
CHECK_RANK = {"failed": 0, "warning": 1, "passed": 2}


class Metrics:
    def __init__(self, status_file=None, users=0, concurrency=0, duration_seconds=0, mode="balanced"):
        self.lock = threading.Lock()
        self.requests = deque(maxlen=100)
        self.errors = deque(maxlen=100)
        self.request_count = 0
        self.error_count = 0
        self.action_stats = {}
        self.sample_rng = random.Random(0)
        self.checks = []
        self.started = time.monotonic()
        self.phase = "Initialisation"
        self.status = "running"
        self.status_file = Path(status_file) if status_file else None
        self.status_config = {
            "users": users,
            "concurrency": concurrency,
            "duration_seconds": duration_seconds,
            "mode": mode,
        }
        self.last_status_write = 0
        self.activity_started = None
        self.actors = {}
        self.social_activity = deque(maxlen=200)
        self.social_activity_count = 0
        self.agent_activity = deque(maxlen=200)
        self.agent_activity_count = 0
        self.baseline = None
        self.sandbox = None
        self.journal_file = None
        self.actors_file = None
        self.last_actors_write = 0
        if self.status_file:
            self.journal_file = self.status_file.with_name(self.status_file.stem + "-activity.jsonl")
            self.actors_file = self.status_file.with_name(self.status_file.stem + "-actors.json")
            self.journal_file.parent.mkdir(parents=True, exist_ok=True)
            self.journal_file.write_text("", encoding="utf-8")

    def record(self, action, status, elapsed_ms, message="", expected=(200,)):
        item = {
            "action": action,
            "status": status,
            "success": status in expected,
            "elapsed_ms": round(elapsed_ms, 2),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            self.requests.append(item)
            self.request_count += 1
            stats = self.action_stats.setdefault(action, {
                "requests": 0, "successes": 0, "failures": 0, "max_ms": 0, "samples": [], "http_statuses": {},
            })
            stats["requests"] += 1
            status_key = str(status)
            stats["http_statuses"][status_key] = stats["http_statuses"].get(status_key, 0) + 1
            stats["successes"] += int(item["success"])
            stats["failures"] += int(not item["success"])
            stats["max_ms"] = max(stats["max_ms"], item["elapsed_ms"])
            samples = stats["samples"]
            if len(samples) < 10000:
                samples.append(item["elapsed_ms"])
            else:
                position = self.sample_rng.randrange(stats["requests"])
                if position < len(samples):
                    samples[position] = item["elapsed_ms"]
            if not item["success"]:
                self.error_count += 1
                self.errors.append({"action": action, "status": status, "message": message[:240]})
            self._write_status_locked()

    def record_error(self, action, message):
        with self.lock:
            self.error_count += 1
            self.errors.append({"action": action, "status": 0, "message": str(message)[:240]})
            self._write_status_locked(force=True)

    def set_phase(self, phase):
        with self.lock:
            self.phase = phase
            if phase.startswith("Interactions continues"):
                self.activity_started = time.monotonic()
            self._write_status_locked(force=True)

    def check(self, role, name, passed, detail="", warning=False):
        status = "passed" if passed else "warning" if warning else "failed"
        with self.lock:
            self.checks.append({"role": role, "name": name, "status": status, "detail": str(detail)[:400]})
            self._agent_event_locked(role, name, status, detail)
            if status == "failed":
                self.error_count += 1
                self.errors.append({"action": f"audit.{role}.{name}", "status": 0, "message": str(detail)[:240]})
            self._write_status_locked(force=True)

    def agent_event(self, role, name, state, detail=""):
        with self.lock:
            self._agent_event_locked(role, name, state, detail)
            self._write_status_locked(force=True)

    def _agent_event_locked(self, role, name, state, detail):
        self.agent_activity_count += 1
        self.agent_activity.append({"id": self.agent_activity_count, "at": datetime.now(timezone.utc).isoformat(),
                                    "role": role, "name": name, "state": state, "detail": str(detail)[:400]})

    def set_baseline(self, baseline):
        with self.lock:
            self.baseline = baseline
            self._write_status_locked(force=True)

    def audit_summary(self):
        summary = {"passed": 0, "warning": 0, "failed": 0}
        for check in self.checks:
            summary[check["status"]] += 1
        return summary

    def set_actors(self, usernames, mode="balanced"):
        with self.lock:
            self.actors = {
                username: {"username": username, "status": "Hors ligne", "actions": 0,
                           "last_action": "Compte sandbox prêt", "detail": "", "joined": "fixture",
                           "persona": persona_for(index, mode)}
                for index, username in enumerate(usernames)
            }
            self._write_status_locked(force=True)

    def add_actor(self, username, persona, joined="api", status="Hors ligne"):
        with self.lock:
            self.actors[username] = {"username": username, "status": status, "actions": 0,
                                     "last_action": "Compte prêt", "detail": "", "persona": persona, "joined": joined}
            self._write_status_locked(force=True)

    def set_sandbox(self, **values):
        with self.lock:
            self.sandbox = (self.sandbox or {}) | values
            self._write_status_locked(force=True)

    def actor_event(self, username, action, detail="", peer="", kind="activity", content="", post_id=None, title="",
                    status=None):
        with self.lock:
            actor = self.actors.setdefault(username, {
                "username": username, "status": "En ligne", "actions": 0, "last_action": "", "detail": "",
            })
            if kind == "error":
                actor["status"] = "Erreur"
            elif status:
                actor["status"] = status
            elif actor["status"] in ("Hors ligne", "Erreur"):
                actor["status"] = "En ligne"
            actor["actions"] += 1
            actor["last_action"] = action
            actor["detail"] = detail[:180]
            self.social_activity_count += 1
            event = {
                "id": self.social_activity_count,
                "at": datetime.now(timezone.utc).isoformat(),
                "username": username,
                "persona": actor.get("persona", ""),
                "action": action,
                "kind": kind,
                "detail": detail[:180],
                "peer": peer,
                "content": str(content)[:2000],
            }
            if post_id is not None:
                event["post_id"] = post_id
            if title:
                event["title"] = title[:160]
            self.social_activity.append(event)
            if self.journal_file:
                try:
                    with self.journal_file.open("a", encoding="utf-8") as journal:
                        journal.write(json.dumps(event, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            self._write_status_locked()

    def finish(self, report_path, status, sandbox_open=False):
        with self.lock:
            self.status = status
            self.phase = {
                "passed": "Simulation terminée",
                "canceled": "Simulation arrêtée par l'utilisateur",
            }.get(status, "Simulation terminée avec erreurs")
            if sandbox_open:
                self.phase += " · site sandbox consultable"
            if self.sandbox:
                self.sandbox["open"] = sandbox_open
            self.report_path = str(report_path)
            self._write_status_locked(force=True)

    def close_sandbox(self):
        with self.lock:
            if self.sandbox:
                self.sandbox["open"] = False
            self.phase = self.phase.replace(" · site sandbox consultable", "") + " · sandbox fermée"
            self._write_status_locked(force=True)

    def population_summary(self):
        summary = {"total": len(self.actors), "online": 0, "offline": 0, "errors": 0, "signups": 0}
        for actor in self.actors.values():
            summary[{"En ligne": "online", "Erreur": "errors"}.get(actor["status"], "offline")] += 1
            summary["signups"] += actor.get("joined") == "signup"
        return summary

    def _write_actors_locked(self, force=False):
        now = time.monotonic()
        if not self.actors_file or (not force and now - self.last_actors_write < 2):
            return
        self.last_actors_write = now
        temporary = self.actors_file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(list(self.actors.values()), ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.actors_file)
        except OSError:
            pass

    def _write_status_locked(self, force=False):
        if not self.status_file:
            return
        now = time.monotonic()
        if not force and now - self.last_status_write < 0.25:
            return
        self._write_actors_locked(force)
        events = list(self.requests)[-40:]
        payload = {
            "status": self.status,
            "phase": self.phase,
            "configuration": self.status_config,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "throughput_rps": round(self.request_count / max(0.001, now - self.started), 2),
            "actions": self.summary(),
            "checks": list(self.checks),
            "recent_events": events,
            "population": self.population_summary(),
            "social_activity": [event | {"content": event["content"][:600]} for event in list(self.social_activity)[-100:]],
            "sandbox": self.sandbox,
            "social_activity_count": self.social_activity_count,
            "agent_activity": list(self.agent_activity)[-80:],
            "agent_activity_count": self.agent_activity_count,
            "baseline": self.baseline,
            "errors": list(self.errors)[-30:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.activity_started is not None:
            payload["activity_elapsed_seconds"] = round(time.monotonic() - self.activity_started, 1)
        if hasattr(self, "report_path"):
            payload["report_path"] = self.report_path
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            for attempt in range(8):
                try:
                    temporary.replace(self.status_file)
                    self.last_status_write = now
                    return
                except PermissionError:
                    if attempt == 7:
                        break
                    time.sleep(0.025 * (attempt + 1))
        except OSError:
            pass

    def summary(self):
        actions = {}
        for action, stats in sorted(self.action_stats.items()):
            latencies = sorted(stats["samples"])
            actions[action] = {
                "requests": stats["requests"],
                "successes": stats["successes"],
                "failures": stats["failures"],
                "latency_sample_count": len(latencies),
                "http_statuses": dict(stats["http_statuses"]),
                "p50_ms": percentile(latencies, 0.50),
                "p95_ms": percentile(latencies, 0.95),
                "p99_ms": percentile(latencies, 0.99),
                "max_ms": stats["max_ms"],
            }
        return actions


def percentile(values, fraction):
    if not values:
        return 0
    index = max(0, math.ceil(len(values) * fraction) - 1)
    return round(values[index], 2)


def persona_for(index, mode):
    return PERSONAS[index % len(PERSONAS)] if mode == "balanced" else mode


def loopback_address(index):
    """Adresse loopback propre à un compte : les limites par IP s'appliquent comme pour des visiteurs distincts."""
    number = index + 1
    return f"127.{10 + number // 62500}.{number // 250 % 250 + 1}.{number % 250 + 1}"


class ApiClient:
    def __init__(self, base_url, source_ip=None):
        self.base_url = base_url
        self.source_ip = source_ip
        self.cookies = http.cookiejar.CookieJar()
        handlers = [urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.cookies)]
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, method, path, payload=None, header_overrides=None):
        headers = {"Accept": "application/json", "User-Agent": "RhetoraSimulation/1.0"}
        if self.source_ip:
            headers["X-Rhetora-Simulated-IP"] = self.source_ip
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if method != "GET":
            headers["X-Rhetora"] = "1"
            headers["Origin"] = self.base_url

        for name, value in (header_overrides or {}).items():
            if value is None:
                headers.pop(name, None)
            else:
                headers[name] = value

        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, ValueError):
                data = {"error": raw.decode("utf-8", errors="replace")[:240]}
            return response.status, data


def perform(metrics, client, action, method, path, payload=None, expected=(200,), header_overrides=None):
    started = time.perf_counter()
    try:
        status, data = client.request(method, path, payload, header_overrides)
        message = data.get("error", "") if isinstance(data, dict) else "Réponse inattendue"
    except Exception as error:
        status, data, message = 0, {}, f"{type(error).__name__}: {error}"
    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.record(action, status, elapsed_ms, message, expected)
    if status not in expected:
        raise RuntimeError(f"{action}: HTTP {status}: {message or 'échec de la requête'}")
    return data


def available_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_server(process, base_url, log_path, timeout=20):
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Le serveur sandbox s'est arrêté (code {process.returncode}).")
        try:
            with opener.open(base_url + "/arbitre.html", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
    raise RuntimeError("Délai dépassé au démarrage du serveur sandbox. " + tail.strip())


def seed_accounts(data_dir, usernames, password):
    if not usernames:
        return
    os.environ["RHETORA_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(ROOT))
    community = importlib.import_module("community")
    password_hash = community.hash_password(password)
    created = int(time.time())
    with community.tx() as connection:
        connection.executemany(
            "INSERT INTO users (username, email, pw_hash, role, status, created_at) VALUES (?,?,?,?,?,?)",
            ((username, f"{username}@example.test", password_hash, "user", "active", created) for username in usernames),
        )


class SimulationCanceled(Exception):
    pass


def run_parallel(label, items, worker, concurrency, metrics, cancel_file=None):
    print(f"[{label}] {len(items)} acteur(s), {concurrency} requête(s) concurrente(s)")
    results = [None] * len(items)
    remaining = iter(enumerate(items))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {}
        while pending or remaining is not None:
            if cancel_file and Path(cancel_file).is_file():
                for future in pending:
                    future.cancel()
                raise SimulationCanceled()
            while remaining is not None and len(pending) < concurrency:
                entry = next(remaining, None)
                if entry is None:
                    remaining = None
                    break
                index, item = entry
                pending[executor.submit(worker, item)] = index
            finished, _ = concurrent.futures.wait(pending, timeout=0.1,
                                                  return_when=concurrent.futures.FIRST_COMPLETED)
            for future in finished:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except SimulationCanceled:
                    raise
                except Exception as error:
                    metrics.record_error(label, error)
    return [result for result in results if result is not None]


REPLIES = (
    "Je suis d'accord, et la source citée va dans ce sens.",
    "Pas tout à fait : l'exemple ne couvre qu'un cas particulier.",
    "Bonne question, je n'avais pas vu cette objection.",
    "Peux-tu préciser ce que tu entends par là ?",
)


def make_actor(username, password, index, persona, seed=42, contacts=(), online=False, client=None, joined="fixture"):
    return {"username": username, "password": password, "index": index, "persona": persona,
            "ip": loopback_address(index), "client": client, "online": online, "joined": joined,
            "rng": random.Random(seed + index * 1009), "cooldowns": {}, "follows": set(),
            "contacts": [contact for contact in contacts if contact != username], "analyses": 0,
            "extra": {"analyze": 0, "publish": 0}, "script": deque(), "current": ""}


class World:
    """État partagé de la sandbox, tel que les acteurs le découvrent : comptes, publications et commentaires."""

    def __init__(self, base_url="", run_id="", password="", operations=None, company=None, seed=42):
        self.base_url, self.run_id, self.password, self.seed = base_url, run_id, password, seed
        self.operations = operations or {}
        self.company = company
        self.rng = random.Random(f"{seed}:world")
        self.lock = threading.Lock()
        self.actors = []
        self.usernames = []
        self.posts = []
        self.comments = {}
        self.comment_count = 0
        self.next_index = 0
        self.signup_numbers = itertools.count(1)
        self.stats = {"signups": 0, "reports": 0, "moderated": 0, "follows": 0, "unfollows": 0,
                      "arrivals": 0, "departures": 0, "arrival_backlog": 0}

    def count(self, key, amount=1):
        with self.lock:
            self.stats[key] += amount

    def add_actor(self, actor):
        with self.lock:
            self.actors.append(actor)
            self.usernames.append(actor["username"])
            self.next_index = max(self.next_index, actor["index"] + 1)

    def add_post(self, post_id, author, title):
        with self.lock:
            self.posts.append({"id": post_id, "author": author, "title": title})

    def add_comment(self, post_id, comment_id, author, body):
        with self.lock:
            items = self.comments.setdefault(post_id, {})
            if comment_id not in items:
                self.comment_count += 1
            items[comment_id] = {"id": comment_id, "author": author, "body": body[:200]}

    def pick_post(self, rng):
        with self.lock:
            return rng.choice(self.posts[-300:]) if self.posts else None

    def pick_comment(self, rng):
        with self.lock:
            candidates = [post for post in self.posts[-300:] if self.comments.get(post["id"])]
            if not candidates:
                return None, None
            post = rng.choice(candidates)
            return post, rng.choice(list(self.comments[post["id"]].values()))

    def pick_user(self, rng, exclude):
        with self.lock:
            if len(self.usernames) < 2:
                return None
            while True:
                username = rng.choice(self.usernames)
                if username != exclude:
                    return username


def run_population(world, duration_seconds, concurrency, metrics, cancel_file=None, pace="realistic",
                   ramp_seconds=10, signups_per_minute=0, visit_percent=100):
    """Fait vivre la population : arrivées, actions, départs, inscriptions et modération, sans session partagée."""
    if not world.actors:
        return False
    started = time.monotonic()
    deadline = started + duration_seconds
    delays = {"realistic": (15, 25), "load": (1, 3)}[pace]
    away = {"realistic": (120, 600), "load": (20, 90)}[pace]
    moderation_every = {"realistic": 30, "load": 10}[pace]
    ramp = min(ramp_seconds, duration_seconds / 2)
    arrival_window = max(ramp, duration_seconds)
    # Les connexions (hachage coûteux) ont une voie réservée pour ne pas affamer les comptes déjà en ligne.
    arrival_slots = max(1, concurrency // 4)
    order = itertools.count()
    queue = []
    arrivals = []

    def schedule(at, kind, actor=None):
        heapq.heappush(arrivals if kind == "actor" and not actor["online"] else queue, (at, next(order), kind, actor))

    online = [actor for actor in world.actors if actor["online"]]
    for position, actor in enumerate(online):
        schedule(started + ramp * position / max(1, len(online) - 1), "actor", actor)
    for actor in world.actors:
        if not actor["online"] and actor["rng"].random() * 100 < visit_percent:
            schedule(started + actor["rng"].uniform(0, arrival_window), "actor", actor)
    if signups_per_minute:
        schedule(started + 60 / signups_per_minute, "signup")
    if world.company:
        schedule(started + moderation_every, "moderation")

    def available(actor, action):
        if action in ("view_post", "comment", "react", "verdict", "report"):
            return bool(world.posts)
        if action in ("reply", "react_comment"):
            return world.comment_count > 0
        if action == "analyze":
            return ("analyze" in world.operations and actor["extra"]["analyze"] < 2
                    and not actor.get("analysis_pending"))
        if action == "publish":
            return "publish" in world.operations and bool(actor.get("analysis_pending"))
        if action == "unfollow":
            return bool(actor["follows"])
        if action in ("send", "thread"):
            return bool(actor["contacts"])
        if action in ("follow", "profile_view"):
            return len(world.usernames) > 1
        return True

    def choose(actor):
        now = time.monotonic()
        while actor["script"]:
            action = actor["script"].popleft()
            if available(actor, action):
                return action
        weights = PERSONA_WEIGHTS[actor["persona"]]
        candidates, chances = [], []
        for action in ACTIONS:
            group = COOLDOWN_GROUPS.get(action, action)
            if actor["cooldowns"].get(group, 0) <= now and available(actor, action):
                candidates.append(action)
                chances.append(weights.get(action, 1))
        action = actor["rng"].choices(candidates, chances)[0] if candidates else "browse"
        group = COOLDOWN_GROUPS.get(action, action)
        actor["cooldowns"][group] = now + COOLDOWNS.get(group, 0)
        return action

    def arrive(actor):
        if actor["client"] is None:
            actor["client"] = ApiClient(world.base_url, actor["ip"])
        perform(metrics, actor["client"], "auth.login", "POST", "/api/auth/login",
                {"login": actor["username"], "password": actor["password"]})
        state = perform(metrics, actor["client"], "account.me", "GET", "/api/me")
        require((state.get("user") or {}).get("username") == actor["username"], "La session appartient à un autre compte.")
        actor["online"] = True
        world.count("arrivals")
        metrics.actor_event(actor["username"], "Se connecte", f"Arrivée depuis {actor['ip']}.", kind="session",
                            content=f"Connexion par /api/auth/login depuis l'adresse {actor['ip']}.", status="En ligne")

    def leave(actor):
        perform(metrics, actor["client"], "auth.logout", "POST", "/api/auth/logout", {})
        state = perform(metrics, actor["client"], "auth.logged_out", "GET", "/api/me")
        require(state.get("user") is None, "La session reste active après déconnexion.")
        actor["online"] = False
        world.count("departures")
        metrics.actor_event(actor["username"], "Se déconnecte", "Fin de la visite.", kind="session",
                            content="Déconnexion, puis vérification que l'ancienne session est refusée.", status="Hors ligne")

    def signup():
        number = next(world.signup_numbers)
        with world.lock:
            index = world.next_index
            world.next_index += 1
        persona = PERSONAS[world.rng.randrange(len(PERSONAS))]
        username = f"new_{world.run_id}_{number:04d}"
        actor = make_actor(username, world.password, index, persona, world.seed, online=True, joined="signup")
        actor["client"] = ApiClient(world.base_url, actor["ip"])
        result = perform(metrics, actor["client"], "auth.signup", "POST", "/api/auth/register",
                         {"username": username, "email": f"{username}@example.test", "password": world.password}, (201,))
        require(result.get("user", {}).get("role") == "user", "Un nouvel inscrit a reçu des droits élevés.")
        actor["contacts"] = list(dict.fromkeys(filter(None, (world.pick_user(actor["rng"], username) for _ in range(3)))))
        actor["script"].extend(("profile", "follow", "browse", "view_post"))
        metrics.add_actor(username, persona, joined="signup", status="En ligne")
        metrics.actor_event(username, "S'inscrit sur Rhetora", f"Nouveau compte depuis {actor['ip']}.", kind="signup",
                            content=f"Inscription par /api/auth/register depuis {actor['ip']}.\nProfil d'usage : {persona}.",
                            status="En ligne")
        world.count("signups")
        world.add_actor(actor)
        return actor

    def moderate():
        username, client = world.company
        data = perform(metrics, client, "moderation.reports", "GET", "/api/admin/reports")
        for report in [item for item in data.get("reports", []) if not item.get("resolved")][:5]:
            perform(metrics, client, "moderation.resolve", "PATCH", f"/api/admin/reports/{report['id']}", {"resolved": True})
            world.count("moderated")
            target = "publication" if report.get("type") == "post" else "commentaire"
            metrics.actor_event(username, "Traite un signalement", f"#{report['id']} · {report.get('reason', '')}",
                                report.get("reporter", ""), kind="moderation", post_id=report.get("post_id"),
                                content=(f"Signalé par : {report.get('reporter')}\nMotif : {report.get('reason')}\n"
                                         f"Contenu ({target}) de {report.get('author', '?')} :\n{report.get('excerpt', '')}\n\n"
                                         "Décision : signalement examiné et classé."))

    def act(actor, action):
        username, client, rng = actor["username"], actor["client"], actor["rng"]
        if action == "browse":
            sort = rng.choice(("recent", "top", "discussed", "foryou"))
            feed = perform(metrics, client, "community.feed", "GET", f"/api/community/posts?page=1&sort={sort}")
            posts = feed.get("posts", [])
            metrics.actor_event(username, "Parcourt le fil", f"Tri « {sort} » · {len(posts)} publication(s).", kind="feed",
                                content="\n".join(f"#{post.get('id')} · {post.get('title', '')} — "
                                                  f"{(post.get('author') or {}).get('username', '')}" for post in posts[:8]))
        elif action in ("view_post", "verdict"):
            post = world.pick_post(rng)
            data = perform(metrics, client, "community.post", "GET", f"/api/community/posts/{post['id']}")
            comments = data.get("comments", [])
            for comment in comments[-30:]:
                world.add_comment(post["id"], comment["id"], comment["author"]["username"], comment.get("body", ""))
            if action == "view_post":
                metrics.actor_event(username, "Lit une publication", f"{post['title']} · {len(comments)} commentaire(s).",
                                    post["author"], kind="view", post_id=post["id"], title=post["title"],
                                    content="\n".join(f"{comment['author']['username']} : {comment.get('body', '')}"
                                                      for comment in comments[-6:]) or "Aucun commentaire pour l'instant.")
            else:
                choice = rng.choice(data["verdict"]["options"])
                result = perform(metrics, client, "community.verdict", "POST", f"/api/community/posts/{post['id']}/verdict",
                                 {"choice": choice["id"]})
                metrics.actor_event(username, "Vote pour un verdict", choice["label"], post["author"], kind="verdict",
                                    post_id=post["id"], title=post["title"],
                                    content=f"Vote : {choice['label']}\nVotes sur cette publication : "
                                            f"{result.get('verdict', {}).get('total', '?')}")
        elif action == "comment":
            post, text = world.pick_post(rng), rng.choice(COMMENTS)
            result = perform(metrics, client, "community.comment", "POST", f"/api/community/posts/{post['id']}/comments",
                             {"body": text}, (201,))
            world.add_comment(post["id"], result["id"], username, text)
            metrics.actor_event(username, "Commente une publication", text, post["author"], kind="comment",
                                content=text, post_id=post["id"], title=post["title"])
        elif action == "reply":
            post, parent = world.pick_comment(rng)
            text = rng.choice(REPLIES)
            result = perform(metrics, client, "community.reply", "POST", f"/api/community/posts/{post['id']}/comments",
                             {"body": text, "parent_id": parent["id"]}, (201,))
            world.add_comment(post["id"], result["id"], username, text)
            metrics.actor_event(username, "Répond à un commentaire", text, parent["author"], kind="reply",
                                post_id=post["id"], title=post["title"],
                                content=f"En réponse à {parent['author']} : « {parent['body']} »\n\n{text}")
        elif action in ("react", "react_comment"):
            kind = rng.choice(("like", "think", "love"))
            post, comment = (world.pick_post(rng), None) if action == "react" else world.pick_comment(rng)
            target = {"type": "post", "id": post["id"]} if comment is None else {"type": "comment", "id": comment["id"]}
            perform(metrics, client, f"community.{action}", "POST", "/api/community/react", target | {"kind": kind})
            metrics.actor_event(username, "Réagit à une publication" if comment is None else "Réagit à un commentaire", kind,
                                post["author"] if comment is None else comment["author"], kind="reaction",
                                post_id=post["id"], title=post["title"],
                                content=f"Réaction « {kind} »" + (f" au commentaire : {comment['body']}" if comment else ""))
        elif action in ("follow", "unfollow"):
            target = (world.pick_user(rng, username) if action == "follow"
                      else rng.choice(sorted(actor["follows"])))
            result = perform(metrics, client, f"social.{action}", "POST",
                             f"/api/community/users/{urllib.parse.quote(target, safe='')}/follow", {"follow": action == "follow"})
            (actor["follows"].add if action == "follow" else actor["follows"].discard)(target)
            world.count("follows" if action == "follow" else "unfollows")
            metrics.actor_event(username, "S'abonne à un membre" if action == "follow" else "Se désabonne d'un membre",
                                target, target, kind="follow",
                                content=f"{target} compte maintenant {result.get('followers', '?')} abonné(s).\n"
                                        f"{username} suit {len(actor['follows'])} compte(s) dans cette simulation.")
        elif action == "send":
            peer, text = rng.choice(actor["contacts"]), rng.choice(MESSAGES)
            perform(metrics, client, "messages.send", "POST", f"/api/me/messages/{urllib.parse.quote(peer, safe='')}", {"body": text})
            metrics.actor_event(username, "Envoie un message privé", text, peer, kind="message", content=text)
        elif action == "inbox":
            inbox = perform(metrics, client, "messages.inbox", "GET", "/api/me/messages")
            conversations = inbox.get("conversations", [])
            metrics.actor_event(username, "Consulte sa boîte de réception",
                                f"{len(conversations)} conversation(s), {inbox.get('unread', 0)} non lue(s).", kind="inbox",
                                content="\n".join(f"{item['user']['username']} : {item['last']['body']}"
                                                  for item in conversations[:8]))
        elif action == "thread":
            peer = rng.choice(actor["contacts"])
            thread = perform(metrics, client, "messages.thread", "GET", f"/api/me/messages/{urllib.parse.quote(peer, safe='')}")
            record_thread(metrics, username, peer, thread)
        elif action == "notifications":
            data = perform(metrics, client, "account.notifications", "GET", "/api/me/notifications")
            if data.get("unread"):
                perform(metrics, client, "account.notifications_read", "POST", "/api/me/notifications/read", {"all": True})
            metrics.actor_event(username, "Consulte ses notifications", f"{data.get('unread', 0)} non lue(s).",
                                kind="notification",
                                content="\n".join(f"{'' if item.get('seen') else '• '}{item.get('title', '')}"
                                                  for item in data.get("notifications", [])[:8]) or "Aucune notification.")
        elif action == "search":
            term = rng.choice(SEARCH_TERMS)
            data = perform(metrics, client, "community.search", "GET", f"/api/community/users?q={urllib.parse.quote(term)}")
            people = data.get("users", [])
            metrics.actor_event(username, "Recherche des membres", f"« {term} » · {len(people)} résultat(s).", kind="search",
                                content="\n".join(f"{person['username']} · {person.get('followers', 0)} abonné(s)"
                                                  for person in people[:8]))
        elif action == "explore":
            data = perform(metrics, client, "community.explore", "GET", "/api/community/explore")
            stats = data.get("stats", {})
            metrics.actor_event(username, "Explore la communauté",
                                f"{stats.get('members', '?')} membres · {stats.get('posts', '?')} publications.", kind="explore",
                                content=f"Tendances : {', '.join(item['word'] for item in data.get('trending', [])[:6])}\n"
                                        f"Suggestions : {', '.join(person.get('username', '') for person in data.get('people', [])[:6])}")
        elif action == "leaderboard":
            data = perform(metrics, client, "community.leaderboard", "GET", "/api/community/leaderboard")
            metrics.actor_event(username, "Consulte le classement", f"Rang : {data.get('my_rank', '?')}.", kind="explore",
                                content="\n".join(f"{position}. {item['username']} · {item.get('xp', 0)} XP"
                                                  for position, item in enumerate(data.get("top", [])[:10], 1)))
        elif action == "profile_view":
            target = world.pick_user(rng, username)
            data = perform(metrics, client, "community.profile", "GET", f"/api/community/users/{urllib.parse.quote(target, safe='')}")
            social = data.get("social", {})
            metrics.actor_event(username, "Consulte un profil", target, target, kind="profile",
                                content=f"Bio : {(data.get('user') or {}).get('bio') or '—'}\nAbonnés : {social.get('followers', '?')}"
                                        f" · Abonnements : {social.get('following', '?')} · Rang : {data.get('rank', '?')}")
        elif action == "profile":
            bio = f"{PERSONA_BIOS[actor['persona']]} Compte de simulation {username[-4:]}."
            perform(metrics, client, "account.profile", "PATCH", "/api/me", {"bio": bio})
            metrics.actor_event(username, "Met à jour son profil", bio, kind="profile", content=bio)
        elif action == "analyze":
            actor.update(world.operations["analyze"](actor))
            actor["analysis_pending"] = True
            actor["extra"]["analyze"] += 1
        elif action == "publish":
            actor.update(world.operations["publish"](actor))
            actor["analysis_pending"] = False
            actor["extra"]["publish"] += 1
            world.add_post(actor["post_id"], username, actor["post_title"])
        elif action == "report":
            reason = rng.choice(REPORT_REASONS)
            post, comment = world.pick_comment(rng) if rng.random() < 0.5 else (None, None)
            if comment is None:
                post = world.pick_post(rng)
            target, author = (("comment", comment["id"]), comment["author"]) if comment else (("post", post["id"]), post["author"])
            perform(metrics, client, "community.report", "POST", "/api/community/reports",
                    {"type": target[0], "id": target[1], "reason": reason}, (201,))
            world.count("reports")
            metrics.actor_event(username, "Signale un contenu", reason, author, kind="report", post_id=post["id"],
                                title=post["title"],
                                content=f"Contenu signalé ({'commentaire' if comment else 'publication'}) de {author} :\n"
                                        f"{comment['body'] if comment else post['title']}\n\nMotif : {reason}")
        elif action == "leave":
            leave(actor)

    def run_task(kind, actor):
        if kind == "signup":
            return signup()
        if kind == "moderation":
            return moderate()
        if not actor["online"]:
            actor["current"] = "arrive"
            return arrive(actor)
        actor["current"] = choose(actor)
        return act(actor, actor["current"])

    def settle(future, kind, actor, reschedule=True):
        result = None
        try:
            result = future.result()
        except SimulationCanceled:
            return True
        except Exception as error:
            if actor:
                metrics.record_error(f"activity.{actor['current'] or 'actor'}", error)
                metrics.actor_event(actor["username"], "Action en échec", str(error)[:160], kind="error", content=str(error))
            else:
                metrics.record_error(f"activity.{kind}", error)
        if not reschedule:
            return False
        now = time.monotonic()
        if kind == "signup":
            if result:
                schedule(now + result["rng"].uniform(*delays), "actor", result)
            schedule(now + 60 / signups_per_minute * world.rng.uniform(0.5, 1.5), "signup")
        elif kind == "moderation":
            schedule(now + moderation_every, "moderation")
        else:
            schedule(now + actor["rng"].uniform(*(delays if actor["online"] else away)), "actor", actor)
        return False

    canceled = False
    arriving = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {}
        while time.monotonic() < deadline and not canceled:
            if cancel_file and Path(cancel_file).is_file():
                canceled = True
                break
            now = time.monotonic()
            while len(pending) < concurrency:
                arrival_due = bool(arrivals) and arrivals[0][0] <= now
                if arrival_due and arriving < arrival_slots:
                    _, _, kind, actor = heapq.heappop(arrivals)
                    arriving += 1
                elif (queue and queue[0][0] <= now
                      and len(pending) - arriving < concurrency - (arrival_slots if arrival_due else 0)):
                    _, _, kind, actor = heapq.heappop(queue)
                else:
                    break
                pending[executor.submit(run_task, kind, actor)] = (kind, actor, actor is not None and not actor["online"])
            if not pending:
                upcoming = min([items[0][0] for items in (queue, arrivals) if items], default=now + 0.1)
                time.sleep(min(0.1, max(0.001, upcoming - now), max(0.001, deadline - now)))
                continue
            finished, _ = concurrent.futures.wait(pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in finished:
                kind, actor, was_arrival = pending.pop(future)
                arriving -= was_arrival
                canceled = settle(future, kind, actor) or canceled
        for future, (kind, actor, _) in pending.items():
            canceled = settle(future, kind, actor, reschedule=False) or canceled
    world.stats["arrival_backlog"] = sum(1 for at, *_ in arrivals if at <= deadline - 10)
    return canceled or bool(cancel_file and Path(cancel_file).is_file())


PERSONA_BIOS = {
    "creator": "Je publie des analyses de débats publics.",
    "community": "J'aime commenter et confronter les arguments.",
    "messages": "Je préfère les discussions en privé.",
    "reader": "Je lis surtout les analyses de la communauté.",
    "complete": "J'analyse, je commente, je vote et j'échange avec les membres.",
}


def record_thread(metrics, username, peer, thread):
    messages = thread.get("messages", []) if isinstance(thread, dict) else []
    metrics.actor_event(username, "Lit une conversation", f"{len(messages)} message(s) dans le fil.", peer,
                        kind="conversation",
                        content="\n".join(f"{username if message.get('mine') else peer} : {message.get('body', '')}"
                                          for message in messages[-8:]))
    return messages


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Recommendation(Exception):
    """Aucune faille exploitable constatée, mais un renforcement est recommandé."""


def audit_check(metrics, role, name, worker):
    metrics.agent_event(role, name, "running", "Contrôle en cours…")
    try:
        detail = worker()
        metrics.check(role, name, True, detail or "Contrôle réussi.")
    except SimulationCanceled:
        raise
    except Recommendation as advice:
        metrics.check(role, name, False, str(advice), warning=True)
    except Exception as error:
        metrics.check(role, name, False, f"{type(error).__name__}: {error}")


def raw_get(metrics, base_url, action, path, expected=(200,)):
    """GET envoyé tel quel (chemins non normalisés) avec en-têtes et corps bruts."""
    parts = urllib.parse.urlsplit(base_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=15)
    started = time.perf_counter()
    try:
        connection.putrequest("GET", path, skip_accept_encoding=True)
        connection.putheader("User-Agent", "RhetoraSecurityAgent/1.0")
        connection.endheaders()
        response = connection.getresponse()
        status, headers, body = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read(1_000_000)
    except Exception as error:
        status, headers, body = 0, {}, f"{type(error).__name__}: {error}".encode()
    finally:
        connection.close()
    message = "" if status in expected else body[:240].decode("utf-8", errors="replace")
    metrics.record(action, status, (time.perf_counter() - started) * 1000, message, expected)
    require(status in expected, f"{path} : HTTP {status} au lieu de {'/'.join(map(str, expected))}.")
    return status, headers, body


def run_security_agents(base_url, metrics, members, run_id, cancel_file=None):
    """Agents offensifs : en-têtes, fichiers protégés, XSS, IDOR et injection SQL sur la sandbox."""
    outsider, owner, third = members
    anonymous = ApiClient(base_url)
    payload = f'<script>alert("{run_id}")</script><img src=x onerror=alert(1)>'
    context = {}

    def headers():
        problems = []
        for path in ("/arbitre.html", "/api/me"):
            _, found, _ = raw_get(metrics, base_url, "security.headers", path)
            if found.get("x-content-type-options", "").lower() != "nosniff":
                problems.append(f"{path} : X-Content-Type-Options absent")
            if found.get("x-frame-options", "").upper() not in ("DENY", "SAMEORIGIN"):
                problems.append(f"{path} : X-Frame-Options absent")
            if not found.get("referrer-policy"):
                problems.append(f"{path} : Referrer-Policy absent")
            if path.startswith("/api/") and not found.get("content-type", "").startswith("application/json"):
                problems.append(f"{path} : réponse API non JSON")
            if path.startswith("/api/") and "no-store" not in found.get("cache-control", ""):
                problems.append(f"{path} : réponse API mise en cache")
        require(not problems, "; ".join(problems))
        return "nosniff, X-Frame-Options et Referrer-Policy présents; l'API répond en JSON non mis en cache."

    def content_security_policy():
        _, found, _ = raw_get(metrics, base_url, "security.csp", "/arbitre.html")
        policy = found.get("content-security-policy", "")
        if not policy:
            raise Recommendation("Aucune Content-Security-Policy sur les pages HTML. Une politique (object-src 'none', "
                                 "frame-ancestors 'self', script-src restreint) limiterait l'impact d'une injection.")
        return f"Politique présente : {policy[:200]}"

    def protected_files():
        for path in PROTECTED_PATHS:
            _, _, body = raw_get(metrics, base_url, "security.protected_file", path, (400, 403, 404))
            require(not body.startswith(b"SQLite format") and b"import " not in body, f"{path} : contenu sensible exposé.")
        return (f"{len(PROTECTED_PATHS)} chemins sensibles refusés : base SQLite, sources Python, rapports, .git, "
                "traversée ../ encodée.")

    def reflected_xss():
        for path in ("/<script>rh</script>.html", "/api/community/users/%3Cscript%3Erh%3C%2Fscript%3E",
                     "/api/jobs/<script>rh</script>"):
            _, _, body = raw_get(metrics, base_url, "security.reflected_xss", path, (400, 404))
            require(b"<script>rh" not in body, f"{path} : la page d'erreur renvoie le script injecté.")
        return "Les pages d'erreur et l'API ne renvoient pas le script injecté dans l'URL."

    def stored_xss():
        perform(metrics, owner["client"], "security.xss_bio", "PATCH", "/api/me", {"bio": payload})
        post = perform(metrics, owner["client"], "security.xss_post", "POST", "/api/community/posts", {
            "title": f"Audit {payload}", "comment": payload,
            "snapshot": {"topic": "Audit sécurité", "analysis": {"synthese": payload, "orateurs": []}},
        }, (201,))
        context["post_id"] = post["id"]
        perform(metrics, third["client"], "security.xss_comment", "POST", f"/api/community/posts/{post['id']}/comments",
                {"body": payload}, (201,))
        perform(metrics, owner["client"], "security.xss_message", "POST", f"/api/me/messages/{third['username']}",
                {"body": payload})
        _, profile_headers, profile_body = raw_get(metrics, base_url, "security.xss_read",
                                                   f"/api/community/users/{urllib.parse.quote(owner['username'])}")
        _, post_headers, post_body = raw_get(metrics, base_url, "security.xss_read", f"/api/community/posts/{post['id']}")
        for found in (profile_headers, post_headers):
            require(found.get("content-type", "").startswith("application/json")
                    and found.get("x-content-type-options") == "nosniff", "Contenu utilisateur servi sans type JSON strict.")
        require(json.loads(profile_body)["user"]["bio"] == payload, "La bio piégée a été altérée ou perdue.")
        require(any(comment.get("body") == payload for comment in json.loads(post_body)["comments"]),
                "Le commentaire piégé a été altéré ou perdu.")
        thread = perform(metrics, third["client"], "security.xss_read", "GET", f"/api/me/messages/{owner['username']}")
        require(any(message.get("body") == payload for message in thread.get("messages", [])),
                "Le message piégé a été altéré ou perdu.")
        return ("Charges <script> et onerror conservées comme texte inerte (bio, publication, commentaire, message) et "
                "servies en JSON + nosniff. L'affichage dépend d'escapeHtml dans le site; le rendu navigateur n'est pas exécuté.")

    def idor_trash():
        require(context.get("post_id"), "Préparation absente : publication d'audit non créée.")
        draft = perform(metrics, owner["client"], "security.idor_comment", "POST",
                        f"/api/community/posts/{context['post_id']}/comments", {"body": f"Brouillon {run_id}"}, (201,))
        trash_id = perform(metrics, owner["client"], "security.idor_delete_own", "DELETE",
                           f"/api/community/comments/{draft['id']}")["trash_id"]

        def owner_has_item():
            items = perform(metrics, owner["client"], "security.idor_trash_owner", "GET", "/api/me/trash")["items"]
            return any(item["id"] == trash_id for item in items)

        require(owner_has_item(), "L'élément supprimé n'apparaît pas dans la corbeille de son auteur.")
        foreign = perform(metrics, outsider["client"], "security.idor_trash_list", "GET", "/api/me/trash")["items"]
        require(all(item["id"] != trash_id for item in foreign), "Un tiers voit la corbeille d'un autre compte.")
        perform(metrics, outsider["client"], "security.idor_trash_restore", "POST", f"/api/me/trash/{trash_id}/restore", {}, (404,))
        perform(metrics, outsider["client"], "security.idor_trash_purge", "DELETE", f"/api/me/trash/{trash_id}", expected=(404,))
        require(owner_has_item(), "Un tiers a modifié la corbeille d'un autre compte.")
        return f"Corbeille #{trash_id} : invisible, non restaurable et non supprimable par un autre compte."

    def idor_notifications():
        require(context.get("post_id"), "Préparation absente : publication d'audit non créée.")

        def owner_notifications():
            return perform(metrics, owner["client"], "security.idor_notifications_owner", "GET",
                           "/api/me/notifications")["notifications"]

        target = next((item for item in owner_notifications()
                       if item.get("post_id") == context["post_id"] and not item.get("seen")), None)
        require(target, "L'auteur n'a reçu aucune notification pour le commentaire d'audit.")
        foreign = perform(metrics, outsider["client"], "security.idor_notifications_list", "GET",
                          "/api/me/notifications")["notifications"]
        require(all(item["id"] != target["id"] for item in foreign), "Un tiers voit les notifications d'un autre compte.")
        perform(metrics, outsider["client"], "security.idor_notifications_read", "POST", "/api/me/notifications/read",
                {"ids": [target["id"]]})
        perform(metrics, outsider["client"], "security.idor_notifications_delete", "DELETE",
                f"/api/me/notifications/{target['id']}")
        kept = next((item for item in owner_notifications() if item["id"] == target["id"]), None)
        require(kept and not kept.get("seen"), "Un tiers a lu ou supprimé la notification d'un autre compte.")
        return f"Notification #{target['id']} : invisible, non lue et non supprimée par un autre compte."

    def idor_jobs():
        job = perform(metrics, owner["client"], "security.idor_job_submit", "POST", "/api/analyze-text", {
            "transcript": TOPICS[0][1], "topic": "Audit IDOR", "skipMap": True}, (202,))["jobId"]
        raw_get(metrics, base_url, "security.idor_job_guess", f"/api/jobs/{secrets.token_hex(16)}", (404,))
        status, _, _ = raw_get(metrics, base_url, "security.idor_job_foreign", f"/api/jobs/{job}", (200, 404))
        if status == 200:
            raise Recommendation("Un identifiant inventé est refusé, mais le résultat d'une analyse reste lisible sans session "
                                 "par quiconque obtient son identifiant de tâche (128 bits aléatoires). Lier /api/jobs/<id> au "
                                 "compte demandeur fermerait cet accès.")
        return "Les tâches d'analyse sont réservées au compte demandeur."

    def sql_injection():
        probe = ApiClient(base_url, "127.254.0.2")
        for text in SQL_PROBES:
            query = urllib.parse.quote(text)
            users = perform(metrics, probe, "security.sql_users", "GET", f"/api/community/users?q={query}")
            require(not users.get("users"), f"Recherche de membres « {text} » : filtre contourné.")
            posts = perform(metrics, probe, "security.sql_posts", "GET", f"/api/community/posts?q={query}")
            require(not posts.get("posts"), f"Recherche de publications « {text} » : filtre contourné.")
        perform(metrics, probe, "security.sql_login", "POST", "/api/auth/login",
                {"login": "' OR '1'='1' --", "password": "' OR '1'='1"}, (400, 401))
        return (f"{len(SQL_PROBES)} charges traitées comme du texte dans la recherche de membres et de publications; "
                "connexion par injection refusée.")

    for name, worker in (("security_headers", headers), ("content_security_policy", content_security_policy),
                         ("protected_files", protected_files), ("reflected_xss", reflected_xss),
                         ("stored_xss", stored_xss), ("idor_trash", idor_trash),
                         ("idor_notifications", idor_notifications), ("idor_jobs", idor_jobs),
                         ("sql_injection", sql_injection)):
        if cancel_file and Path(cancel_file).is_file():
            raise SimulationCanceled()
        audit_check(metrics, "security", name, worker)


def check_trend(before, after):
    if before is None:
        return "new"
    if after is None:
        return "missing"
    difference = CHECK_RANK.get(after, 0) - CHECK_RANK.get(before, 0)
    return "improved" if difference > 0 else "regressed" if difference < 0 else "stable"


def latency_trend(before, after):
    if not before:
        return "new"
    margin = max(50, before * 0.2)
    return "regressed" if after - before > margin else "improved" if before - after > margin else "stable"


def load_baseline(output_dir, configuration, limit=60):
    """Dernier rapport terminé de même configuration, sinon le plus récent (latences alors non comparées)."""
    fallback = None
    for path in sorted(Path(output_dir).glob("run-*.json"), reverse=True)[:limit]:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if report.get("status") == "canceled" or not report.get("checks"):
            continue
        previous = report.get("configuration", {})
        comparable = all(previous.get(key) == configuration.get(key) for key in COMPARABLE_KEYS)
        if not comparable and fallback:
            continue
        baseline = {
            "report": path.name, "finished_at": report.get("finished_at"), "status": report.get("status"),
            "comparable": comparable,
            "checks": {f"{check['role']}.{check['name']}": check["status"] for check in report["checks"]},
            "p95": {name: result.get("p95_ms", 0) for name, result in report.get("actions", {}).items()},
        }
        if comparable:
            return baseline
        fallback = baseline
    return fallback


def compare_runs(baseline, checks, actions):
    if not baseline:
        return None
    summary = {"improved": 0, "regressed": 0, "stable": 0, "new": 0, "missing": 0}
    current = {f"{check['role']}.{check['name']}": check["status"] for check in checks}
    check_rows = []
    for key in list(current) + [key for key in baseline["checks"] if key not in current]:
        trend = check_trend(baseline["checks"].get(key), current.get(key))
        summary[trend] += 1
        check_rows.append({"check": key, "before": baseline["checks"].get(key), "after": current.get(key), "trend": trend})
    latency_rows = []
    if baseline["comparable"]:
        for name, result in actions.items():
            if name.startswith("security."):
                continue
            before = baseline["p95"].get(name)
            latency_rows.append({"action": name, "before_p95_ms": before, "after_p95_ms": result["p95_ms"],
                                 "trend": latency_trend(before, result["p95_ms"])})
    return {"baseline": {key: baseline[key] for key in ("report", "finished_at", "status", "comparable")},
            "summary": summary, "checks": check_rows, "latency": latency_rows,
            "latency_regressions": sum(row["trend"] == "regressed" for row in latency_rows),
            "latency_improvements": sum(row["trend"] == "improved" for row in latency_rows)}


def run_security_audit(base_url, metrics, actors, password, run_id, cancel_file=None):
    members = []
    for index in range(3):
        if cancel_file and Path(cancel_file).is_file():
            raise SimulationCanceled()
        username = f"audit_{run_id}_{index}"
        client = ApiClient(base_url)
        result = perform(metrics, client, "security.register", "POST", "/api/auth/register",
                         {"username": username, "email": f"{username}@example.test", "password": password}, (201,))
        require(result.get("user", {}).get("role") == "user", "Un compte ordinaire a reçu des droits élevés.")
        members.append({"username": username, "client": client})
    outsider, owner, third = members
    anonymous = ApiClient(base_url)

    def request_check(name, client, method, path, payload=None, expected=(200,), headers=None):
        if cancel_file and Path(cancel_file).is_file():
            raise SimulationCanceled()
        audit_check(metrics, "security", name, lambda: (
            perform(metrics, client, f"security.{name}", method, path, payload, expected, headers) and None))

    request_check("anonymous_messages", anonymous, "GET", "/api/me/messages", expected=(401,))
    request_check("anonymous_admin", anonymous, "GET", "/api/admin/stats", expected=(401,))
    request_check("user_admin", outsider["client"], "GET", "/api/admin/stats", expected=(403,))
    request_check("csrf_header", outsider["client"], "PATCH", "/api/me", {"bio": "refused"}, (403,),
                  {"X-Rhetora": None})
    request_check("csrf_origin", outsider["client"], "PATCH", "/api/me", {"bio": "refused"}, (403,),
                  {"Origin": "https://untrusted.example.test"})
    request_check("invalid_message", outsider["client"], "POST", f"/api/me/messages/{owner['username']}",
                  {"body": ""}, (400,))
    request_check("invalid_registration", anonymous, "POST", "/api/auth/register",
                  {"username": "!", "email": "invalid", "password": "short"}, (400,))
    if actors:
        target = actors[0]
        path = f"/api/community/posts/{target['post_id']}"
        request_check("foreign_delete", outsider["client"], "DELETE", path, expected=(403,))

        def foreign_update():
            before = perform(metrics, outsider["client"], "security.post_before", "GET", path)["post"]
            perform(metrics, outsider["client"], "security.foreign_patch", "PATCH", path,
                    {"title": "Unauthorized change"}, (200, 403))
            after = perform(metrics, outsider["client"], "security.post_after", "GET", path)["post"]
            require(before["title"] == after["title"], "Un tiers a modifié la publication d'un autre compte.")

        audit_check(metrics, "security", "foreign_update_unchanged", foreign_update)

    def privilege_escalation():
        perform(metrics, outsider["client"], "security.role_patch", "PATCH", "/api/me", {"role": "admin"})
        result = perform(metrics, outsider["client"], "security.role_verify", "GET", "/api/me")
        require(result.get("user", {}).get("role") == "user", "Le profil permet une élévation de privilèges.")

    audit_check(metrics, "security", "privilege_escalation", privilege_escalation)

    def private_messages():
        marker = f"private-{run_id}"
        perform(metrics, owner["client"], "security.private_send", "POST",
                f"/api/me/messages/{outsider['username']}", {"body": marker})
        received = perform(metrics, outsider["client"], "security.private_receive", "GET",
                           f"/api/me/messages/{owner['username']}")
        require(any(message.get("body") == marker for message in received.get("messages", [])),
                "Message privé perdu.")
        unrelated = perform(metrics, third["client"], "security.private_isolation", "GET",
                            f"/api/me/messages/{owner['username']}")
        require(not unrelated.get("messages"), "Un tiers voit une conversation privée.")

    audit_check(metrics, "security", "private_message_isolation", private_messages)
    # Avant logout_replay : ce contrôle déconnecte définitivement le compte tiers.
    run_security_agents(base_url, metrics, members, run_id, cancel_file)

    def logout_replay():
        replay = ApiClient(base_url)
        for cookie in third["client"].cookies:
            replay.cookies.set_cookie(cookie)
            require(cookie.has_nonstandard_attr("HttpOnly") and cookie.get_nonstandard_attr("SameSite") == "Lax",
                    "Les protections du cookie de session sont absentes.")
        require(len(replay.cookies) > 0, "Aucun cookie de session à vérifier.")
        perform(metrics, third["client"], "security.logout", "POST", "/api/auth/logout", {})
        result = perform(metrics, replay, "security.session_replay", "GET", "/api/me")
        require(result.get("user") is None, "Un ancien cookie reste utilisable après déconnexion.")

    audit_check(metrics, "security", "logout_revokes_session", logout_replay)

    def registration_limit():
        for index, expected in ((0, (201,)), (1, (429,))):
            username = f"limit_{run_id}_{index}"
            perform(metrics, anonymous, "security.registration_limit", "POST", "/api/auth/register",
                    {"username": username, "email": f"{username}@example.test", "password": password}, expected)

    audit_check(metrics, "security", "registration_rate_limit", registration_limit)

    def login_limit():
        for index in range(9):
            if cancel_file and Path(cancel_file).is_file():
                raise SimulationCanceled()
            perform(metrics, anonymous, "security.login_limit", "POST", "/api/auth/login",
                    {"login": outsider["username"], "password": "wrong-password"}, (401,) if index < 8 else (429,))

    audit_check(metrics, "security", "login_rate_limit", login_limit)


def run_quality_audit(data_dir, metrics, users, published, p95_budget_ms, expected_counts=None):
    def integrity():
        with closing(sqlite3.connect((data_dir / "rhetora.db").as_uri() + "?mode=ro", uri=True)) as connection:
            require(connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "SQLite : intégrité invalide.")
            require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "SQLite : références orphelines.")
            require(connection.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0] == 1,
                    "Le nombre de comptes administrateurs a changé.")
        return "Intégrité SQLite, références et séparation administrateur/utilisateurs vérifiées."

    def population():
        with closing(sqlite3.connect((data_dir / "rhetora.db").as_uri() + "?mode=ro", uri=True)) as connection:
            stored_users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            stored_reports = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            resolved = connection.execute("SELECT COUNT(*) FROM reports WHERE resolved = 1").fetchone()[0]
        require(stored_users == expected_counts["users"],
                f"{stored_users} comptes en base pour {expected_counts['users']} créations confirmées.")
        require(stored_reports == expected_counts["reports"],
                f"{stored_reports} signalements en base pour {expected_counts['reports']} confirmés.")
        require(resolved >= expected_counts["moderated"], "Des signalements traités ne sont pas marqués comme résolus.")
        return (f"{stored_users} comptes, {stored_reports} signalements et {resolved} signalement(s) classé(s) : "
                "aucune inscription ni aucun signalement perdu ou dupliqué.")

    audit_check(metrics, "qa", "database_integrity", integrity)
    if expected_counts:
        audit_check(metrics, "qa", "population_consistency", population)
    metrics.check("qa", "user_journeys", len(published) == users,
                  f"{len(published)}/{users} acteurs de la cohorte initiale ont ouvert une session, analysé et publié.")
    mapped = sum(bool(((actor.get("analysis") or {}).get("carte") or {}).get("arguments")) for actor in published)
    metrics.check("qa", "full_analysis", bool(published) and mapped == len(published),
                  f"{mapped}/{len(published)} analyses publiées contiennent extraits, synthèse et arbre argumentatif.")
    actions = metrics.summary()
    slow = {name: result["p95_ms"] for name, result in actions.items()
            if not name.startswith("security.") and result["p95_ms"] > p95_budget_ms}
    metrics.check("performance", "latency_budget", not slow,
                  f"Budget p95 : {p95_budget_ms} ms. Routes hors budget : {slow or 'aucune'}. Les p95 au-delà de 10 000 mesures sont estimés.")
    failures = sum(result["failures"] for result in actions.values())
    metrics.check("performance", "request_reliability", failures == 0,
                  f"{failures} requête(s) avec un statut inattendu sur {metrics.request_count}.")


def run_company_audit(client, username, actors, metrics):
    def operations():
        stats = perform(metrics, client, "company.statistics", "GET", "/api/admin/stats")
        perform(metrics, client, "company.moderation", "GET", "/api/admin/reports")
        metrics.actor_event(username, "Consulte le tableau de bord", "Statistiques et file de modération.",
                            kind="admin", content=json.dumps(stats, ensure_ascii=False, indent=1)[:2000])
        return "Statistiques et file de modération accessibles à l'équipe d'exploitation."

    audit_check(metrics, "operations", "company_dashboard", operations)
    if actors:
        actor = actors[0]
        user_path = f"/api/me/messages/{actor['username']}"
        company_path = f"/api/me/messages/{username}"

        def support():
            question = "Support sandbox : comment vérifier mon analyse ?"
            answer = "Support Rhetora : votre analyse et votre publication sont disponibles."
            perform(metrics, actor["client"], "company.support_question", "POST", company_path, {"body": question})
            metrics.actor_event(actor["username"], "Contacte le support Rhetora", question, username,
                                kind="message", content=question)
            thread = perform(metrics, client, "company.support_inbox", "GET", user_path)
            require(any(message.get("body") == question for message in thread.get("messages", [])),
                    "L'équipe de support ne reçoit pas la demande utilisateur.")
            perform(metrics, client, "company.support_reply", "POST", user_path, {"body": answer})
            metrics.actor_event(username, "Répond à une demande de support", answer, actor["username"],
                                kind="message", content=answer)
            reply = perform(metrics, actor["client"], "company.support_received", "GET", company_path)
            require(any(message.get("body") == answer for message in reply.get("messages", [])),
                    "L'utilisateur ne reçoit pas la réponse de support.")
            record_thread(metrics, actor["username"], username, reply)

        audit_check(metrics, "operations", "support_round_trip", support)


def stop_server(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def write_report(output_dir, report):
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"run-{stamp}-{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def simulate(users, concurrency, output_dir, status_file=None, duration_seconds=0, mode="balanced", cancel_file=None,
             seed=42, pace="realistic", ramp_seconds=10, p95_budget_ms=2000, keep_open_seconds=0, signups_per_minute=0,
             visit_percent=100):
    metrics = Metrics(status_file, users, concurrency, duration_seconds, mode)
    metrics.status_config.update(seed=seed, pace=pace, ramp_seconds=ramp_seconds, p95_budget_ms=p95_budget_ms,
                                 keep_open_seconds=keep_open_seconds, signups_per_minute=signups_per_minute,
                                 visit_percent=visit_percent)
    metrics.set_baseline(load_baseline(output_dir, metrics.status_config))
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    fatal_error = ""
    canceled = False
    server_log_tail = ""
    fixture_accounts = 0
    report_path = None
    world = None
    view_key = secrets.token_urlsafe(24)

    def finalize(sandbox_open=False):
        final_status = "canceled" if canceled else ("failed" if metrics.error_count else "passed")
        report = {
            "status": final_status,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.perf_counter() - started, 2),
            "configuration": {
                "users": users,
                "concurrency": concurrency,
                "duration_seconds": duration_seconds,
                "mode": mode,
                "seed": seed,
                "pace": pace,
                "ramp_seconds": ramp_seconds,
                "p95_budget_ms": p95_budget_ms,
                "keep_open_seconds": keep_open_seconds,
                "signups_per_minute": signups_per_minute,
                "visit_percent": visit_percent,
                "initial_cohort": min(users, COHORT_SIZE),
                "client_addresses": "une adresse loopback distincte par compte simulé",
                "server": "instance locale sur 127.0.0.1",
                "database": "éphémère et supprimée à la fin",
                "analysis_mode": "tâche d'analyse réelle avec fournisseur déterministe local; aucun appel IA externe",
                "api_registration_count": sum(metrics.action_stats.get(action, {}).get("http_statuses", {}).get("201", 0)
                                              for action in ("auth.register", "company.bootstrap", "security.register",
                                                             "security.registration_limit", "auth.signup")),
                "fixture_accounts": fixture_accounts,
            },
            "population": metrics.population_summary() | (world.stats if world else {}),
            "actions": metrics.summary(),
            "request_count": metrics.request_count,
            "error_count": metrics.error_count,
            "throughput_rps": round(metrics.request_count / max(0.001, time.perf_counter() - started), 2),
            "checks": metrics.checks,
            "audit_team": [
                {"role": role, "status": next((status for status in ("failed", "warning", "passed")
                                               if any(check["role"] == role and check["status"] == status for check in metrics.checks)),
                                              "not_run"),
                 "checks": sum(check["role"] == role for check in metrics.checks)}
                for role in ("operations", "qa", "performance", "security")
            ],
            "audit_summary": metrics.audit_summary(),
            "comparison": compare_runs(metrics.baseline, metrics.checks, metrics.summary()),
            "agent_activity": list(metrics.agent_activity),
            "actors": list(metrics.actors.values()),
            "social_activity": list(metrics.social_activity),
            "social_activity_count": metrics.social_activity_count,
            "errors": list(metrics.errors),
        }
        if fatal_error:
            report["fatal_error"] = fatal_error
        if server_log_tail and (fatal_error or metrics.errors):
            report["sandbox_server_log_tail"] = server_log_tail
        path = write_report(output_dir, report)
        metrics.finish(path, report["status"], sandbox_open)
        return path, report

    with tempfile.TemporaryDirectory(prefix="rhetora-simulation-") as temporary:
        sandbox = Path(temporary)
        data_dir = sandbox / "data"
        data_dir.mkdir()
        log_path = sandbox / "server.log"
        port = available_port()
        base_url = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        for key in list(environment):
            upper = key.upper()
            if "API_KEY" in upper or "APIKEY" in upper or "SECRET" in upper or "PASSWORD" in upper:
                environment.pop(key, None)
        environment.update({
            "RHETORA_DATA_DIR": str(data_dir),
            "ARBITRE_PORT": str(port),
            "ARBITRE_HOST": "127.0.0.1",
            "RHETORA_SIMULATION_MODE": "1",
            "RHETORA_SIMULATION_VIEW_KEY": view_key,
            "GROQ_API_KEY": "gsk_rhetora_simulation_only",
            "PYTHONUTF8": "1",
        })

        process = None
        try:
            metrics.set_phase("Démarrage du serveur isolé")
            print("[1/4] Démarrage du serveur sur une base temporaire isolée...")
            with log_path.open("w", encoding="utf-8") as server_log:
                process = subprocess.Popen(
                    [sys.executable, "-u", str(SERVER)],
                    cwd=str(ROOT),
                    env=environment,
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
                wait_for_server(process, base_url, log_path)

                run_id = uuid.uuid4().hex[:8]
                password = "Sim-" + secrets.token_urlsafe(20) + "-A9!"
                company_username = f"company_{run_id}"
                company_client = ApiClient(base_url)
                metrics.set_sandbox(url=base_url, view_key=view_key, open=True, company=company_username)
                perform(metrics, company_client, "company.bootstrap", "POST", "/api/auth/register",
                    {"username": company_username, "email": f"{company_username}@example.test", "password": password}, (201,))
                actors = []
                width = max(3, len(str(users - 1)))
                usernames = [f"sim_{run_id}_{index:0{width}d}" for index in range(users)]
                first_username = usernames[0]
                first_actor = {"username": first_username, "email": f"{first_username}@example.test", "password": password}
                metrics.set_phase("Préparation des comptes fictifs")
                print("[2/6] Inscription API et préparation des comptes de test...")
                perform(metrics, ApiClient(base_url), "auth.register", "POST", "/api/auth/register", first_actor, (201,))
                seed_accounts(data_dir, usernames[1:], password)
                fixture_accounts = users - 1
                metrics.set_actors(usernames, mode)
                metrics.add_actor(company_username, "company", joined="company", status="En ligne")
                actors = [make_actor(username, password, index, persona_for(index, mode), seed,
                                     contacts=[usernames[(index + step) % users] for step in (1, 2, 3, 5, 8)])
                          for index, username in enumerate(usernames)]
                cohort = actors[:min(users, COHORT_SIZE)]

                def login_actor(actor):
                    client = ApiClient(base_url, actor["ip"])
                    perform(metrics, client, "auth.login", "POST", "/api/auth/login",
                            {"login": actor["username"], "password": actor["password"]})
                    state = perform(metrics, client, "account.me", "GET", "/api/me")
                    require(state.get("user", {}).get("username") == actor["username"], "La session appartient à un autre compte.")
                    require(state["user"].get("role") == "user", "Un utilisateur simulé possède des droits élevés.")
                    actor.update(client=client, online=True)
                    metrics.actor_event(actor["username"], "Se connecte", f"Arrivée depuis {actor['ip']}.", kind="session",
                                        content=f"Connexion par /api/auth/login depuis {actor['ip']}, puis lecture du profil courant.",
                                        status="En ligne")
                    return actor

                metrics.set_phase("Connexions de la cohorte initiale")
                active_actors = run_parallel("3/6 Connexions", cohort, login_actor, concurrency, metrics, cancel_file)
                if not active_actors:
                    raise RuntimeError("Aucun acteur n'a pu ouvrir une session.")

                def analyze_actor(actor):
                    count = actor.get("analyses", 0)
                    topic, transcript = random.Random(f"{seed}:{actor['username']}:{count}").choice(TOPICS)
                    job = perform(metrics, actor["client"], "analysis.submit", "POST", "/api/analyze-text", {
                        "transcript": transcript,
                        "topic": topic,
                    }, (202,))
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        if cancel_file and Path(cancel_file).is_file():
                            raise SimulationCanceled()
                        state = perform(metrics, actor["client"], "analysis.poll", "GET",
                                        f"/api/jobs/{urllib.parse.quote(job['jobId'], safe='')}")
                        if state.get("status") == "done":
                            result = state.get("result")
                            if not isinstance(result, dict) or not result.get("orateurs") or not result.get("synthese"):
                                raise RuntimeError("L'analyse simulée n'a pas produit de résultat exploitable.")
                            if not (result.get("carte") or {}).get("arguments"):
                                raise RuntimeError("L'analyse simulée n'a pas produit d'arbre argumentatif.")
                            metrics.actor_event(actor["username"], "Analyse un débat", f"{topic} · {result['synthese']}",
                                                kind="analysis", title=topic,
                                                content=f"Transcription soumise :\n{transcript}\n\nSynthèse :\n{result['synthese']}")
                            actor.update(analysis=result, analysis_topic=topic, analyses=count + 1)
                            return actor
                        if state.get("status") in ("error", "canceled"):
                            raise RuntimeError(state.get("error") or "La tâche d'analyse a échoué.")
                        time.sleep(0.15)
                    raise RuntimeError("Délai dépassé pendant l'analyse simulée.")

                print("[4/6] Analyses via la vraie file de tâches et le fournisseur déterministe...")
                metrics.set_phase("Analyses déterministes en cours")
                analyzed_actors = run_parallel("analysis", active_actors, analyze_actor, concurrency, metrics, cancel_file)
                if not analyzed_actors:
                    raise RuntimeError("Aucun acteur n'a obtenu de résultat d'analyse.")

                def create_post(actor):
                    username = actor["username"]
                    topic = actor.get("analysis_topic", TOPICS[0][0])
                    snapshot = {
                        "topic": topic,
                        "analyzedAt": int(time.time()),
                        "analysis": actor["analysis"],
                    }
                    title = f"{topic} : analyse de {username[-4:]}"
                    comment = f"Voici mon analyse du débat sur « {topic} ». Quel argument vous convainc le plus ?"
                    result = perform(metrics, actor["client"], "community.publish", "POST", "/api/community/posts", {
                        "title": title,
                        "comment": comment,
                        "snapshot": snapshot,
                    }, (201,))
                    metrics.actor_event(username, "Publie une analyse", title, kind="post", post_id=result["id"], title=title,
                                        content=f"{comment}\n\nSynthèse jointe :\n{snapshot['analysis'].get('synthese', '')}")
                    actor.update(post_id=result["id"], post_title=title)
                    return actor

                print("[5/6] Publication des résultats d'analyse...")
                metrics.set_phase("Publication des résultats d'analyse")
                published = run_parallel("community.publish", analyzed_actors, create_post, concurrency, metrics, cancel_file)
                if not published:
                    raise RuntimeError("Aucune publication de test n'a été créée.")

                def interact(item):
                    index, actor = item
                    target = published[(index + 1) % len(published)]
                    client = actor["client"]
                    rng = random.Random(f"{seed}:{actor['username']}:initial")
                    comment = rng.choice(COMMENTS)
                    perform(metrics, client, "community.comment", "POST",
                            f"/api/community/posts/{target['post_id']}/comments",
                            {"body": comment}, (201,))
                    metrics.actor_event(actor["username"], "Commente une publication", comment, target["username"],
                                        kind="comment", content=comment, post_id=target["post_id"], title=target["post_title"])
                    perform(metrics, client, "community.react", "POST", "/api/community/react",
                            {"type": "post", "id": target["post_id"], "kind": "like"})
                    metrics.actor_event(actor["username"], "Réagit à une publication", "like", target["username"],
                                        kind="reaction", content="Réaction « like »", post_id=target["post_id"],
                                        title=target["post_title"])
                    perform(metrics, client, "community.feed", "GET", "/api/community/posts?page=1")
                    bio = f"Membre de la communauté Rhetora, compte de simulation {actor['username'][-3:]}."
                    perform(metrics, client, "account.profile", "PATCH", "/api/me", {"bio": bio})
                    if len(published) > 1:
                        message = rng.choice(MESSAGES)
                        perform(metrics, client, "messages.send", "POST",
                                f"/api/me/messages/{urllib.parse.quote(target['username'], safe='')}",
                                {"body": message})
                        metrics.actor_event(actor["username"], "Envoie un message privé", message, target["username"],
                                            kind="message", content=message)
                    return {"actor": actor, "target": target}

                print("[6/6] Commentaires, réactions, profils, messages privés et lecture des conversations...")
                metrics.set_phase("Interactions communautaires et messages")
                interactions = run_parallel("community.interactions", list(enumerate(published)), interact,
                                             concurrency, metrics, cancel_file)

                def read_messages(item):
                    actor = item["actor"]
                    index = published.index(actor)
                    sender = published[(index - 1) % len(published)]
                    client = actor["client"]
                    perform(metrics, client, "messages.inbox", "GET", "/api/me/messages")
                    if len(published) > 1:
                        thread = perform(metrics, client, "messages.thread", "GET",
                                         f"/api/me/messages/{urllib.parse.quote(sender['username'], safe='')}")
                        if not any(not message.get("mine") for message in thread.get("messages", [])):
                            raise RuntimeError("Le message entrant n'est pas visible dans la conversation.")
                        record_thread(metrics, actor["username"], sender["username"], thread)

                metrics.set_phase("Lecture des boîtes de réception")
                run_parallel("messages.read", interactions, read_messages, concurrency, metrics, cancel_file)
                world = World(base_url, run_id, password, {"analyze": analyze_actor, "publish": create_post},
                              (company_username, company_client), seed)
                for actor in actors:
                    world.add_actor(actor)
                for actor in published:
                    world.add_post(actor["post_id"], actor["username"], actor["post_title"])
                if duration_seconds:
                    metrics.set_phase(f"Interactions continues · {mode}")
                    print(f"[7/7] Population de {users} compte(s) pendant {duration_seconds} s ({mode})...")
                    canceled = run_population(world, duration_seconds, concurrency, metrics, cancel_file, pace,
                                              ramp_seconds, signups_per_minute, visit_percent)
                    if not canceled:
                        backlog = world.stats["arrival_backlog"]
                        metrics.check("performance", "login_capacity", backlog == 0,
                                      f"{world.stats['arrivals']} connexion(s) servie(s) en {duration_seconds} s "
                                      f"({world.stats['arrivals'] / duration_seconds:.1f}/s); {backlog} arrivée(s) en attente "
                                      "depuis plus de 10 s à la fin. Chaque connexion vérifie un mot de passe PBKDF2 coûteux par conception.")
                else:
                    canceled = bool(cancel_file and Path(cancel_file).is_file())
                if not canceled:
                    metrics.set_phase("Équipe Rhetora : exploitation et support")
                    support_actor = next((actor for actor in world.actors if actor["online"] and actor["client"]), None)
                    if support_actor is None:
                        support_actor = login_actor(published[0])
                    run_company_audit(company_client, company_username, [support_actor], metrics)
                    if cancel_file and Path(cancel_file).is_file():
                        raise SimulationCanceled()
                    metrics.set_phase("Équipe Rhetora : audit de sécurité")
                    run_security_audit(base_url, metrics, published, password, run_id, cancel_file)
                    if cancel_file and Path(cancel_file).is_file():
                        raise SimulationCanceled()
                    metrics.set_phase("Équipe Rhetora : qualité et performance")
                    created = {action: metrics.action_stats.get(action, {}).get("http_statuses", {}).get("201", 0)
                               for action in ("auth.signup", "security.register", "security.registration_limit")}
                    run_quality_audit(data_dir, metrics, len(cohort), published, p95_budget_ms, {
                        "users": 1 + users + sum(created.values()),
                        "reports": world.stats["reports"],
                        "moderated": world.stats["moderated"],
                    })
                    if keep_open_seconds and process.poll() is None:
                        server_log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
                        report_path, report = finalize(sandbox_open=True)
                        print(f"Site sandbox consultable pendant {keep_open_seconds} s : {base_url}/arbitre.html")
                        keep_until = time.monotonic() + keep_open_seconds
                        while (time.monotonic() < keep_until and process.poll() is None
                               and not (cancel_file and Path(cancel_file).is_file())):
                            time.sleep(0.5)
                        metrics.close_sandbox()
        except SimulationCanceled:
            canceled = True
        except Exception as error:
            fatal_error = f"{type(error).__name__}: {error}"
            metrics.record_error("simulation", fatal_error)
            canceled = bool(cancel_file and Path(cancel_file).is_file())
        finally:
            if process:
                stop_server(process)
            if report_path is None and log_path.exists():
                server_log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]

    if report_path is None:
        report_path, report = finalize()
    try:
        display_path = report_path.relative_to(ROOT)
    except ValueError:
        display_path = report_path
    print(f"Résultat: {report['status']} | {sum(item['requests'] for item in report['actions'].values())} requête(s)")
    print(f"Rapport: {display_path}")
    for action, result in report["actions"].items():
        print(f"  {action}: {result['successes']}/{result['requests']} succès, p95 {result['p95_ms']} ms")
    if metrics.errors:
        print(f"Échecs: {metrics.error_count} (100 derniers détails maximum dans le rapport JSON)")
    return 130 if canceled else (1 if metrics.error_count else 0)


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("entrez un nombre entier") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("la valeur doit être supérieure à zéro")
    return parsed


def non_negative_int(value):
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("entrez un nombre entier") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("la valeur ne peut pas être négative")
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Simule des parcours communautaires dans un Rhetora isolé.")
    parser.add_argument("--users", type=positive_int, default=10, help=f"nombre de comptes simulés (maximum {MAX_USERS})")
    parser.add_argument("--concurrency", type=positive_int, default=5, help=f"nombre de requêtes simultanées (maximum {MAX_CONCURRENCY})")
    parser.add_argument("--duration-seconds", type=non_negative_int, default=0, help="durée des interactions continues (0 à 1200 s)")
    parser.add_argument("--mode", choices=MODES, default="balanced")
    parser.add_argument("--signups-per-minute", type=non_negative_int, default=0,
                        help="nouvelles inscriptions pendant l'activité, chacune depuis sa propre adresse (maximum 600)")
    parser.add_argument("--visit-percent", type=positive_int, default=100,
                        help="part des comptes hors cohorte qui se connectent pendant l'activité (1 à 100)")
    parser.add_argument("--seed", type=non_negative_int, default=42, help="graine des décisions par acteur")
    parser.add_argument("--pace", choices=("realistic", "load"), default="realistic", help="rythme : 15-25 s ou 1-3 s par acteur")
    parser.add_argument("--ramp-seconds", type=non_negative_int, default=10, help="montée progressive de la phase active (maximum 300 s)")
    parser.add_argument("--p95-budget-ms", type=positive_int, default=2000, help="seuil de latence p95 par action hors sondes de sécurité")
    parser.add_argument("--keep-open-seconds", type=non_negative_int, default=0,
                        help="garder le site sandbox consultable après le rapport (maximum 3600 s)")
    parser.add_argument("--output", type=Path, default=ROOT / "simulation-results", help="dossier des rapports JSON")
    parser.add_argument("--status-file", type=Path, help="fichier d'état actualisé pour la console locale")
    parser.add_argument("--cancel-file", type=Path, help="fichier sentinelle demandant l'arrêt propre")
    args = parser.parse_args()
    if args.users > MAX_USERS:
        parser.error(f"--users est limité à {MAX_USERS}")
    if args.concurrency > min(args.users, MAX_CONCURRENCY):
        parser.error(f"--concurrency doit être inférieur ou égal au nombre d'utilisateurs et à {MAX_CONCURRENCY}")
    if args.signups_per_minute > 600:
        parser.error("--signups-per-minute est limité à 600")
    if args.visit_percent > 100:
        parser.error("--visit-percent est limité à 100")
    if args.duration_seconds > 1200:
        parser.error("--duration-seconds est limité à 1200 (20 minutes)")
    if args.ramp_seconds > 300:
        parser.error("--ramp-seconds est limité à 300")
    if args.keep_open_seconds > 3600:
        parser.error("--keep-open-seconds est limité à 3600")
    if not SERVER.is_file():
        parser.error(f"serveur introuvable : {SERVER}")
    status_file = args.status_file.resolve() if args.status_file else None
    cancel_file = args.cancel_file.resolve() if args.cancel_file else None
    return simulate(args.users, args.concurrency, args.output.resolve(), status_file,
                    args.duration_seconds, args.mode, cancel_file, args.seed, args.pace, args.ramp_seconds, args.p95_budget_ms,
                    args.keep_open_seconds, args.signups_per_minute, args.visit_percent)


if __name__ == "__main__":
    raise SystemExit(main())