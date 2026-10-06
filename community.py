"""rhetora — comptes, espace communautaire, défis et administration.

Stockage SQLite (bibliothèque standard) dans data/rhetora.db : ce dossier n'est jamais servi par le serveur HTTP.
Sécurité : mots de passe PBKDF2-SHA256 salés, jetons de session aléatoires stockés hachés (cookie HttpOnly,
SameSite=Lax), en-tête X-Rhetora + contrôle d'Origin sur toute requête modifiante (anti-CSRF), limitation des
tentatives, rôles vérifiés côté serveur (user < moderator < admin). Le premier compte créé devient administrateur.
"""
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import traceback
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from http.cookies import CookieError, SimpleCookie

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "rhetora.db")
COOKIE = "rh_session"
SESSION_TTL = 30 * 86400
PBKDF2_ROUNDS = 310_000
MAX_BODY = 3_000_000
MAX_SNAPSHOT = 2_000_000
PAGE = 20
PREFIXES = ("/api/auth/", "/api/me", "/api/community/", "/api/admin/", "/api/ads/", "/api/media/")
SUDO_TTL = 600  # fenêtre de confiance après re-saisie du mot de passe (actions sensibles)
IMAGE_MAX = 1_600_000
LIVE_STALE = 90  # un direct sans nouvelles de l'hôte depuis 90 s est considéré comme terminé
ROLES = ("user", "moderator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
TONES = ("blue", "green", "yellow", "red", "purple", "teal")
PLACEMENTS = ("all", "feed", "home", "post")
USERNAME_RE = re.compile(r"^[A-Za-z0-9À-ÖØ-öø-ÿ_.-]{3,24}$")
MEDIA_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,24}")
EMAIL_RE = re.compile(r"^[^@\s<>\"]{1,64}@[^@\s<>\"]{1,190}\.[A-Za-z]{2,24}$")
LEVELS = [(0, "Auditeur"), (50, "Curieux"), (150, "Contradicteur"), (300, "Rhéteur"), (500, "Dialecticien"), (800, "Logicien"),
          (1200, "Orateur"), (1800, "Arbitre"), (2600, "Grand arbitre"), (3600, "Maître de la disputatio")]
FALLACIES = ["Ad hominem", "Homme de paille", "Fausse alternative", "Pente glissante", "Appel à l'autorité", "Appel à la popularité",
             "Appel à l'émotion", "Généralisation hâtive", "Corrélation n'est pas causalité", "Raisonnement circulaire", "Diversion",
             "Tu quoque", "Renversement de la charge de la preuve", "Appel à la tradition", "Appel à la nature", "Question piège",
             "Déplacement des critères", "Sophisme du juste milieu", "Appel à l'ignorance"]
ALIASES = {
    "Fausse alternative": ["faux dilemme", "fausse dichotomie", "dilemme"],
    "Appel à l'autorité": ["argument d'autorite", "autorite"],
    "Homme de paille": ["epouvantail", "strawman"],
    "Appel à la popularité": ["ad populum", "argument du nombre", "popularite"],
    "Appel à l'émotion": ["pathos", "emotion"],
    "Renversement de la charge de la preuve": ["charge de la preuve"],
    "Corrélation n'est pas causalité": ["correlation", "cum hoc", "post hoc"],
    "Raisonnement circulaire": ["petition de principe", "circulaire"],
    "Diversion": ["hareng rouge", "red herring"],
    "Ad hominem": ["attaque personnelle", "ad personam"],
    "Généralisation hâtive": ["generalisation"],
    "Pente glissante": ["pente savonneuse"],
    "Appel à l'ignorance": ["ad ignorantiam", "ignorance"],
    "Déplacement des critères": ["deplacement"],
}
BADGES = [
    ("first_post", "Première tribune", "Publier un premier débat", lambda s: s["posts"] >= 1),
    ("tribune", "Tribun", "Publier 10 débats", lambda s: s["posts"] >= 10),
    ("pen", "Plume", "Écrire 10 commentaires", lambda s: s["comments"] >= 10),
    ("lynx", "Œil de lynx", "Démasquer 10 sophismes", lambda s: s["correct"] >= 10),
    ("hunter", "Chasseur de sophismes", "Démasquer 50 sophismes", lambda s: s["correct"] >= 50),
    ("juror", "Juré", "Rendre 10 verdicts", lambda s: s["verdicts"] >= 10),
    ("streak3", "Assidu", "3 jours de défi d'affilée", lambda s: s["best_streak"] >= 3),
    ("streak7", "Inarrêtable", "7 jours de défi d'affilée", lambda s: s["best_streak"] >= 7),
    ("heard", "Écouté", "Recevoir 10 réactions", lambda s: s["likes"] >= 10),
]
REACTIONS = ("like", "love", "haha", "wow", "think", "angry")
REACTION_LABELS = {"like": "👍", "love": "❤️", "haha": "😂", "wow": "😮", "think": "🤔", "angry": "😡"}
# Fonctions qu'un administrateur peut bloquer compte par compte
PERMISSIONS = {"analyze": "Analyses IA", "transcribe": "Transcription au micro", "publish": "Publication", "comment": "Commentaires",
               "react": "Réactions", "vote": "Votes et défis", "follow": "Abonnements", "profile": "Modification du profil",
               "message": "Messages privés"}
ANALYSIS_ENGINES = ("groq", "gemini")
TRANSCRIBE_ENGINES = ("groq-turbo", "groq-v3", "local", "gemini")
GROQ_MODELS = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.1-8b-instant"]
SECRETS = {"groq": "secret:groq_key", "gemini": "secret:gemini_key", "smtp": "secret:smtp_password"}  # jamais renvoyés en clair
ENV_KEYS = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}
HOOKS = {}  # fourni par arbitre_server : chat(cfg, system, user, max_tokens) -> dict ; test(cfg) -> dict
DEFAULT_SETTINGS = {
    "registrations_open": True, "publishing_enabled": True, "comments_enabled": True, "quiz_enabled": True,
    "announcement": "", "announcement_tone": "blue", "ads_enabled": False, "ad_frequency": 6, "ads": [],
    "llm": {"analysis_engine": "groq", "transcribe_engine": "groq-turbo", "groq_models": GROQ_MODELS, "temperature": 0.2,
            "require_account": True, "daily_quota": 20, "comment_ai": True},
    "smtp": {"host": "", "port": 587, "security": "starttls", "username": "", "from_email": "", "from_name": "rhetora"},
    "trash_days": 30, "lives_enabled": True,
}
USER_COLUMNS = {"blocked": "TEXT NOT NULL DEFAULT '[]'", "quota": "INTEGER", "verified": "INTEGER NOT NULL DEFAULT 0",
                "note": "TEXT NOT NULL DEFAULT ''", "email_ok": "INTEGER NOT NULL DEFAULT 1", "avatar": "TEXT",
                "prefs": "TEXT NOT NULL DEFAULT '{}'", "dm_policy": "TEXT NOT NULL DEFAULT 'all'"}
COMMENT_COLUMNS = {"attachment": "TEXT", "ai": "TEXT", "edited_at": "INTEGER"}
POST_COLUMNS = {"images": "TEXT NOT NULL DEFAULT '[]'"}
SESSION_COLUMNS = {"ip": "TEXT NOT NULL DEFAULT ''", "device": "TEXT NOT NULL DEFAULT ''", "seen_at": "INTEGER NOT NULL DEFAULT 0",
                   "sudo_until": "INTEGER NOT NULL DEFAULT 0"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active', bio TEXT NOT NULL DEFAULT '',
  xp INTEGER NOT NULL DEFAULT 0, streak INTEGER NOT NULL DEFAULT 0, best_streak INTEGER NOT NULL DEFAULT 0,
  streak_day TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL,
  stats TEXT NOT NULL DEFAULT '{}', hidden INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, views INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
  body TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS likes (post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (post_id, user_id));
CREATE TABLE IF NOT EXISTS verdicts (post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, choice TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (post_id, user_id));
CREATE TABLE IF NOT EXISTS quiz_answers (post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, qkey TEXT NOT NULL, choice TEXT NOT NULL,
  correct INTEGER NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (post_id, user_id, qkey));
CREATE TABLE IF NOT EXISTS daily_answers (day TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  choice TEXT NOT NULL, correct INTEGER NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (day, user_id));
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, target_type TEXT NOT NULL, target_id INTEGER NOT NULL,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, reason TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, user_id INTEGER, username TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ad_clicks (ad_id TEXT PRIMARY KEY, clicks INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions (target_type TEXT NOT NULL, target_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (target_type, target_id, user_id));
CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL, post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
  comment_id INTEGER REFERENCES comments(id) ON DELETE CASCADE, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
  seen INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS llm_usage (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  ip TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS broadcasts (id INTEGER PRIMARY KEY, sender TEXT NOT NULL, target TEXT NOT NULL, subject TEXT NOT NULL,
  body TEXT NOT NULL, recipients INTEGER NOT NULL, emailed INTEGER NOT NULL DEFAULT 0, email_status TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS follows (follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  followee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at INTEGER NOT NULL, PRIMARY KEY (follower_id, followee_id));
CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trash (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', owner_id INTEGER,
  owner_name TEXT NOT NULL DEFAULT '', deleted_by INTEGER, deleted_by_name TEXT NOT NULL DEFAULT '', by_staff INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '{}', data TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, purge_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL,
  mime TEXT NOT NULL, data BLOB NOT NULL, size INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lives (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL,
  speakers TEXT NOT NULL DEFAULT '[]', entries TEXT NOT NULL DEFAULT '[]', now_text TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'recording',
  status TEXT NOT NULL DEFAULT 'live', version INTEGER NOT NULL DEFAULT 0, peak INTEGER NOT NULL DEFAULT 0, cheers INTEGER NOT NULL DEFAULT 0,
  started_at INTEGER NOT NULL, last_push INTEGER NOT NULL, ended_at INTEGER);
CREATE TABLE IF NOT EXISTS live_chat (id INTEGER PRIMARY KEY, live_id INTEGER NOT NULL REFERENCES lives(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS post_views (post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, at INTEGER NOT NULL, PRIMARY KEY (post_id, user_id));
CREATE TABLE IF NOT EXISTS post_feedback (post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, value INTEGER NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (post_id, user_id));
CREATE TABLE IF NOT EXISTS dm_messages (id INTEGER PRIMARY KEY, sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, body TEXT NOT NULL, created_at INTEGER NOT NULL,
  read_at INTEGER, hidden_s INTEGER NOT NULL DEFAULT 0, hidden_r INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dm_blocks (blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at INTEGER NOT NULL, PRIMARY KEY (blocker_id, blocked_id));
CREATE INDEX IF NOT EXISTS idx_dm_pair ON dm_messages(sender_id, recipient_id, id);
CREATE INDEX IF NOT EXISTS idx_dm_inbox ON dm_messages(recipient_id, read_at);
CREATE INDEX IF NOT EXISTS idx_trash_owner ON trash(owner_id);
CREATE INDEX IF NOT EXISTS idx_lives_status ON lives(status);
CREATE INDEX IF NOT EXISTS idx_live_chat ON live_chat(live_id, id);
CREATE INDEX IF NOT EXISTS idx_post_views_user ON post_views(user_id);
CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows(followee_id);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reactions_user ON reactions(user_id);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, seen);
CREATE INDEX IF NOT EXISTS idx_llm_usage ON llm_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_quiz_user ON quiz_answers(user_id);
"""


class ApiError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class Binary:
    """Réponse binaire (image) servie telle quelle par le serveur HTTP"""
    def __init__(self, mime, data):
        self.mime, self.data = mime, data


class Redirect:
    def __init__(self, url):
        self.url = url


# ---------- Outils ----------
def now():
    return int(time.time())


def today():
    return date.today().isoformat()


def _norm(value):
    text = unicodedata.normalize("NFD", str(value or "").lower().replace("’", "'"))
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _str(value, limit=200):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()[:limit] if isinstance(value, str) else ""


def _list(value):
    return value if isinstance(value, list) else []


def _bool(value):
    return value is True or value in (1, "1", "true", "on")


def _int(value, default=0, lo=None, hi=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _safe_url(value):
    url = _str(value, 500)
    parts = urllib.parse.urlsplit(url)
    return url if parts.scheme in ("http", "https") and parts.netloc else ""


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    try:
        _, rounds, salt, digest = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), digest)


DUMMY_HASH = hash_password(secrets.token_hex(8))  # égalise le temps de réponse quand le compte n'existe pas

_hits = {}
_hits_lock = threading.Lock()


def _blocked(key, limit, window):
    t = time.time()
    with _hits_lock:
        _hits[key] = [h for h in _hits.get(key, []) if t - h < window]
        return len(_hits[key]) >= limit


def _record(key):
    with _hits_lock:
        _hits.setdefault(key, []).append(time.time())


def _limit(key, limit, window, message="Trop de requêtes : patientez un peu."):
    if _blocked(key, limit, window):
        raise ApiError(429, message)
    _record(key)


@contextmanager
def tx():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_columns(conn, table, columns):
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init():
    os.makedirs(DATA_DIR, exist_ok=True)
    with tx() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _add_columns(conn, "users", USER_COLUMNS)
        _add_columns(conn, "comments", COMMENT_COLUMNS)
        _add_columns(conn, "posts", POST_COLUMNS)
        _add_columns(conn, "sessions", SESSION_COLUMNS)
        maintenance(conn, force=True)
        # Anciens « j'aime » → réactions
        conn.execute("INSERT OR IGNORE INTO reactions (target_type, target_id, user_id, kind, created_at) SELECT 'post', post_id, user_id, 'like', ? FROM likes", (now(),))
        conn.execute("DELETE FROM likes")


# ---------- Niveaux, profils, badges ----------
def level_info(xp):
    idx = max(i for i, (threshold, _) in enumerate(LEVELS) if xp >= threshold)
    return {"level": idx + 1, "title": LEVELS[idx][1], "floor": LEVELS[idx][0], "next": LEVELS[idx + 1][0] if idx + 1 < len(LEVELS) else None}


def current_streak(row):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return row["streak"] if row["streak_day"] in (today(), yesterday) else 0


def blocked_perms(row):
    try:
        value = json.loads(row["blocked"] or "[]")
    except (ValueError, TypeError, IndexError, KeyError):
        return []
    return [p for p in value if p in PERMISSIONS] if isinstance(value, list) else []


def user_public(row, private=False):
    data = {"id": row["id"], "username": row["username"], "role": row["role"], "status": row["status"], "bio": row["bio"],
            "xp": row["xp"], "streak": current_streak(row), "best_streak": row["best_streak"], "created_at": row["created_at"],
            "verified": bool(row["verified"]), "avatar": row["avatar"]}
    if private:
        data |= {"email": row["email"], "blocked": blocked_perms(row), "prefs": user_prefs(row),
                 "dm_policy": row["dm_policy"] if row["dm_policy"] in DM_POLICIES else "all"}
    return data | level_info(row["xp"])


def user_prefs(row):
    try:
        value = json.loads(row["prefs"] or "{}")
    except (ValueError, TypeError):
        value = {}
    value = value if isinstance(value, dict) else {}
    return {"topics": [t for t in _list(value.get("topics")) if isinstance(t, str)][:12],
            "kinds": [k for k in _list(value.get("kinds")) if k in ("analysis", "debate")]}


def user_stats(conn, uid, best_streak=0):
    def one(sql):
        return conn.execute(sql, (uid,)).fetchone()[0]
    return {
        "posts": one("SELECT COUNT(*) FROM posts WHERE user_id = ?"),
        "comments": one("SELECT COUNT(*) FROM comments WHERE user_id = ?"),
        "correct": one("SELECT COUNT(*) FROM quiz_answers WHERE user_id = ? AND correct = 1") + one("SELECT COUNT(*) FROM daily_answers WHERE user_id = ? AND correct = 1"),
        "answers": one("SELECT COUNT(*) FROM quiz_answers WHERE user_id = ?") + one("SELECT COUNT(*) FROM daily_answers WHERE user_id = ?"),
        "verdicts": one("SELECT COUNT(*) FROM verdicts WHERE user_id = ?"),
        "likes": one("""SELECT COUNT(*) FROM reactions r WHERE r.user_id != ?1 AND (
            (r.target_type = 'post' AND r.target_id IN (SELECT id FROM posts WHERE user_id = ?1))
            OR (r.target_type = 'comment' AND r.target_id IN (SELECT id FROM comments WHERE user_id = ?1)))"""),
        "best_streak": best_streak,
    }


def badges_for(stats):
    return [{"id": bid, "name": name, "desc": desc, "earned": bool(test(stats))} for bid, name, desc, test in BADGES]


def add_xp(conn, uid, amount):
    if amount:
        conn.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (amount, uid))
    return amount


def me_payload(conn, uid):
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return user_public(row, private=True) if row else None


def follow_counts(conn, uid):
    return {"followers": conn.execute("SELECT COUNT(*) FROM follows WHERE followee_id = ?", (uid,)).fetchone()[0],
            "following": conn.execute("SELECT COUNT(*) FROM follows WHERE follower_id = ?", (uid,)).fetchone()[0]}


# ---------- Historique d'activité ----------
def _device(user_agent):
    ua = user_agent or ""
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")) if key in ua), "Navigateur inconnu")
    system = next((name for key, name in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iOS"), ("iPad", "iPadOS"), ("Mac OS", "macOS"), ("Linux", "Linux")) if key in ua), "")
    return f"{browser} · {system}" if system else browser


def log_activity(conn, uid, kind, detail="", ip=""):
    """Événement de compte (connexion, mot de passe, action de l'administration…) ; 500 derniers conservés par compte"""
    if not uid:
        return
    conn.execute("INSERT INTO activity (user_id, kind, detail, ip, created_at) VALUES (?,?,?,?,?)", (uid, kind, detail[:300], ip[:64], now()))
    conn.execute("""DELETE FROM activity WHERE user_id = ?1 AND id NOT IN
                    (SELECT id FROM activity WHERE user_id = ?1 ORDER BY id DESC LIMIT 500)""", (uid,))


# Chaque source renvoie (kind, at, a, b, post_id) ; les catégories servent de filtres
ACTIVITY_SOURCES = {
    "content": [
        "SELECT 'post' AS kind, p.created_at AS at, p.title AS a, '' AS b, p.id AS post_id FROM posts p WHERE p.user_id = :uid",
        "SELECT 'comment', c.created_at, substr(c.body, 1, 160), p.title, c.post_id FROM comments c JOIN posts p ON p.id = c.post_id WHERE c.user_id = :uid",
    ],
    "social": [
        """SELECT 'reaction_' || r.target_type, r.created_at, r.kind, COALESCE(p.title, substr(c.body, 1, 100), ''), COALESCE(p.id, c.post_id)
           FROM reactions r LEFT JOIN posts p ON r.target_type = 'post' AND p.id = r.target_id
           LEFT JOIN comments c ON r.target_type = 'comment' AND c.id = r.target_id WHERE r.user_id = :uid""",
        "SELECT 'follow', f.created_at, u.username, '', NULL FROM follows f JOIN users u ON u.id = f.followee_id WHERE f.follower_id = :uid",
    ],
    "games": [
        "SELECT 'verdict', v.created_at, v.choice, p.title, v.post_id FROM verdicts v JOIN posts p ON p.id = v.post_id WHERE v.user_id = :uid",
        """SELECT CASE WHEN q.correct THEN 'quiz_ok' ELSE 'quiz_ko' END, q.created_at, q.choice, p.title, q.post_id
           FROM quiz_answers q JOIN posts p ON p.id = q.post_id WHERE q.user_id = :uid""",
        "SELECT CASE WHEN d.correct THEN 'daily_ok' ELSE 'daily_ko' END, d.created_at, d.choice, '', NULL FROM daily_answers d WHERE d.user_id = :uid",
    ],
    "ai": ["SELECT 'ai', l.created_at, l.kind, '', NULL FROM llm_usage l WHERE l.user_id = :uid"],
    "live": ["SELECT 'live', l.started_at, l.title, CAST(l.peak AS TEXT), NULL FROM lives l WHERE l.user_id = :uid"],
    "account": [
        "SELECT 'register', u.created_at, '', '', NULL FROM users u WHERE u.id = :uid",
        "SELECT a.kind, a.created_at, a.detail, a.ip, NULL FROM activity a WHERE a.user_id = :uid",
    ],
}
# Actions de modération effectuées par le compte : visibles par l'administration uniquement
STAFF_SOURCES = {"account": ["SELECT 'moderation', au.created_at, au.action, au.detail, NULL FROM audit au WHERE au.user_id = :uid AND au.action != 'Inscription'"]}
# En-tête vide qui nomme les colonnes de l'union, quelle que soit la première source retenue
_ACT_HEAD = "SELECT NULL AS kind, NULL AS at, NULL AS a, NULL AS b, NULL AS post_id WHERE 0"
# Filtres du journal : chaque groupe rassemble des types d'événements
ACTIVITY_GROUPS = {
    "post": ("post",), "comment": ("comment",), "reaction": ("reaction_post", "reaction_comment"), "follow": ("follow",),
    "verdict": ("verdict",), "quiz": ("quiz_ok", "quiz_ko", "daily_ok", "daily_ko"), "ai": ("ai",), "live": ("live",),
    "login": ("register", "login", "logout"),
    "security": ("login_failed", "password", "sudo", "sudo_failed", "session_revoked", "profile", "prefs"),
    "trash": ("trash", "restore", "purge"), "admin": ("admin",), "moderation": ("moderation",),
}
PERIODS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}


def activity_feed(conn, uid, group, page, staff, q="", period=""):
    sources = [sql for k, v in ACTIVITY_SOURCES.items() for sql in v + (STAFF_SOURCES.get(k, []) if staff else [])]
    union = " UNION ALL ".join([_ACT_HEAD] + sources)
    params, base = {"uid": uid, "off": (page - 1) * 50}, []
    if q:
        base.append("(a LIKE :q OR b LIKE :q)")
        params["q"] = f"%{q}%"
    cond = list(base)
    if period in PERIODS:
        cond.append("at >= :since")
        params["since"] = now() - PERIODS[period]
    kinds = []
    if group:
        if group not in ACTIVITY_GROUPS:
            raise ApiError(400, "Catégorie inconnue.")
        kinds = [f":k{i}" for i in range(len(ACTIVITY_GROUPS[group]))]
        params |= {f"k{i}": k for i, k in enumerate(ACTIVITY_GROUPS[group])}

    def where(*parts):
        parts = [p for p in parts if p]
        return " WHERE " + " AND ".join(parts) if parts else ""
    kind_cond = f"kind IN ({','.join(kinds)})" if kinds else ""
    rows = conn.execute(f"SELECT * FROM ({union}){where(*cond, kind_cond)} ORDER BY at DESC LIMIT 51 OFFSET :off", params).fetchall()
    items = [{"kind": r["kind"], "at": r["at"], "a": r["a"] or "", "b": r["b"] or "", "post_id": r["post_id"]} for r in rows[:50]]
    data = {"items": items, "hasMore": len(rows) > 50}
    if page == 1:
        per_kind = {r["kind"]: r["n"] for r in conn.execute(f"SELECT kind, COUNT(*) AS n FROM ({union}){where(*cond)} GROUP BY kind", params)}
        data["counts"] = {g: sum(per_kind.get(k, 0) for k in ks) for g, ks in ACTIVITY_GROUPS.items()} | {"": sum(per_kind.values())}
        params["hsince"] = _midnight() - 29 * 86400
        per_day = {r["d"]: r["n"] for r in conn.execute(
            f"SELECT date(at, 'unixepoch', 'localtime') AS d, COUNT(*) AS n FROM ({union}){where(*base, kind_cond, 'at >= :hsince')} GROUP BY d", params)}
        data["days"] = [{"day": d, "n": per_day.get(d, 0)} for d in ((date.today() - timedelta(days=i)).isoformat() for i in range(29, -1, -1))]
    return data


# ---------- Paramètres ----------
def get_settings(conn):
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in out:
            try:
                value = json.loads(row["value"])
            except ValueError:
                continue
            if isinstance(out[row["key"]], dict):
                out[row["key"]] |= value if isinstance(value, dict) else {}
            else:
                out[row["key"]] = value
    return out


def _stored_secret(conn, name):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SECRETS[name],)).fetchone()
    try:
        value = json.loads(row["value"]) if row else ""
    except ValueError:
        value = ""
    return value if isinstance(value, str) else ""


def get_secret(conn, name):
    return _stored_secret(conn, name) or (os.environ.get(ENV_KEYS[name], "").strip() if name in ENV_KEYS else "")


def secret_info(conn, name):
    """État d'un secret pour l'administration : jamais la valeur, seulement un indice masqué"""
    stored = _stored_secret(conn, name)
    value = get_secret(conn, name)
    hint = f"{value[:4]}…{value[-4:]}" if len(value) >= 16 else ("••••" if value else "")
    return {"set": bool(value), "source": "admin" if stored else ("env" if value else ""), "hint": hint}


def set_secret(conn, name, value):
    if value:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (SECRETS[name], json.dumps(value)))
    else:
        conn.execute("DELETE FROM settings WHERE key = ?", (SECRETS[name],))


def llm_status(conn, llm):
    has = {"groq": bool(get_secret(conn, "groq")), "gemini": bool(get_secret(conn, "gemini")), "local": True}
    t = llm["transcribe_engine"]
    return {"ready": has[llm["analysis_engine"]], "transcribe_ready": has["groq" if t.startswith("groq") else t]}


def llm_config(conn, llm):
    """Configuration complète (avec clés) transmise au moteur d'analyse : usage interne au serveur uniquement"""
    return {"keys": {"groq": get_secret(conn, "groq"), "gemini": get_secret(conn, "gemini"), "groq_models": list(llm["groq_models"]),
                     "temperature": float(llm["temperature"])},
            "analysis_engine": llm["analysis_engine"], "transcribe_engine": llm["transcribe_engine"]}


def public_config(conn):
    s = get_settings(conn)
    ads = [a for a in s["ads"] if a.get("active")] if s["ads_enabled"] else []
    keys = ("registrations_open", "publishing_enabled", "comments_enabled", "quiz_enabled", "announcement", "announcement_tone", "ad_frequency",
            "lives_enabled", "trash_days")
    llm = s["llm"]
    status = llm_status(conn, llm)
    model = llm["groq_models"][0] if llm["analysis_engine"] == "groq" and llm["groq_models"] else "Gemini"
    return {k: s[k] for k in keys} | {
        "ads": [{k: a.get(k, "") for k in ("id", "title", "text", "sponsor", "image", "cta", "placement")} for a in ads],
        "llm": status | {"analysis_engine": llm["analysis_engine"], "transcribe_engine": llm["transcribe_engine"], "model": model,
                         "require_account": llm["require_account"], "daily_quota": llm["daily_quota"],
                         "comment_ai": bool(llm["comment_ai"]) and status["ready"]},
        "reactions": list(REACTIONS),
    }


# ---------- Quotas IA ----------
def _midnight():
    return int(datetime.combine(date.today(), datetime.min.time()).timestamp())


def user_quota(llm, row):
    """Analyses IA autorisées par jour (0 = illimité) : réglage du compte, sinon réglage global ; les admins sont illimités"""
    if row is not None and row["role"] == "admin":
        return 0
    if row is not None and row["quota"] is not None:
        return row["quota"]
    return _int(llm["daily_quota"], 20, 0)


def usage_today(conn, uid, ip=""):
    if uid:
        return conn.execute("SELECT COUNT(*) FROM llm_usage WHERE user_id = ? AND created_at >= ?", (uid, _midnight())).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM llm_usage WHERE user_id IS NULL AND ip = ? AND created_at >= ?", (ip, _midnight())).fetchone()[0]


def consume_quota(conn, llm, user, ip, kind):
    quota = user_quota(llm, user)
    uid = user["id"] if user else None
    if quota and usage_today(conn, uid, ip) >= quota:
        raise ApiError(429, f"Quota atteint : {quota} analyse{'s' if quota > 1 else ''} IA par jour. Revenez demain ou contactez l’administration.")
    conn.execute("INSERT INTO llm_usage (user_id, ip, kind, created_at) VALUES (?,?,?,?)", (uid, ip, kind, now()))


# ---------- Contenu des débats publiés ----------
def canonical(name):
    n = _norm(name)
    for canon in FALLACIES:
        if any(key in n for key in [_norm(canon)] + ALIASES.get(canon, [])):
            return canon
    return None


def clean_snapshot(snap, with_transcript):
    if not isinstance(snap, dict):
        raise ApiError(400, "Débat manquant.")
    analysis = snap.get("analysis") if isinstance(snap.get("analysis"), dict) else None
    out = {
        "topic": _str(snap.get("topic"), 200),
        "speakers": [s for s in (_str(x, 60) for x in _list(snap.get("speakers"))[:8]) if s],
        "entries": [e for e in _list(snap.get("entries")) if isinstance(e, dict)] if with_transcript else [],
        "analysis": analysis,
        "source": _safe_url(snap.get("source")),
        "analyzedAt": snap.get("analyzedAt") if isinstance(snap.get("analyzedAt"), (int, float, str)) else None,
    }
    if not out["entries"] and not analysis:
        raise ApiError(400, "Ce débat est vide : analysez-le ou joignez sa transcription avant de le publier.")
    if len(json.dumps(out, ensure_ascii=False).encode()) > MAX_SNAPSHOT:
        raise ApiError(413, "Débat trop volumineux : publiez-le sans la transcription.")
    return out


def orators(snap):
    return [o for o in _list((snap.get("analysis") or {}).get("orateurs")) if isinstance(o, dict)]


def carte_of(snap):
    carte = (snap.get("analysis") or {}).get("carte")
    return carte if isinstance(carte, dict) else None


def snapshot_stats(snap):
    ors = orators(snap)
    carte = carte_of(snap) or {}
    conclusion = carte.get("conclusion") if isinstance(carte.get("conclusion"), dict) else {}
    adv = conclusion.get("avantage")
    names = [_str(o.get("nom"), 60) for o in ors if _str(o.get("nom"), 60)] or snap["speakers"]
    return {
        "speakers": names[:6],
        "arguments": sum(len(_list(o.get("arguments"))) for o in ors),
        "sophismes": sum(len(_list(o.get("sophismes"))) for o in ors),
        "entries": len(snap["entries"]),
        "synthese": _str((snap.get("analysis") or {}).get("synthese"), 600),
        "carte": bool(carte),
        "avantage": adv if adv in ("pour", "contre", "equilibre") else "",
        "source": snap["source"],
    }


def verdict_options(snap):
    carte = carte_of(snap)
    if carte and isinstance(carte.get("pour"), dict) and isinstance(carte.get("contre"), dict):
        def camp(key, label):
            c = carte[key]
            who = ", ".join(_str(x, 40) for x in _list(c.get("orateurs"))[:3] if _str(x, 40))
            return {"id": key, "label": label, "detail": _str(c.get("intitule") or c.get("these"), 160), "who": who}
        conclusion = carte.get("conclusion") if isinstance(carte.get("conclusion"), dict) else {}
        options = [camp("pour", "Camp pour"), camp("contre", "Camp contre"), {"id": "equilibre", "label": "Match nul", "detail": "Aucun camp ne l’emporte nettement."}]
        ai = conclusion.get("avantage")
        return options, ai if ai in ("pour", "contre", "equilibre") else None
    names = [n for n in snapshot_stats(snap)["speakers"] if n and n != "Vidéo"][:6]
    return [{"id": f"s{i}", "label": n, "detail": ""} for i, n in enumerate(names)] + [{"id": "nul", "label": "Match nul", "detail": ""}], None


def quiz_items(post_id, snap):
    items = []
    for i, o in enumerate(orators(snap)):
        for j, s in enumerate(_list(o.get("sophismes"))):
            if not isinstance(s, dict) or not _str(s.get("extrait")) or not _str(s.get("nom")):
                continue
            answer = canonical(s["nom"]) or _str(s["nom"], 80)[:1].upper() + _str(s["nom"], 80)[1:]
            rng = random.Random(f"{post_id}:{i}-{j}")
            pool = [f for f in FALLACIES if f != answer and canonical(answer) != f]
            choices = rng.sample(pool, 3) + [answer]
            rng.shuffle(choices)
            items.append({"key": f"{i}-{j}", "speaker": _str(o.get("nom"), 60), "quote": _str(s["extrait"], 420),
                          "choices": choices, "answer": answer, "explication": _str(s.get("explication"), 600)})
            if len(items) >= 8:
                return items
    return items


def success_rates(conn, post_id):
    rows = conn.execute("SELECT qkey, COUNT(*) AS n, SUM(correct) AS ok FROM quiz_answers WHERE post_id = ? GROUP BY qkey", (post_id,))
    return {r["qkey"]: {"players": r["n"], "rate": round(100 * (r["ok"] or 0) / r["n"])} for r in rows}


def quiz_state(conn, post_id, snap, user):
    items = quiz_items(post_id, snap)
    mine = {}
    if user:
        mine = {r["qkey"]: r for r in conn.execute("SELECT qkey, choice, correct FROM quiz_answers WHERE post_id = ? AND user_id = ?", (post_id, user["id"]))}
    rates = success_rates(conn, post_id)
    out = []
    for it in items:
        q = {k: it[k] for k in ("key", "speaker", "quote", "choices")}
        if it["key"] in mine:
            q |= {"answered": True, "choice": mine[it["key"]]["choice"], "correct": bool(mine[it["key"]]["correct"]),
                  "answer": it["answer"], "explication": it["explication"], "stats": rates.get(it["key"])}
        out.append(q)
    return out


def verdict_state(conn, post_id, snap, user):
    options, ai = verdict_options(snap)
    mine = None
    if user:
        row = conn.execute("SELECT choice FROM verdicts WHERE post_id = ? AND user_id = ?", (post_id, user["id"])).fetchone()
        mine = row and row["choice"]
    state = {"options": options, "mine": mine, "total": conn.execute("SELECT COUNT(*) FROM verdicts WHERE post_id = ?", (post_id,)).fetchone()[0]}
    if mine:  # les résultats (public et arbitre) ne sont révélés qu'après avoir voté
        counts = {r["choice"]: r["n"] for r in conn.execute("SELECT choice, COUNT(*) AS n FROM verdicts WHERE post_id = ? GROUP BY choice", (post_id,))}
        state |= {"counts": counts, "ai": ai}
    return state


# ---------- Requête ----------
class Req:
    def __init__(self, conn, query, headers, body, ip):
        self.conn, self.headers, self.body, self.ip = conn, headers, body, ip
        self.query = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        self.user, self.token, self.out_headers = None, None, []

    def need(self, role="user"):
        if not self.user:
            raise ApiError(401, "Connectez-vous pour continuer.")
        if RANK.get(self.user["role"], 0) < RANK[role]:
            raise ApiError(403, "Droits insuffisants.")
        return self.user

    def is_mod(self):
        return bool(self.user) and RANK.get(self.user["role"], 0) >= RANK["moderator"]

    def can(self, perm):
        """Compte connecté dont la fonction `perm` n'a pas été bloquée par l'administration"""
        user = self.need()
        if perm in blocked_perms(user):
            raise ApiError(403, f"« {PERMISSIONS[perm]} » a été désactivé sur votre compte par l’administration.")
        return user

    def audit(self, action, detail=""):
        self.conn.execute("INSERT INTO audit (user_id, username, action, detail, created_at) VALUES (?,?,?,?,?)",
                          (self.user and self.user["id"], self.user["username"] if self.user else "", action, detail[:300], now()))

    def sudo(self):
        """Action sensible : exige un mot de passe ressaisi depuis moins de SUDO_TTL secondes sur cette session"""
        user = self.need()
        if (user["sudo_until"] or 0) < now():
            raise ApiError(403, "Confirmez votre mot de passe pour effectuer cette action sensible.", code="reauth")
        return user


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _cookie(token, max_age):
    return ("Set-Cookie", f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")


def _load_user(req):
    try:
        jar = SimpleCookie(req.headers.get("Cookie") or "")
    except CookieError:
        return
    morsel = jar.get(COOKIE)
    if not morsel or not morsel.value:
        return
    row = req.conn.execute(
        """SELECT u.*, s.rowid AS sid, s.sudo_until AS sudo_until, s.seen_at AS s_seen FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.expires_at > ? AND u.status = 'active'""",
        (_token_hash(morsel.value), now())).fetchone()
    if row:
        req.user, req.token = row, morsel.value
        if now() - row["last_seen"] > 300:
            req.conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), row["id"]))
        if now() - row["s_seen"] > 120:
            req.conn.execute("UPDATE sessions SET seen_at = ?, ip = ? WHERE rowid = ?", (now(), req.ip[:64], row["sid"]))


def _start_session(req, uid):
    token = secrets.token_urlsafe(32)
    device = _device(req.headers.get("User-Agent"))
    req.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    known = req.conn.execute("SELECT 1 FROM activity WHERE user_id = ? AND kind IN ('login', 'register') AND detail = ? LIMIT 1", (uid, device)).fetchone()
    if not known and req.conn.execute("SELECT 1 FROM activity WHERE user_id = ? AND kind = 'login' LIMIT 1", (uid,)).fetchone():
        notify(req.conn, uid, "security", None, title="Nouvelle connexion à votre compte",
               body=f"Connexion depuis un nouvel appareil : {device} (IP {req.ip}). Si ce n’est pas vous, changez votre mot de passe et fermez les autres sessions.")
    req.conn.execute("INSERT INTO sessions (token, user_id, created_at, expires_at, ip, device, seen_at) VALUES (?,?,?,?,?,?,?)",
                     (_token_hash(token), uid, now(), now() + SESSION_TTL, req.ip[:64], device, now()))
    req.conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now(), uid))
    req.out_headers.append(_cookie(token, SESSION_TTL))


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return deco


def handles(path):
    return path.startswith(PREFIXES)


def handle(method, path, query, headers, body, ip):
    """Point d'entrée appelé par le serveur HTTP. Renvoie (statut, données JSON | None, en-têtes)."""
    for m, rx, fn in ROUTES:
        match = rx.match(path)
        if match and m == method:
            break
    else:
        return 404, {"error": "Route inconnue."}, []
    try:
        if method != "GET":
            if headers.get("X-Rhetora") != "1":
                raise ApiError(403, "Requête refusée.")
            origin = headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc != headers.get("Host"):
                raise ApiError(403, "Origine refusée.")
        payload = {}
        if method in ("POST", "PUT", "PATCH") and body:
            try:
                payload = json.loads(body)
            except ValueError:
                raise ApiError(400, "Requête invalide.")
            if not isinstance(payload, dict):
                raise ApiError(400, "Requête invalide.")
        with tx() as conn:
            maintenance(conn)
            req = Req(conn, query, headers, payload, ip)
            _load_user(req)
            data = fn(req, *match.groups())
        if isinstance(data, Redirect):
            return 302, None, req.out_headers + [("Location", data.url)]
        status = 200
        if isinstance(data, tuple):
            status, data = data
        return status, data, req.out_headers
    except ApiError as e:
        return e.status, {"error": str(e)} | ({"code": e.code} if e.code else {}), []
    except Exception:
        traceback.print_exc()
        return 500, {"error": "Erreur interne du serveur."}, []


def llm_access(headers, ip, perm, label, transcription=False):
    """Contrôle d'accès aux fonctions IA du serveur (compte, permission, quota) ; renvoie la configuration
    définie par l'administrateur, clés comprises (usage interne uniquement). Lève ApiError."""
    with tx() as conn:
        req = Req(conn, "", headers, {}, ip)
        _load_user(req)
        llm = get_settings(conn)["llm"]
        if req.user:
            req.can(perm)
        elif llm["require_account"]:
            raise ApiError(401, "Connectez-vous pour utiliser l’IA de rhetora.")
        status = llm_status(conn, llm)
        if (perm == "transcribe" or transcription) and not status["transcribe_ready"]:
            raise ApiError(503, "La transcription n’est pas encore configurée par l’administrateur.")
        if perm == "transcribe":
            _limit(f"live:{req.user['id'] if req.user else ip}", 400, 3600, "Trop de segments transcrits : patientez un peu.")
        else:
            if not status["ready"]:
                raise ApiError(503, "Le modèle IA n’est pas encore configuré par l’administrateur.")
            consume_quota(conn, llm, req.user, ip, label)
        return llm_config(conn, llm)


# ---------- Authentification ----------
@route("GET", "/api/me")
def get_me(req):
    data = {"user": None, "config": public_config(req.conn)}
    if req.user:
        uid = req.user["id"]
        data["user"] = user_public(req.user, private=True)
        data["user"]["daily_done"] = bool(req.conn.execute("SELECT 1 FROM daily_answers WHERE day = ? AND user_id = ?", (today(), uid)).fetchone())
        data["user"] |= {"quota": user_quota(get_settings(req.conn)["llm"], req.user), "usage_today": usage_today(req.conn, uid),
                         "unread": req.conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND seen = 0", (uid,)).fetchone()[0],
                         "dm_unread": _dm_unread(req.conn, uid)}
        if req.is_mod():
            data["user"]["open_reports"] = req.conn.execute("SELECT COUNT(*) FROM reports WHERE resolved = 0").fetchone()[0]
    return data


@route("POST", "/api/auth/register")
def register(req):
    first = req.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    if not first and not get_settings(req.conn)["registrations_open"]:
        raise ApiError(403, "Les inscriptions sont fermées pour le moment.")
    username, email, password = _str(req.body.get("username"), 40), _str(req.body.get("email"), 254).lower(), req.body.get("password")
    if not USERNAME_RE.match(username):
        raise ApiError(400, "Pseudo : 3 à 24 caractères (lettres, chiffres, point, tiret, souligné).")
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Adresse e-mail invalide.")
    if not isinstance(password, str) or not 8 <= len(password) <= 200:
        raise ApiError(400, "Mot de passe : 8 caractères minimum.")
    if password.lower() in (username.lower(), email):
        raise ApiError(400, "Le mot de passe doit différer du pseudo et de l’e-mail.")
    _limit(f"register:{req.ip}", 6, 3600, "Trop d’inscriptions depuis cette adresse : réessayez plus tard.")
    try:
        uid = _next_id(req.conn, "users")
        req.conn.execute("INSERT INTO users (id, username, email, pw_hash, role, created_at) VALUES (?,?,?,?,?,?)",
                         (uid, username, email, hash_password(password), "admin" if first else "user", now()))
    except sqlite3.IntegrityError:
        raise ApiError(409, "Ce pseudo ou cette adresse e-mail est déjà utilisé.")
    req.user = req.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    req.audit("Inscription", "premier compte : administrateur" if first else "")
    _start_session(req, uid)
    log_activity(req.conn, uid, "login", _device(req.headers.get("User-Agent")), req.ip)
    return 201, {"user": user_public(req.user, private=True)}


@route("POST", "/api/auth/login")
def login(req):
    ident, password = _str(req.body.get("login"), 254), req.body.get("password")
    key = f"login:{req.ip}"
    if _blocked(key, 8, 600):
        raise ApiError(429, "Trop de tentatives : réessayez dans quelques minutes.")
    row = req.conn.execute("SELECT * FROM users WHERE username = ? OR email = ?", (ident, ident.lower())).fetchone()
    ok = check_password(password if isinstance(password, str) else "", row["pw_hash"] if row else DUMMY_HASH)
    if not row or not ok:
        _record(key)
        if row:  # tentative visible dans l'historique du compte visé
            log_activity(req.conn, row["id"], "login_failed", _device(req.headers.get("User-Agent")), req.ip)
            req.conn.commit()
        raise ApiError(401, "Identifiants incorrects.")
    if row["status"] != "active":
        raise ApiError(403, "Ce compte a été suspendu par la modération.")
    _start_session(req, row["id"])
    log_activity(req.conn, row["id"], "login", _device(req.headers.get("User-Agent")), req.ip)
    return {"user": user_public(row, private=True)}


@route("POST", "/api/auth/logout")
def logout(req):
    if req.token:
        req.conn.execute("DELETE FROM sessions WHERE token = ?", (_token_hash(req.token),))
        log_activity(req.conn, req.user and req.user["id"], "logout", _device(req.headers.get("User-Agent")), req.ip)
    req.out_headers.append(_cookie("", 0))
    return {"ok": True}


@route("PATCH", "/api/me")
def update_me(req):
    user = req.need()
    if "bio" in req.body:
        req.can("profile")
        req.conn.execute("UPDATE users SET bio = ? WHERE id = ?", (_str(req.body.get("bio"), 280), user["id"]))
        log_activity(req.conn, user["id"], "profile", "Bio modifiée", req.ip)
    if req.body.get("new_password") is not None:
        current, new = req.body.get("current_password"), req.body.get("new_password")
        if not isinstance(current, str) or not check_password(current, user["pw_hash"]):
            raise ApiError(403, "Mot de passe actuel incorrect.")
        if not isinstance(new, str) or not 8 <= len(new) <= 200:
            raise ApiError(400, "Nouveau mot de passe : 8 caractères minimum.")
        req.conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(new), user["id"]))
        req.conn.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user["id"], _token_hash(req.token)))
        log_activity(req.conn, user["id"], "password", "Mot de passe modifié, autres sessions fermées", req.ip)
    if isinstance(req.body.get("prefs"), dict):
        raw, topics = req.body["prefs"], []
        for t in _list(raw.get("topics"))[:12]:
            t = re.sub(r"\s+", " ", _str(t, 30))
            if len(t) >= 3 and _norm(t) not in (_norm(x) for x in topics):
                topics.append(t)
        kinds = [k for k in _list(raw.get("kinds")) if k in ("analysis", "debate")]
        req.conn.execute("UPDATE users SET prefs = ? WHERE id = ?", (json.dumps({"topics": topics, "kinds": kinds}, ensure_ascii=False), user["id"]))
        log_activity(req.conn, user["id"], "prefs", "Centres d’intérêt : " + (", ".join(topics) or "aucun"), req.ip)
    if "dm_policy" in req.body:
        policy = req.body.get("dm_policy")
        if policy not in DM_POLICIES:
            raise ApiError(400, "Réglage des messages privés invalide.")
        req.conn.execute("UPDATE users SET dm_policy = ? WHERE id = ?", (policy, user["id"]))
    return {"user": me_payload(req.conn, user["id"])}


@route("GET", "/api/me/activity")
def my_activity(req):
    user = req.need()
    q = req.query
    return activity_feed(req.conn, user["id"], q.get("type", ""), _int(q.get("page"), 1, 1, 1000), RANK.get(user["role"], 0) >= RANK["moderator"],
                         _str(q.get("q"), 80), q.get("period", ""))


# ---------- Publications ----------
POST_SELECT = """SELECT p.id, p.user_id, p.title, p.comment, p.kind, p.topic, p.stats, p.hidden, p.pinned, p.views, p.created_at,
  u.username, u.xp AS author_xp, u.role AS author_role, u.verified AS author_verified, u.avatar AS author_avatar, p.images,
  (SELECT COUNT(*) FROM reactions r WHERE r.target_type = 'post' AND r.target_id = p.id) AS likes,
  (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id AND c.hidden = 0) AS comments,
  (SELECT COUNT(*) FROM verdicts v WHERE v.post_id = p.id) AS votes,
  (SELECT r.kind FROM reactions r WHERE r.target_type = 'post' AND r.target_id = p.id AND r.user_id = ?) AS my_reaction
  FROM posts p JOIN users u ON u.id = p.user_id"""


def author_of(row, prefix=""):
    return {"id": row["user_id"], "username": row["username"], "role": row[f"{prefix}role"], "title": level_info(row[f"{prefix}xp"])["title"],
            "verified": bool(row[f"{prefix}verified"]), "avatar": row[f"{prefix}avatar"]}


def post_summary(row, excerpt=True):
    stats = json.loads(row["stats"] or "{}")
    return {"id": row["id"], "title": row["title"], "comment": row["comment"][:280] if excerpt else row["comment"], "kind": row["kind"],
            "topic": row["topic"], "stats": stats, "hidden": bool(row["hidden"]), "pinned": bool(row["pinned"]), "views": row["views"],
            "created_at": row["created_at"], "likes": row["likes"], "comments": row["comments"], "votes": row["votes"],
            "liked": bool(row["my_reaction"]), "author": author_of(row, "author_"), "images": _images(row["images"])}


def _images(value):
    try:
        ids = json.loads(value or "[]")
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str) and MEDIA_ID_RE.fullmatch(i)][:4] if isinstance(ids, list) else []


def reaction_summary(conn, target_type, ids, uid):
    """{id: {total, counts: {type: n}, mine}} pour une liste de publications ou de commentaires"""
    out = {i: {"total": 0, "counts": {}, "mine": None} for i in ids}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT target_id, kind, COUNT(*) AS n FROM reactions WHERE target_type = ? AND target_id IN ({marks}) GROUP BY target_id, kind",
                              (target_type, *chunk)):
            out[r["target_id"]]["counts"][r["kind"]] = r["n"]
            out[r["target_id"]]["total"] += r["n"]
        for r in conn.execute(f"SELECT target_id, kind FROM reactions WHERE target_type = ? AND user_id = ? AND target_id IN ({marks})",
                              (target_type, uid, *chunk)):
            out[r["target_id"]]["mine"] = r["kind"]
    return out


def _json_or_none(value):
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def notify(conn, user_id, kind, actor_id=None, post_id=None, comment_id=None, title="", body=""):
    """Notification interne (jamais à soi-même) ; les réactions non lues sur un même contenu sont regroupées"""
    if not user_id or user_id == actor_id:
        return
    if kind == "reaction":
        prev = conn.execute("SELECT id FROM notifications WHERE user_id = ? AND kind = 'reaction' AND seen = 0 AND post_id IS ? AND comment_id IS ?",
                            (user_id, post_id, comment_id)).fetchone()
        if prev:
            conn.execute("UPDATE notifications SET actor_id = ?, title = ?, created_at = ? WHERE id = ?", (actor_id, title, now(), prev["id"]))
            return
    conn.execute("INSERT INTO notifications (user_id, kind, actor_id, post_id, comment_id, title, body, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (user_id, kind, actor_id, post_id, comment_id, title[:200], body[:5000], now()))


def _purge_reactions(conn):
    conn.execute("""DELETE FROM reactions WHERE (target_type = 'post' AND target_id NOT IN (SELECT id FROM posts))
                    OR (target_type = 'comment' AND target_id NOT IN (SELECT id FROM comments))""")


def _uid(req):
    return req.user["id"] if req.user else -1


def _fetch_post(req, pid, full=False):
    row = req.conn.execute(POST_SELECT.replace("SELECT p.id", "SELECT p.snapshot, p.id") + " WHERE p.id = ?", (_uid(req), pid)).fetchone()
    if not row or (row["hidden"] and not req.is_mod() and row["user_id"] != _uid(req)):
        raise ApiError(404, "Publication introuvable ou retirée.")
    return row


@route("GET", "/api/community/posts")
def list_posts(req):
    q = req.query
    where, args = [], [_uid(req)]
    if not req.is_mod():
        where.append("(p.hidden = 0 OR p.user_id = ?)")
        args.append(_uid(req))
    if q.get("kind") in ("analysis", "debate"):
        where.append("p.kind = ?")
        args.append(q["kind"])
    if q.get("author"):
        where.append("u.username = ?")
        args.append(_str(q["author"], 40))
    if q.get("feed") == "following":
        where.append("p.user_id IN (SELECT followee_id FROM follows WHERE follower_id = ?)")
        args.append(req.need()["id"])
    if q.get("liked_by"):  # publications aimées : visibles par leur auteur et l'administration
        liker = req.conn.execute("SELECT id FROM users WHERE username = ?", (_str(q["liked_by"], 40),)).fetchone()
        if not liker or (liker["id"] != _uid(req) and not (req.user and req.user["role"] == "admin")):
            raise ApiError(403, "Ces réactions sont privées.")
        where.append("p.id IN (SELECT target_id FROM reactions WHERE target_type = 'post' AND user_id = ?)")
        args.append(liker["id"])
    if q.get("q"):
        where.append("(p.title LIKE ? OR p.topic LIKE ? OR p.comment LIKE ?)")
        args += [f"%{_str(q['q'], 100)}%"] * 3
    page = _int(q.get("page"), 1, 1, 500)
    reasons = {}
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    if q.get("sort") == "foryou":
        # « Pour vous » : 500 candidats récents classés selon les intérêts, interactions, abonnements, popularité et fraîcheur
        rows = req.conn.execute(POST_SELECT + where_sql + " ORDER BY p.created_at DESC LIMIT 500", args).fetchall()
        profile = interest_profile(req.conn, req.user) if req.user else None
        ranked = rank_for_you(rows, profile, _uid(req))
        ranked = [x for x in ranked if x[1]["pinned"]] + [x for x in ranked if not x[1]["pinned"]]
        chunk = ranked[(page - 1) * PAGE:page * PAGE + 1]
        rows, reasons = [x[1] for x in chunk], {x[1]["id"]: x[2] for x in chunk}
    else:
        order = {"top": "likes DESC, p.created_at DESC", "discussed": "comments DESC, p.created_at DESC"}.get(q.get("sort"), "p.created_at DESC")
        sql = POST_SELECT + where_sql + f" ORDER BY p.pinned DESC, {order} LIMIT ? OFFSET ?"
        rows = req.conn.execute(sql, args + [PAGE + 1, (page - 1) * PAGE]).fetchall()
    posts = [post_summary(r) | ({"reason": reasons[r["id"]]} if r["id"] in reasons else {}) for r in rows[:PAGE]]
    reactions = reaction_summary(req.conn, "post", [p["id"] for p in posts], _uid(req))
    for p in posts:
        p["reactions"] = reactions[p["id"]]
        top = req.conn.execute("""SELECT c.id, c.body, c.created_at, u.username, u.avatar,
                (SELECT COUNT(*) FROM reactions r WHERE r.target_type = 'comment' AND r.target_id = c.id) AS n
                FROM comments c JOIN users u ON u.id = c.user_id WHERE c.post_id = ? AND c.hidden = 0
                ORDER BY n DESC, c.created_at DESC LIMIT 1""", (p["id"],)).fetchone()
        p["top_comment"] = top and {"id": top["id"], "body": top["body"][:240], "username": top["username"], "avatar": top["avatar"],
                                    "created_at": top["created_at"], "reactions": top["n"]}
    return {"posts": posts, "hasMore": len(rows) > PAGE}


@route("POST", "/api/community/posts")
def create_post(req):
    user = req.can("publish")
    if not get_settings(req.conn)["publishing_enabled"] and not req.is_mod():
        raise ApiError(403, "La publication est désactivée pour le moment.")
    title, comment = _str(req.body.get("title"), 140), _str(req.body.get("comment"), 4000)
    if len(title) < 4:
        raise ApiError(400, "Donnez un titre d’au moins 4 caractères.")
    snap = clean_snapshot(req.body.get("snapshot"), _bool(req.body.get("with_transcript")))
    image_ids = list(dict.fromkeys(i for i in _list(req.body.get("images")) if isinstance(i, str) and MEDIA_ID_RE.fullmatch(i)))
    if len(image_ids) > 4:
        raise ApiError(400, "4 images maximum par publication.")
    for mid in image_ids:
        if not req.conn.execute("SELECT 1 FROM media WHERE id = ? AND user_id = ? AND kind = 'post' AND used = 0", (mid, user["id"])).fetchone():
            raise ApiError(400, "Image introuvable : renvoyez-la.")
    _limit(f"post:{user['id']}", 10, 3600, "Vous publiez beaucoup : réessayez dans un moment.")
    stats = snapshot_stats(snap)
    pid = _next_id(req.conn, "posts")
    req.conn.execute("INSERT INTO posts (id, user_id, title, comment, kind, topic, snapshot, stats, images, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (pid, user["id"], title, comment, "analysis" if snap["analysis"] else "debate", snap["topic"],
                      json.dumps(snap, ensure_ascii=False), json.dumps(stats, ensure_ascii=False), json.dumps(image_ids), now()))
    for mid in image_ids:
        req.conn.execute("UPDATE media SET used = 1 WHERE id = ?", (mid,))
    midnight = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    today_posts = req.conn.execute("SELECT COUNT(*) FROM posts WHERE user_id = ? AND created_at >= ?", (user["id"], midnight)).fetchone()[0]
    xp = add_xp(req.conn, user["id"], 15 if today_posts <= 3 else 0)
    return 201, {"id": pid, "xp": xp, "user": me_payload(req.conn, user["id"])}


@route("GET", r"/api/community/posts/(\d+)")
def get_post(req, pid):
    pid = int(pid)
    row = _fetch_post(req, pid)
    if not _bool(req.query.get("refresh")):
        req.conn.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (pid,))
        if req.user:
            req.conn.execute("INSERT OR REPLACE INTO post_views (post_id, user_id, at) VALUES (?,?,?)", (pid, req.user["id"], now()))
    snap = json.loads(row["snapshot"])
    comments = []
    rows = req.conn.execute("""SELECT c.*, u.username, u.xp, u.role, u.verified, u.avatar FROM comments c JOIN users u ON u.id = c.user_id
                               WHERE c.post_id = ? ORDER BY c.created_at LIMIT 1000""", (pid,)).fetchall()
    reactions = reaction_summary(req.conn, "comment", [c["id"] for c in rows], _uid(req))
    for c in rows:
        hide = c["hidden"] and not req.is_mod()
        comments.append({"id": c["id"], "parent_id": c["parent_id"], "body": "" if hide else c["body"], "hidden": bool(c["hidden"]),
                         "created_at": c["created_at"], "edited_at": c["edited_at"], "mine": c["user_id"] == _uid(req),
                         "attachment": None if hide else _json_or_none(c["attachment"]), "ai": None if hide else _json_or_none(c["ai"]),
                         "reactions": reactions[c["id"]], "author": author_of(c)})
    settings = get_settings(req.conn)
    return {"post": post_summary(row, excerpt=False) | {"snapshot": snap, "mine": row["user_id"] == _uid(req),
                                                        "reactions": reaction_summary(req.conn, "post", [pid], _uid(req))[pid]},
            "comments": comments,
            "verdict": verdict_state(req.conn, pid, snap, req.user),
            "quiz": quiz_state(req.conn, pid, snap, req.user) if settings["quiz_enabled"] else [],
            "comments_enabled": settings["comments_enabled"],
            "following": bool(req.conn.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (_uid(req), row["user_id"])).fetchone())}


@route("PATCH", r"/api/community/posts/(\d+)")
def update_post(req, pid):
    user = req.need()
    row = _fetch_post(req, int(pid))
    if row["user_id"] == user["id"]:
        if "title" in req.body:
            title = _str(req.body.get("title"), 140)
            if len(title) < 4:
                raise ApiError(400, "Titre trop court.")
            req.conn.execute("UPDATE posts SET title = ? WHERE id = ?", (title, row["id"]))
        if "comment" in req.body:
            req.conn.execute("UPDATE posts SET comment = ? WHERE id = ?", (_str(req.body.get("comment"), 4000), row["id"]))
    for flag in ("hidden", "pinned"):
        if flag in req.body:
            req.need("moderator")
            req.conn.execute(f"UPDATE posts SET {flag} = ? WHERE id = ?", (int(_bool(req.body[flag])), row["id"]))
            req.audit("Publication " + ({"hidden": "masquée", "pinned": "épinglée"}[flag] if _bool(req.body[flag]) else {"hidden": "réaffichée", "pinned": "désépinglée"}[flag]), f"#{row['id']} {row['title']}")
    return {"ok": True}


@route("DELETE", r"/api/community/posts/(\d+)")
def delete_post(req, pid):
    user = req.need()
    row = _fetch_post(req, int(pid))
    staff = row["user_id"] != user["id"]
    if staff:
        req.need("moderator")
        req.audit("Publication supprimée", f"#{row['id']} {row['title']} (de {row['username']})")
    bundle = trash_bundle(req.conn, post_ids=[row["id"]]) | {"roots": {"posts": [row["id"]]}}
    owner = {"id": row["user_id"], "username": row["username"]}
    tid = move_to_trash(req, "post", row["title"], owner, bundle, _str(req.query.get("reason"), 300))
    req.conn.execute("DELETE FROM posts WHERE id = ?", (row["id"],))
    _purge_reactions(req.conn)
    log_activity(req.conn, user["id"], "trash", f"Publication « {row['title'][:80]} »" + (f" (de {row['username']})" if staff else ""), req.ip)
    if staff:
        notify(req.conn, row["user_id"], "moderation", user["id"], title=f"Votre publication « {row['title'][:60]} » a été retirée par la modération",
               body=_str(req.query.get("reason"), 300))
    return {"ok": True, "trash_id": tid}


@route("POST", "/api/community/react")
def react(req):
    user = req.can("react")
    ttype, tid, kind = req.body.get("type"), _int(req.body.get("id")), req.body.get("kind")
    if kind is not None and kind not in REACTIONS:
        raise ApiError(400, "Réaction inconnue.")
    if ttype == "post":
        row = _fetch_post(req, tid)
        owner, post_id, comment_id, what = row["user_id"], row["id"], None, f"votre publication « {row['title'][:60]} »"
    elif ttype == "comment":
        row = req.conn.execute("SELECT * FROM comments WHERE id = ?", (tid,)).fetchone()
        if not row or (row["hidden"] and not req.is_mod()):
            raise ApiError(404, "Commentaire introuvable.")
        _fetch_post(req, row["post_id"])
        owner, post_id, comment_id, what = row["user_id"], row["post_id"], row["id"], f"votre commentaire « {row['body'][:60]} »"
    else:
        raise ApiError(400, "Contenu invalide.")
    _limit(f"react:{user['id']}", 120, 600, "Vous réagissez très vite : patientez un instant.")
    prev = req.conn.execute("SELECT kind FROM reactions WHERE target_type = ? AND target_id = ? AND user_id = ?", (ttype, tid, user["id"])).fetchone()
    if kind is None or (prev and prev["kind"] == kind):
        req.conn.execute("DELETE FROM reactions WHERE target_type = ? AND target_id = ? AND user_id = ?", (ttype, tid, user["id"]))
    else:
        req.conn.execute("INSERT OR REPLACE INTO reactions (target_type, target_id, user_id, kind, created_at) VALUES (?,?,?,?,?)",
                         (ttype, tid, user["id"], kind, now()))
        if not prev:
            notify(req.conn, owner, "reaction", user["id"], post_id, comment_id, f"{user['username']} a réagi {REACTION_LABELS[kind]} à {what}")
    return {"reactions": reaction_summary(req.conn, ttype, [tid], user["id"])[tid]}


@route("POST", r"/api/community/posts/(\d+)/verdict")
def vote_verdict(req, pid):
    user = req.can("vote")
    row = _fetch_post(req, int(pid))
    snap = json.loads(row["snapshot"])
    choice = _str(req.body.get("choice"), 20)
    if choice not in {o["id"] for o in verdict_options(snap)[0]}:
        raise ApiError(400, "Choix invalide.")
    first = not req.conn.execute("SELECT 1 FROM verdicts WHERE post_id = ? AND user_id = ?", (row["id"], user["id"])).fetchone()
    req.conn.execute("INSERT OR REPLACE INTO verdicts (post_id, user_id, choice, created_at) VALUES (?,?,?,?)", (row["id"], user["id"], choice, now()))
    xp = add_xp(req.conn, user["id"], 3 if first else 0)
    return {"verdict": verdict_state(req.conn, row["id"], snap, user), "xp": xp, "user": me_payload(req.conn, user["id"])}


@route("POST", r"/api/community/posts/(\d+)/quiz")
def answer_quiz(req, pid):
    user = req.can("vote")
    if not get_settings(req.conn)["quiz_enabled"]:
        raise ApiError(403, "Les défis sont désactivés.")
    row = _fetch_post(req, int(pid))
    snap = json.loads(row["snapshot"])
    item = next((it for it in quiz_items(row["id"], snap) if it["key"] == req.body.get("key")), None)
    if not item:
        raise ApiError(404, "Question introuvable.")
    choice = _str(req.body.get("choice"), 80)
    if choice not in item["choices"]:
        raise ApiError(400, "Choix invalide.")
    xp = 0
    if not req.conn.execute("SELECT 1 FROM quiz_answers WHERE post_id = ? AND user_id = ? AND qkey = ?", (row["id"], user["id"], item["key"])).fetchone():
        correct = choice == item["answer"]
        req.conn.execute("INSERT INTO quiz_answers (post_id, user_id, qkey, choice, correct, created_at) VALUES (?,?,?,?,?,?)",
                         (row["id"], user["id"], item["key"], choice, int(correct), now()))
        xp = add_xp(req.conn, user["id"], 10 if correct else 2)
    quiz = quiz_state(req.conn, row["id"], snap, user)
    return {"question": next(q for q in quiz if q["key"] == item["key"]), "xp": xp, "user": me_payload(req.conn, user["id"])}


def clean_attachment(att):
    """Résumé compact d'une analyse joint à un commentaire (bibliothèque de l'auteur)"""
    if att is None:
        return None
    if not isinstance(att, dict):
        raise ApiError(400, "Analyse jointe invalide.")
    ors = []
    for o in _list(att.get("orateurs"))[:6]:
        if not isinstance(o, dict) or not _str(o.get("nom"), 60):
            continue
        ors.append({"nom": _str(o.get("nom"), 60), "these": _str(o.get("these"), 300), "arguments": _int(o.get("arguments"), 0, 0, 999),
                    "nb_sophismes": _int(o.get("nb_sophismes"), 0, 0, 999),
                    "sophismes": [{"nom": _str(s.get("nom"), 80), "extrait": _str(s.get("extrait"), 240)}
                                  for s in _list(o.get("sophismes"))[:3] if isinstance(s, dict) and _str(s.get("nom"), 80)]})
    out = {"topic": _str(att.get("topic"), 200), "source": _safe_url(att.get("source")), "synthese": _str(att.get("synthese"), 900),
           "avantage": att.get("avantage") if att.get("avantage") in ("pour", "contre", "equilibre") else "",
           "question": _str(att.get("question"), 200), "orateurs": ors}
    if not ors and not out["synthese"]:
        raise ApiError(400, "L’analyse jointe est vide.")
    return out


@route("POST", r"/api/community/posts/(\d+)/comments")
def add_comment(req, pid):
    user = req.can("comment")
    if not get_settings(req.conn)["comments_enabled"] and not req.is_mod():
        raise ApiError(403, "Les commentaires sont désactivés pour le moment.")
    row = _fetch_post(req, int(pid))
    body = _str(req.body.get("body"), 2000)
    attachment = clean_attachment(req.body.get("attachment"))
    if not body and not attachment:
        raise ApiError(400, "Commentaire vide.")
    parent, prow = req.body.get("parent_id"), None
    if parent is not None:
        prow = req.conn.execute("SELECT id, parent_id, user_id, body FROM comments WHERE id = ? AND post_id = ?", (_int(parent), row["id"])).fetchone()
        if not prow:
            raise ApiError(400, "Commentaire parent introuvable.")
        parent = prow["parent_id"] or prow["id"]  # un seul niveau de réponses
    _limit(f"comment:{user['id']}", 20, 600, "Vous commentez très vite : patientez un instant.")
    cid = _next_id(req.conn, "comments")
    req.conn.execute("INSERT INTO comments (id, post_id, user_id, parent_id, body, attachment, created_at) VALUES (?,?,?,?,?,?,?)",
                     (cid, row["id"], user["id"], parent, body, json.dumps(attachment, ensure_ascii=False) if attachment else None, now()))
    excerpt = (body or "(analyse jointe)")[:140]
    if prow:
        notify(req.conn, prow["user_id"], "reply", user["id"], row["id"], cid, f"{user['username']} a répondu à votre commentaire", excerpt)
    if not prow or prow["user_id"] != row["user_id"]:
        notify(req.conn, row["user_id"], "comment", user["id"], row["id"], cid, f"{user['username']} a commenté « {row['title'][:60]} »", excerpt)
    return 201, {"ok": True, "id": cid}


@route("PATCH", r"/api/community/comments/(\d+)")
def update_comment(req, cid):
    user = req.need()
    row = req.conn.execute("SELECT * FROM comments WHERE id = ?", (int(cid),)).fetchone()
    if not row:
        raise ApiError(404, "Commentaire introuvable.")
    if "body" in req.body:
        if row["user_id"] != user["id"]:
            raise ApiError(403, "Vous ne pouvez modifier que vos propres commentaires.")
        req.can("comment")
        body = _str(req.body.get("body"), 2000)
        if not body and not row["attachment"]:
            raise ApiError(400, "Commentaire vide.")
        if body != row["body"]:  # le texte change : l'arbitrage IA éventuel ne s'applique plus
            req.conn.execute("UPDATE comments SET body = ?, edited_at = ?, ai = NULL WHERE id = ?", (body, now(), row["id"]))
    if "hidden" in req.body:
        req.need("moderator")
        hidden = _bool(req.body.get("hidden"))
        req.conn.execute("UPDATE comments SET hidden = ? WHERE id = ?", (int(hidden), row["id"]))
        req.audit("Commentaire " + ("masqué" if hidden else "réaffiché"), f"#{row['id']} : {row['body'][:80]}")
    return {"ok": True}


COMMENT_AI_PROMPT = """Tu es un arbitre de débat impartial, expert en logique et en rhétorique. On te fournit un commentaire publié sous un débat (et, si besoin, le contexte : titre du débat, message auquel il répond).
Évalue la qualité argumentative du COMMENTAIRE uniquement : thèse défendue, solidité du raisonnement, sophismes clairement présents (en cas de doute, n'en signale pas), points forts.
N'évalue pas si l'opinion est juste ou fausse, seulement la façon d'argumenter. Textes courts, compréhensibles par tous, ton bienveillant.
Réponds UNIQUEMENT avec un objet JSON valide :
{"verdict":"string (2 phrases maximum)","solidite":"forte|moyenne|faible","these":"string","sophismes":[{"nom":"string","extrait":"string (citation exacte, 20 mots max)","explication":"string"}],"points_forts":["string"]}"""


def clean_ai(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {"verdict": _str(raw.get("verdict"), 400), "solidite": raw.get("solidite") if raw.get("solidite") in ("forte", "moyenne", "faible") else "moyenne",
            "these": _str(raw.get("these"), 300),
            "sophismes": [{"nom": _str(s.get("nom"), 80), "extrait": _str(s.get("extrait"), 200), "explication": _str(s.get("explication"), 300)}
                          for s in _list(raw.get("sophismes"))[:4] if isinstance(s, dict) and _str(s.get("nom"), 80)],
            "points_forts": [p for p in (_str(x, 200) for x in _list(raw.get("points_forts"))[:3]) if p]}


@route("POST", r"/api/community/comments/(\d+)/ai")
def comment_ai(req, cid):
    user = req.can("analyze")
    llm = get_settings(req.conn)["llm"]
    if not llm["comment_ai"]:
        raise ApiError(403, "L’arbitrage IA des commentaires est désactivé.")
    row = req.conn.execute("SELECT * FROM comments WHERE id = ?", (int(cid),)).fetchone()
    if not row or row["hidden"]:
        raise ApiError(404, "Commentaire introuvable.")
    post = _fetch_post(req, row["post_id"])
    if row["ai"]:
        return {"ai": _json_or_none(row["ai"])}
    if len(row["body"]) < 30:
        raise ApiError(400, "Commentaire trop court pour être arbitré.")
    if not llm_status(req.conn, llm)["ready"] or "chat" not in HOOKS:
        raise ApiError(503, "Le modèle IA n’est pas encore configuré par l’administrateur.")
    _limit(f"commentai:{user['id']}", 10, 600, "Patientez un peu avant de demander un nouvel arbitrage.")
    consume_quota(req.conn, llm, user, req.ip, "comment")
    parent = row["parent_id"] and req.conn.execute("SELECT body FROM comments WHERE id = ?", (row["parent_id"],)).fetchone()
    prompt = f"Débat : {post['title']}\n" + (f"Message auquel il répond : {parent['body'][:1200]}\n" if parent else "") + f"\nCOMMENTAIRE À ÉVALUER :\n{row['body']}"
    cfg = llm_config(req.conn, llm)
    req.conn.commit()  # libère la base pendant l'appel au modèle
    try:
        ai = clean_ai(HOOKS["chat"](cfg, COMMENT_AI_PROMPT, prompt, 1200))
    except ApiError:
        raise
    except Exception as e:
        traceback.print_exc()
        raise ApiError(502, f"Le modèle IA n’a pas répondu : {str(e)[:160]}")
    ai |= {"by": user["username"], "at": now(), "model": cfg["keys"]["groq_models"][0] if cfg["analysis_engine"] == "groq" else "Gemini"}
    req.conn.execute("UPDATE comments SET ai = ? WHERE id = ? AND ai IS NULL", (json.dumps(ai, ensure_ascii=False), row["id"]))
    if row["user_id"] != user["id"]:
        notify(req.conn, row["user_id"], "ai", user["id"], row["post_id"], row["id"], f"{user['username']} a fait arbitrer votre commentaire par l’IA", ai["verdict"])
    return {"ai": ai}


@route("DELETE", r"/api/community/comments/(\d+)")
def delete_comment(req, cid):
    user = req.need()
    row = req.conn.execute("SELECT c.*, u.username FROM comments c JOIN users u ON u.id = c.user_id WHERE c.id = ?", (int(cid),)).fetchone()
    if not row:
        raise ApiError(404, "Commentaire introuvable.")
    staff = row["user_id"] != user["id"]
    if staff:
        req.need("moderator")
        req.audit("Commentaire supprimé", f"#{row['id']} : {row['body'][:80]}")
    label = row["body"][:120] or "(analyse jointe)"
    bundle = trash_bundle(req.conn, comment_ids=[row["id"]]) | {"roots": {"comments": [row["id"]]}, "post_id": row["post_id"]}
    tid = move_to_trash(req, "comment", label, {"id": row["user_id"], "username": row["username"]}, bundle, _str(req.query.get("reason"), 300))
    req.conn.execute("DELETE FROM comments WHERE id = ?", (row["id"],))
    _purge_reactions(req.conn)
    log_activity(req.conn, user["id"], "trash", f"Commentaire « {label[:80]} »" + (f" (de {row['username']})" if staff else ""), req.ip)
    if staff:
        notify(req.conn, row["user_id"], "moderation", user["id"], row["post_id"], title="Un de vos commentaires a été retiré par la modération",
               body=_str(req.query.get("reason"), 300) or label)
    return {"ok": True, "trash_id": tid}


@route("POST", "/api/community/reports")
def report(req):
    user = req.need()
    kind, tid, reason = req.body.get("type"), _int(req.body.get("id")), _str(req.body.get("reason"), 500)
    table = {"post": "posts", "comment": "comments"}.get(kind)
    if not table or not req.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (tid,)).fetchone():
        raise ApiError(404, "Contenu introuvable.")
    if len(reason) < 3:
        raise ApiError(400, "Précisez le motif du signalement.")
    _limit(f"report:{user['id']}", 10, 3600)
    req.conn.execute("INSERT INTO reports (target_type, target_id, user_id, reason, created_at) VALUES (?,?,?,?,?)", (kind, tid, user["id"], reason, now()))
    return 201, {"ok": True}


# ---------- Défi du jour, classement, profils ----------
_daily = {}


def daily_pick(conn):
    day = today()
    pick = _daily.get(day)
    if pick and conn.execute("SELECT 1 FROM posts WHERE id = ? AND hidden = 0", (pick[0],)).fetchone():
        return pick
    pool = []
    for r in conn.execute("SELECT id, title, snapshot FROM posts WHERE hidden = 0 AND kind = 'analysis' ORDER BY id DESC LIMIT 300"):
        try:
            snap = json.loads(r["snapshot"])
        except ValueError:
            continue
        pool += [(r["id"], r["title"], it) for it in quiz_items(r["id"], snap)]
    if not pool:
        return None
    pick = random.Random(day).choice(sorted(pool, key=lambda p: (p[0], p[2]["key"])))
    _daily.clear()
    _daily[day] = pick
    return pick


def daily_state(conn, user):
    pick = daily_pick(conn)
    day = today()
    players = conn.execute("SELECT COUNT(*), SUM(correct) FROM daily_answers WHERE day = ?", (day,)).fetchone()
    data = {"day": day, "question": None, "answered": None, "players": players[0], "rate": round(100 * (players[1] or 0) / players[0]) if players[0] else None}
    if not pick:
        return data
    pid, title, it = pick
    data["question"] = {"post_id": pid, "post_title": title, "speaker": it["speaker"], "quote": it["quote"], "choices": it["choices"]}
    if user:
        row = conn.execute("SELECT choice, correct FROM daily_answers WHERE day = ? AND user_id = ?", (day, user["id"])).fetchone()
        if row:
            data["answered"] = {"choice": row["choice"], "correct": bool(row["correct"]), "answer": it["answer"], "explication": it["explication"]}
    return data


@route("GET", "/api/community/daily")
def get_daily(req):
    if not get_settings(req.conn)["quiz_enabled"]:
        return {"disabled": True}
    return daily_state(req.conn, req.user)


@route("POST", "/api/community/daily")
def answer_daily(req):
    user = req.can("vote")
    if not get_settings(req.conn)["quiz_enabled"]:
        raise ApiError(403, "Les défis sont désactivés.")
    pick = daily_pick(req.conn)
    if not pick:
        raise ApiError(404, "Pas de défi aujourd’hui.")
    day, it = today(), pick[2]
    if req.conn.execute("SELECT 1 FROM daily_answers WHERE day = ? AND user_id = ?", (day, user["id"])).fetchone():
        raise ApiError(409, "Vous avez déjà relevé le défi du jour : revenez demain !")
    choice = _str(req.body.get("choice"), 80)
    if choice not in it["choices"]:
        raise ApiError(400, "Choix invalide.")
    correct = choice == it["answer"]
    req.conn.execute("INSERT INTO daily_answers (day, user_id, choice, correct, created_at) VALUES (?,?,?,?,?)", (day, user["id"], choice, int(correct), now()))
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    streak = user["streak"] + 1 if user["streak_day"] == yesterday else 1
    req.conn.execute("UPDATE users SET streak = ?, best_streak = MAX(best_streak, ?), streak_day = ? WHERE id = ?", (streak, streak, day, user["id"]))
    xp = add_xp(req.conn, user["id"], (25 if correct else 5) + min(streak, 10) * 2)
    return {"daily": daily_state(req.conn, user), "xp": xp, "streak": streak, "user": me_payload(req.conn, user["id"])}


@route("GET", "/api/community/leaderboard")
def leaderboard(req):
    rows = req.conn.execute("SELECT * FROM users WHERE status = 'active' ORDER BY xp DESC, created_at LIMIT 25").fetchall()
    week = int(time.time()) - 7 * 86400
    weekly = req.conn.execute("""SELECT u.id, u.username, u.xp, u.avatar, COUNT(*) AS n FROM (
            SELECT user_id FROM quiz_answers WHERE correct = 1 AND created_at >= ?
            UNION ALL SELECT user_id FROM daily_answers WHERE correct = 1 AND created_at >= ?) a
        JOIN users u ON u.id = a.user_id WHERE u.status = 'active' GROUP BY u.id ORDER BY n DESC, u.xp DESC LIMIT 10""", (week, week)).fetchall()
    data = {"top": [user_public(r) for r in rows],
            "weekly": [{"username": r["username"], "avatar": r["avatar"], "title": level_info(r["xp"])["title"], "found": r["n"]} for r in weekly]}
    if req.user:
        data["my_rank"] = req.conn.execute("SELECT COUNT(*) FROM users WHERE status = 'active' AND xp > ?", (req.user["xp"],)).fetchone()[0] + 1
    return data


@route("GET", r"/api/community/users/([^/]{1,200})")
def profile(req, username):
    row = _profile_row(req, username)
    stats = user_stats(req.conn, row["id"], row["best_streak"])
    uid = _uid(req)
    social = follow_counts(req.conn, row["id"]) | {
        "i_follow": bool(req.conn.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (uid, row["id"])).fetchone()),
        "follows_me": bool(req.conn.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (row["id"], uid)).fetchone()),
        "views": req.conn.execute("SELECT COALESCE(SUM(views), 0) FROM posts WHERE user_id = ? AND hidden = 0", (row["id"],)).fetchone()[0]}
    return {"user": user_public(row, private=req.user is not None and req.user["id"] == row["id"]), "stats": stats, "badges": badges_for(stats),
            "social": social, "rank": req.conn.execute("SELECT COUNT(*) FROM users WHERE status = 'active' AND xp > ?", (row["xp"],)).fetchone()[0] + 1}


def _profile_row(req, username):
    row = req.conn.execute("SELECT * FROM users WHERE username = ?", (urllib.parse.unquote(username),)).fetchone()
    if not row or (row["status"] != "active" and not req.is_mod()):
        raise ApiError(404, "Profil introuvable.")
    return row


@route("POST", r"/api/community/users/([^/]{1,200})/follow")
def follow(req, username):
    user = req.can("follow")
    row = _profile_row(req, username)
    if row["id"] == user["id"]:
        raise ApiError(400, "Vous ne pouvez pas vous abonner à vous-même.")
    _limit(f"follow:{user['id']}", 60, 600, "Vous vous abonnez très vite : patientez un instant.")
    if _bool(req.body.get("follow", True)):
        cur = req.conn.execute("INSERT OR IGNORE INTO follows (follower_id, followee_id, created_at) VALUES (?,?,?)", (user["id"], row["id"], now()))
        if cur.rowcount and not req.conn.execute("SELECT 1 FROM notifications WHERE user_id = ? AND actor_id = ? AND kind = 'follow' AND seen = 0",
                                                 (row["id"], user["id"])).fetchone():
            notify(req.conn, row["id"], "follow", user["id"], title=f"{user['username']} s’est abonné à vous")
    else:
        req.conn.execute("DELETE FROM follows WHERE follower_id = ? AND followee_id = ?", (user["id"], row["id"]))
    following = bool(req.conn.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (user["id"], row["id"])).fetchone())
    return follow_counts(req.conn, row["id"]) | {"i_follow": following}


@route("GET", r"/api/community/users/([^/]{1,200})/(followers|following)")
def follow_list(req, username, which):
    row = _profile_row(req, username)
    page = _int(req.query.get("page"), 1, 1, 1000)
    mine, other = ("followee_id", "follower_id") if which == "followers" else ("follower_id", "followee_id")
    rows = req.conn.execute(f"""SELECT u.*, f.created_at AS since,
            EXISTS(SELECT 1 FROM follows x WHERE x.follower_id = ? AND x.followee_id = u.id) AS i_follow
            FROM follows f JOIN users u ON u.id = f.{other} WHERE f.{mine} = ? AND u.status = 'active'
            ORDER BY f.created_at DESC LIMIT 51 OFFSET ?""", (_uid(req), row["id"], (page - 1) * 50)).fetchall()
    return {"users": [{k: v for k, v in user_public(r).items() if k in ("id", "username", "bio", "title", "level", "verified", "role", "avatar")}
                      | {"since": r["since"], "i_follow": bool(r["i_follow"])} for r in rows[:50]], "hasMore": len(rows) > 50}


# ---------- Messages privés entre membres ----------
DM_POLICIES = ("all", "following", "none")  # qui peut m'écrire : tout le monde, les comptes que je suis, personne
DM_MAX = 2000
_DM_PAIR = """((sender_id = :me AND recipient_id = :o AND hidden_s = 0) OR (sender_id = :o AND recipient_id = :me AND hidden_r = 0))"""


def _dm_unread(conn, uid):
    return conn.execute("SELECT COUNT(*) FROM dm_messages WHERE recipient_id = ? AND read_at IS NULL AND hidden_r = 0", (uid,)).fetchone()[0]


def _dm_peer(req, username):
    user = req.need()
    peer = req.conn.execute("SELECT * FROM users WHERE username = ?", (urllib.parse.unquote(username),)).fetchone()
    if not peer:
        raise ApiError(404, "Membre introuvable.")
    if peer["id"] == user["id"]:
        raise ApiError(400, "Vous ne pouvez pas vous écrire à vous-même.")
    if peer["status"] != "active" and not req.is_mod() and not req.conn.execute(
            "SELECT 1 FROM dm_messages WHERE (sender_id = ?1 AND recipient_id = ?2) OR (sender_id = ?2 AND recipient_id = ?1) LIMIT 1",
            (user["id"], peer["id"])).fetchone():
        raise ApiError(404, "Membre introuvable.")
    return user, peer


def _dm_state(req, me, peer):
    """Droit d'écrire à `peer` : blocages, suspension, restriction d'administration et préférence du destinataire"""
    c = req.conn
    blocked = bool(c.execute("SELECT 1 FROM dm_blocks WHERE blocker_id = ? AND blocked_id = ?", (me["id"], peer["id"])).fetchone())
    reason = ""
    if blocked:
        reason = "Vous avez bloqué ce membre : débloquez-le pour lui écrire."
    elif peer["status"] != "active":
        reason = "Ce compte est suspendu."
    elif "message" in blocked_perms(me):
        reason = f"« {PERMISSIONS['message']} » a été désactivé sur votre compte par l’administration."
    elif c.execute("SELECT 1 FROM dm_blocks WHERE blocker_id = ? AND blocked_id = ?", (peer["id"], me["id"])).fetchone():
        reason = "Ce membre n’accepte pas vos messages."
    elif not req.is_mod():
        policy = peer["dm_policy"] if peer["dm_policy"] in DM_POLICIES else "all"
        wrote_me = c.execute("SELECT 1 FROM dm_messages WHERE sender_id = ? AND recipient_id = ? LIMIT 1", (peer["id"], me["id"])).fetchone()
        follows_me = c.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (peer["id"], me["id"])).fetchone()
        if policy == "none" and not wrote_me:
            reason = "Ce membre n’accepte pas les messages privés."
        elif policy == "following" and not (follows_me or wrote_me):
            reason = "Ce membre n’accepte que les messages des comptes qu’il suit."
    return {"can_send": not reason, "reason": reason, "blocked": blocked}


def _dm_user(row):
    keep = ("id", "username", "title", "level", "verified", "role", "avatar")
    return {k: v for k, v in user_public(row).items() if k in keep} | {"active": row["status"] == "active"}


def _dm_msg(row, me):
    return {"id": row["id"], "body": row["body"], "at": row["created_at"], "mine": row["sender_id"] == me}


@route("GET", "/api/me/messages")
def dm_list(req):
    me = req.need()["id"]
    rows = req.conn.execute("""SELECT m.id AS mid, m.sender_id AS msender, m.body AS mbody, m.created_at AS mat, m.read_at AS mread, u.*,
            (SELECT COUNT(*) FROM dm_messages x WHERE x.sender_id = u.id AND x.recipient_id = :me AND x.read_at IS NULL AND x.hidden_r = 0) AS unread
            FROM dm_messages m JOIN users u ON u.id = CASE WHEN m.sender_id = :me THEN m.recipient_id ELSE m.sender_id END
            WHERE m.id IN (SELECT MAX(id) FROM dm_messages WHERE (sender_id = :me AND hidden_s = 0) OR (recipient_id = :me AND hidden_r = 0)
                           GROUP BY CASE WHEN sender_id = :me THEN recipient_id ELSE sender_id END)
            ORDER BY m.id DESC LIMIT 200""", {"me": me}).fetchall()
    convs = [{"user": _dm_user(r), "unread": r["unread"],
              "last": {"id": r["mid"], "body": r["mbody"][:160], "at": r["mat"], "mine": r["msender"] == me, "read": r["mread"] is not None}}
             for r in rows]
    return {"conversations": convs, "unread": _dm_unread(req.conn, me)}


@route("GET", r"/api/me/messages/([^/]{1,200})")
def dm_thread(req, username):
    user, peer = _dm_peer(req, username)
    p = {"me": user["id"], "o": peer["id"], "before": _int(req.query.get("before"), 0, 0), "after": _int(req.query.get("after"), 0, 0)}
    if p["after"]:
        rows, more = req.conn.execute(f"SELECT * FROM dm_messages WHERE {_DM_PAIR} AND id > :after ORDER BY id LIMIT 200", p).fetchall(), None
    else:
        rows = req.conn.execute(f"SELECT * FROM dm_messages WHERE {_DM_PAIR}{' AND id < :before' if p['before'] else ''} ORDER BY id DESC LIMIT 51", p).fetchall()
        more, rows = len(rows) > 50, rows[:50][::-1]
    req.conn.execute("UPDATE dm_messages SET read_at = ? WHERE sender_id = ? AND recipient_id = ? AND read_at IS NULL", (now(), peer["id"], user["id"]))
    seen = req.conn.execute("SELECT MAX(id) FROM dm_messages WHERE sender_id = ? AND recipient_id = ? AND read_at IS NOT NULL",
                            (user["id"], peer["id"])).fetchone()[0]
    social = {"i_follow": bool(req.conn.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (user["id"], peer["id"])).fetchone()),
              "follows_me": bool(req.conn.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followee_id = ?", (peer["id"], user["id"])).fetchone())}
    data = {"user": _dm_user(peer) | social, "messages": [_dm_msg(r, user["id"]) for r in rows], "seen_id": seen or 0,
            "unread": _dm_unread(req.conn, user["id"])} | _dm_state(req, user, peer)
    if more is not None:
        data["hasMore"] = more
    return data


@route("POST", r"/api/me/messages/([^/]{1,200})")
def dm_send(req, username):
    user, peer = _dm_peer(req, username)
    raw = req.body.get("body")
    body = _str(raw, DM_MAX + 1)
    if not body:
        raise ApiError(400, "Message vide.")
    if len(body) > DM_MAX:
        raise ApiError(400, f"Message trop long ({DM_MAX} caractères maximum).")
    state = _dm_state(req, user, peer)
    if not state["can_send"]:
        raise ApiError(403, state["reason"])
    _limit(f"dm:{user['id']}", 30, 60, "Vous envoyez beaucoup de messages : patientez un instant.")
    if not req.conn.execute("SELECT 1 FROM dm_messages WHERE (sender_id = ?1 AND recipient_id = ?2) OR (sender_id = ?2 AND recipient_id = ?1) LIMIT 1",
                            (user["id"], peer["id"])).fetchone():
        _limit(f"dmnew:{user['id']}", 20, 3600, "Trop de nouvelles conversations : patientez un peu.")
    cur = req.conn.execute("INSERT INTO dm_messages (sender_id, recipient_id, body, created_at) VALUES (?,?,?,?)", (user["id"], peer["id"], body, now()))
    row = req.conn.execute("SELECT * FROM dm_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"message": _dm_msg(row, user["id"])}


@route("DELETE", r"/api/me/messages/([^/]{1,200})")
def dm_hide(req, username):
    """Retire la conversation de ma messagerie (l'autre membre la conserve)"""
    user, peer = _dm_peer(req, username)
    req.conn.execute("UPDATE dm_messages SET hidden_s = 1 WHERE sender_id = ? AND recipient_id = ?", (user["id"], peer["id"]))
    req.conn.execute("UPDATE dm_messages SET hidden_r = 1, read_at = COALESCE(read_at, ?) WHERE sender_id = ? AND recipient_id = ?",
                     (now(), peer["id"], user["id"]))
    return {"ok": True, "unread": _dm_unread(req.conn, user["id"])}


@route("POST", r"/api/me/messages/([^/]{1,200})/block")
def dm_block(req, username):
    user, peer = _dm_peer(req, username)
    if _bool(req.body.get("block", True)):
        req.conn.execute("INSERT OR IGNORE INTO dm_blocks (blocker_id, blocked_id, created_at) VALUES (?,?,?)", (user["id"], peer["id"], now()))
        log_activity(req.conn, user["id"], "prefs", f"Messages de {peer['username']} bloqués", req.ip)
    else:
        req.conn.execute("DELETE FROM dm_blocks WHERE blocker_id = ? AND blocked_id = ?", (user["id"], peer["id"]))
        log_activity(req.conn, user["id"], "prefs", f"Messages de {peer['username']} débloqués", req.ip)
    return _dm_state(req, user, peer)


# ---------- Recherche de comptes et page Explorer de la communauté ----------
PEOPLE_SELECT = """SELECT u.*, (SELECT COUNT(*) FROM follows f WHERE f.followee_id = u.id) AS followers,
    (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id AND p.hidden = 0) AS posts,
    EXISTS(SELECT 1 FROM follows x WHERE x.follower_id = :me AND x.followee_id = u.id) AS i_follow,
    EXISTS(SELECT 1 FROM follows y WHERE y.follower_id = u.id AND y.followee_id = :me) AS follows_me
    FROM users u WHERE u.status = 'active'"""


def _like(text, prefix=False):
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{esc}%" if prefix else f"%{esc}%"


def _person(row, reason=None):
    keep = ("id", "username", "bio", "title", "level", "verified", "role", "avatar", "xp")
    data = {k: v for k, v in user_public(row).items() if k in keep} | {
        "followers": row["followers"], "posts": row["posts"], "i_follow": bool(row["i_follow"]), "follows_me": bool(row["follows_me"])}
    return data | ({"reason": reason} if reason else {})


@route("GET", "/api/community/users")
def search_users(req):
    q = _str(req.query.get("q"), 60)
    page = _int(req.query.get("page"), 1, 1, 200)
    _limit(f"usearch:{req.ip}", 90, 60, "Trop de recherches : patientez un instant.")
    params = {"me": _uid(req), "off": (page - 1) * 24}
    if q:
        params |= {"like": _like(q), "pre": _like(q, True), "exact": q}
        sql = PEOPLE_SELECT + r""" AND (u.username LIKE :like ESCAPE '\' OR u.bio LIKE :like ESCAPE '\')
            ORDER BY (u.username = :exact COLLATE NOCASE) DESC, (u.username LIKE :pre ESCAPE '\') DESC, followers DESC, u.xp DESC LIMIT 25 OFFSET :off"""
    else:
        sql = PEOPLE_SELECT + " ORDER BY followers DESC, posts DESC, u.xp DESC LIMIT 25 OFFSET :off"
    rows = req.conn.execute(sql, params).fetchall()
    return {"users": [_person(r) for r in rows[:24]], "hasMore": len(rows) > 24}


def _trending(conn, limit=12):
    """Sujets tendance : mots-clés des publications récentes, pondérés par l'engagement et la fraîcheur"""
    t, score, count, forms = now(), {}, {}, {}
    rows = conn.execute("""SELECT p.title, p.topic, p.created_at,
            (SELECT COUNT(*) FROM reactions r WHERE r.target_type = 'post' AND r.target_id = p.id) AS likes,
            (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id AND c.hidden = 0) AS comments
            FROM posts p WHERE p.hidden = 0 ORDER BY p.id DESC LIMIT 300""").fetchall()
    for r in rows:
        weight = (1 + math.log1p(r["likes"] + 2 * r["comments"])) / (1 + max(0, t - r["created_at"]) / (7 * 86400))
        toks = _tokens(r["title"], r["topic"])
        for tok in toks:
            score[tok] = score.get(tok, 0) + weight
            count[tok] = count.get(tok, 0) + 1
        for word in re.findall(r"[^\W\d_]{4,}", f"{r['title']} {r['topic']}"):
            key = _norm(word)
            if key in toks:
                forms.setdefault(key, {})
                forms[key][word.lower()] = forms[key].get(word.lower(), 0) + 1
    best = sorted(score, key=lambda k: (-(score[k] * (1 + 0.5 * (count[k] - 1))), k))[:limit]
    return [{"word": max(forms.get(k, {k: 1}).items(), key=lambda x: x[1])[0], "posts": count[k]} for k in best]


def _suggest_people(req, limit=6):
    """Comptes à suivre : amis d'amis, affinités de lecture, sujets communs, popularité"""
    c, uid = req.conn, _uid(req)
    rows = c.execute(PEOPLE_SELECT + " AND u.id != :me ORDER BY followers DESC, posts DESC, u.xp DESC LIMIT 200", {"me": uid}).fetchall()
    rows = [r for r in rows if not r["i_follow"]]
    if not rows:
        return []
    fof, authors, weights = {}, {}, {}
    if req.user:
        fof = dict(c.execute("""SELECT f2.followee_id, COUNT(*) FROM follows f1 JOIN follows f2 ON f2.follower_id = f1.followee_id
                                WHERE f1.follower_id = ? GROUP BY f2.followee_id""", (uid,)).fetchall())
        profile = interest_profile(c, req.user)
        authors, weights = profile["authors"], profile["weights"]
    ids = [r["id"] for r in rows]
    topics = {}
    for p in c.execute(f"SELECT user_id, title, topic FROM posts WHERE hidden = 0 AND user_id IN ({_marks(ids)}) ORDER BY id DESC LIMIT 1500", ids):
        bucket = topics.setdefault(p["user_id"], {})
        for tok in _post_tokens(p):
            bucket[tok] = bucket.get(tok, 0) + 1
    scored = []
    for r in rows:
        shared = sorted(((weights.get(k, 0) * n, k) for k, n in topics.get(r["id"], {}).items() if weights.get(k, 0) > 0), reverse=True)
        aff = max(0, authors.get(r["id"], 0))
        s = (math.log1p(r["followers"]) + 0.6 * math.log1p(r["posts"]) + 0.3 * math.log1p(r["xp"] / 50)
             + 2.5 * math.log1p(fof.get(r["id"], 0)) + 2 * aff / (aff + 4) + 1.5 * bool(r["follows_me"]) + (1.2 * shared[0][0] / (shared[0][0] + 6) if shared else 0))
        if r["follows_me"]:
            reason = "Vous suit"
        elif fof.get(r["id"]):
            reason = f"Suivi par {fof[r['id']]} de vos abonnements" if fof[r["id"]] > 1 else "Suivi par l’un de vos abonnements"
        elif aff > 1:
            reason = "Vous réagissez souvent à ses débats"
        elif shared:
            reason = f"Publie sur « {shared[0][1]} »"
        elif r["followers"] >= 3:
            reason = "Populaire dans la communauté"
        elif r["posts"]:
            reason = f"{r['posts']} débat{'s' if r['posts'] > 1 else ''} publié{'s' if r['posts'] > 1 else ''}"
        else:
            reason = "Nouveau membre"
        scored.append((s * (0.9 + 0.2 * random.Random(f"{uid}:{today()}:{r['id']}").random()), r, reason))
    scored.sort(key=lambda x: -x[0])
    return [_person(r, reason) for _, r, reason in scored[:limit]]


@route("GET", "/api/community/explore")
def community_explore(req):
    c, t = req.conn, now()
    _limit(f"explore:{req.ip}", 60, 60)
    top = c.execute(POST_SELECT + " WHERE p.hidden = 0 AND p.created_at >= ? ORDER BY (likes + 2 * comments + votes) DESC, p.created_at DESC LIMIT 4",
                    (_uid(req), t - 7 * 86400)).fetchall()
    if not top:
        top = c.execute(POST_SELECT + " WHERE p.hidden = 0 ORDER BY (likes + 2 * comments + votes) DESC, p.created_at DESC LIMIT 4", (_uid(req),)).fetchall()

    def one(sql, *args):
        return c.execute(sql, args).fetchone()[0] or 0
    return {
        "trending": _trending(c),
        "people": _suggest_people(req),
        "top": [post_summary(r) for r in top],
        "stats": {"members": one("SELECT COUNT(*) FROM users WHERE status = 'active'"),
                  "posts": one("SELECT COUNT(*) FROM posts WHERE hidden = 0"),
                  "week_posts": one("SELECT COUNT(*) FROM posts WHERE hidden = 0 AND created_at >= ?", t - 7 * 86400),
                  "today_comments": one("SELECT COUNT(*) FROM comments WHERE hidden = 0 AND created_at >= ?", t - 86400),
                  "lives": len(active_lives(c, 50))},
    }


# ---------- Publicités ----------
@route("GET", r"/api/ads/([\w-]{1,40})/go")
def ad_click(req, ad_id):
    s = get_settings(req.conn)
    ad = next((a for a in s["ads"] if a.get("id") == ad_id and a.get("active")), None) if s["ads_enabled"] else None
    if not ad or not _safe_url(ad.get("url")):
        raise ApiError(404, "Annonce introuvable.")
    req.conn.execute("INSERT INTO ad_clicks (ad_id, clicks) VALUES (?, 1) ON CONFLICT(ad_id) DO UPDATE SET clicks = clicks + 1", (ad_id,))
    return Redirect(ad["url"])


# ---------- Administration ----------
@route("GET", "/api/admin/stats")
def admin_stats(req):
    req.need("moderator")
    c = req.conn

    def one(sql, *args):
        return c.execute(sql, args).fetchone()[0] or 0
    week = now() - 7 * 86400
    start = now() - 13 * 86400
    series = {}
    for table, col in (("users", "users"), ("posts", "posts"), ("comments", "comments")):
        for r in c.execute(f"SELECT date(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) AS n FROM {table} WHERE created_at >= ? GROUP BY d", (start,)):
            series.setdefault(r["d"], {})[col] = r["n"]
    for r in c.execute("SELECT date(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) AS n FROM (SELECT created_at FROM quiz_answers UNION ALL SELECT created_at FROM daily_answers) WHERE created_at >= ? GROUP BY d", (start,)):
        series.setdefault(r["d"], {})["answers"] = r["n"]
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    return {
        "users": one("SELECT COUNT(*) FROM users"), "new_users": one("SELECT COUNT(*) FROM users WHERE created_at >= ?", week),
        "active_users": one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", week), "banned": one("SELECT COUNT(*) FROM users WHERE status != 'active'"),
        "posts": one("SELECT COUNT(*) FROM posts"), "new_posts": one("SELECT COUNT(*) FROM posts WHERE created_at >= ?", week),
        "hidden_posts": one("SELECT COUNT(*) FROM posts WHERE hidden = 1"), "comments": one("SELECT COUNT(*) FROM comments"),
        "answers": one("SELECT COUNT(*) FROM quiz_answers") + one("SELECT COUNT(*) FROM daily_answers"),
        "daily_players": one("SELECT COUNT(*) FROM daily_answers WHERE day = ?", today()),
        "open_reports": one("SELECT COUNT(*) FROM reports WHERE resolved = 0"), "ad_clicks": one("SELECT SUM(clicks) FROM ad_clicks"),
        "ai_today": one("SELECT COUNT(*) FROM llm_usage WHERE created_at >= ?", _midnight()),
        "ai_week": one("SELECT COUNT(*) FROM llm_usage WHERE created_at >= ?", week),
        "series": [{"day": d} | {k: series.get(d, {}).get(k, 0) for k in ("users", "posts", "comments", "answers")} for d in days],
    }


USER_FILTERS = {"admin": "u.role = 'admin'", "moderator": "u.role = 'moderator'", "user": "u.role = 'user'", "banned": "u.status != 'active'",
                "restricted": "u.blocked != '[]'", "verified": "u.verified = 1"}


@route("GET", "/api/admin/users")
def admin_users(req):
    req.need("admin")
    q, page = _str(req.query.get("q"), 100), _int(req.query.get("page"), 1, 1, 1000)
    extra = USER_FILTERS.get(req.query.get("filter") or "", "1")
    rows = req.conn.execute(f"""SELECT u.*, (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id) AS posts,
        (SELECT COUNT(*) FROM comments c WHERE c.user_id = u.id) AS comments,
        (SELECT COUNT(*) FROM llm_usage l WHERE l.user_id = u.id AND l.created_at >= ?) AS ai_today FROM users u
        WHERE (u.username LIKE ? OR u.email LIKE ?) AND {extra} ORDER BY u.created_at DESC LIMIT ? OFFSET ?""",
                            (_midnight(), f"%{q}%", f"%{q}%", 51, (page - 1) * 50)).fetchall()
    return {"users": [user_public(r, private=True) | {"posts": r["posts"], "comments": r["comments"], "last_seen": r["last_seen"], "quota": r["quota"],
                                                    "note": r["note"], "email_ok": bool(r["email_ok"]), "ai_today": r["ai_today"]} for r in rows[:50]],
            "hasMore": len(rows) > 50, "permissions": PERMISSIONS}


def _user_row(req, uid):
    row = req.conn.execute("SELECT * FROM users WHERE id = ?", (int(uid),)).fetchone()
    if not row:
        raise ApiError(404, "Compte introuvable.")
    return row


@route("GET", r"/api/admin/users/(\d+)")
def admin_user_detail(req, uid):
    req.need("admin")
    row, c = _user_row(req, uid), req.conn
    llm = get_settings(c)["llm"]
    usage = {r["d"]: r["n"] for r in c.execute("""SELECT date(created_at, 'unixepoch', 'localtime') AS d, COUNT(*) AS n FROM llm_usage
                                                   WHERE user_id = ? AND created_at >= ? GROUP BY d""", (row["id"], now() - 13 * 86400))}
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    return {
        "user": user_public(row, private=True) | {"quota": row["quota"], "effective_quota": user_quota(llm, row), "note": row["note"],
                                                  "email_ok": bool(row["email_ok"]), "last_seen": row["last_seen"], "ai_today": usage_today(c, row["id"]),
                                                  "sessions": c.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND expires_at > ?", (row["id"], now())).fetchone()[0],
                                                  "session_list": [dict(s) for s in c.execute(
                                                      """SELECT rowid AS id, device, ip, created_at, seen_at FROM sessions WHERE user_id = ? AND expires_at > ?
                                                         ORDER BY seen_at DESC LIMIT 20""", (row["id"], now()))]},
        "stats": user_stats(c, row["id"], row["best_streak"]) | follow_counts(c, row["id"]),
        "usage": [{"day": d, "n": usage.get(d, 0)} for d in days],
        "posts": [dict(r) for r in c.execute("SELECT id, title, hidden, created_at FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT 8", (row["id"],))],
        "comments": [dict(r) | {"body": r["body"][:160]} for r in c.execute("SELECT id, post_id, body, hidden, created_at FROM comments WHERE user_id = ? ORDER BY id DESC LIMIT 8", (row["id"],))],
        "permissions": PERMISSIONS, "default_quota": llm["daily_quota"],
    }


@route("PATCH", r"/api/admin/users/(\d+)")
def admin_update_user(req, uid):
    admin = req.need("admin")
    row, b = _user_row(req, uid), req.body
    name = row["username"]
    if row["id"] == admin["id"] and ("role" in b or "status" in b or "blocked" in b):
        raise ApiError(400, "Vous ne pouvez pas modifier votre propre rôle, statut ou vos droits.")
    role_change = "role" in b and b["role"] != row["role"]
    ident_change = ("username" in b and _str(b["username"], 24) != row["username"]) or \
                   ("email" in b and _str(b["email"], 254).lower() != row["email"].lower())
    if role_change or ident_change or (b.get("status") == "banned" and row["status"] != "banned"):
        req.sudo()  # élévation de privilège, prise de contrôle d'identité ou suspension : mot de passe re-confirmé
    target_role = b["role"] if role_change else row["role"]
    if b.get("status") == "banned" and target_role == "admin":
        raise ApiError(400, "Rétrogradez d’abord cet administrateur avant de le suspendre.")

    def update(column, value, action, detail):
        try:
            req.conn.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (value, row["id"]))
        except sqlite3.IntegrityError:
            raise ApiError(409, "Ce pseudo ou cet e-mail est déjà utilisé.")
        req.audit(action, f"{name} : {detail}")
        log_activity(req.conn, row["id"], "admin", f"{action} ({detail})" if detail else action)
    if role_change:
        if b["role"] not in ROLES:
            raise ApiError(400, "Rôle invalide.")
        if RANK[b["role"]] > RANK[row["role"]] and row["status"] != "active":
            raise ApiError(400, "Réactivez ce compte avant de lui confier un rôle.")
        if row["role"] == "admin" and req.conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'").fetchone()[0] <= 1:
            raise ApiError(400, "Il doit toujours rester au moins un administrateur.")
        update("role", b["role"], "Rôle modifié", f"{row['role']} → {b['role']}")
        req.conn.execute("UPDATE sessions SET sudo_until = 0 WHERE user_id = ?", (row["id"],))
        up = RANK[b["role"]] > RANK[row["role"]]
        notify(req.conn, row["id"], "security", admin["id"], title=f"Votre rôle a changé : {ROLE_LABELS[b['role']]}",
               body=f"{admin['username']} vous a {'promu' if up else 'rétrogradé'} ({ROLE_LABELS[row['role']]} → {ROLE_LABELS[b['role']]}).")
        alert_admins(req, f"Rôle modifié : {name} est désormais {ROLE_LABELS[b['role']]}", f"Par {admin['username']} (IP {req.ip}).")
    if "status" in b and b["status"] != row["status"]:
        if b["status"] not in ("active", "banned"):
            raise ApiError(400, "Statut invalide.")
        update("status", b["status"], "Compte suspendu" if b["status"] == "banned" else "Compte réactivé", b["status"])
        if b["status"] == "banned":
            req.conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            req.conn.execute("UPDATE lives SET status = 'ended', ended_at = ? WHERE user_id = ? AND status = 'live'", (now(), row["id"]))
    if "xp" in b:
        xp = _int(b["xp"], row["xp"], 0, 10_000_000)
        update("xp", xp, "XP ajustée", f"{row['xp']} → {xp}")
    if "username" in b and _str(b["username"], 24) != row["username"]:
        new = _str(b["username"], 24)
        if not USERNAME_RE.match(new):
            raise ApiError(400, "Pseudo invalide (3 à 24 caractères : lettres, chiffres, . _ -).")
        update("username", new, "Pseudo modifié", f"→ {new}")
        notify(req.conn, row["id"], "security", admin["id"], title="Votre pseudo a été modifié par l’administration", body=f"{row['username']} → {new}")
    if "email" in b and _str(b["email"], 254).lower() != row["email"].lower():
        email = _str(b["email"], 254).lower()
        if not EMAIL_RE.match(email):
            raise ApiError(400, "Adresse e-mail invalide.")
        update("email", email, "E-mail modifié", f"{row['email']} → {email}")
        notify(req.conn, row["id"], "security", admin["id"], title="Votre adresse e-mail a été modifiée par l’administration",
               body=f"Nouvelle adresse : {email}")
        alert_admins(req, f"E-mail de {name} modifié", f"Par {admin['username']} : {row['email']} → {email}")
    if "bio" in b:
        update("bio", _str(b["bio"], 300), "Bio modifiée", "")
    if "blocked" in b:
        blocked = sorted({p for p in _list(b["blocked"]) if p in PERMISSIONS})
        update("blocked", json.dumps(blocked), "Droits modifiés", ", ".join(PERMISSIONS[p] for p in blocked) or "tout autorisé")
    if "quota" in b:
        quota = None if b["quota"] in (None, "") else _int(b["quota"], 0, 0, 10_000)
        update("quota", quota, "Quota IA modifié", "par défaut" if quota is None else ("illimité" if quota == 0 else f"{quota}/jour"))
    if "verified" in b:
        update("verified", int(_bool(b["verified"])), "Badge vérifié", "ajouté" if _bool(b["verified"]) else "retiré")
    if "email_ok" in b:
        update("email_ok", int(_bool(b["email_ok"])), "E-mails", "autorisés" if _bool(b["email_ok"]) else "bloqués")
    if "note" in b:
        req.conn.execute("UPDATE users SET note = ? WHERE id = ?", (_str(b["note"], 1000), row["id"]))
    if _bool(b.get("logout_all")):
        req.conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        req.audit("Sessions fermées", name)
        log_activity(req.conn, row["id"], "admin", "Toutes les sessions fermées")
    return {"ok": True}


@route("GET", r"/api/admin/users/(\d+)/activity")
def admin_user_activity(req, uid):
    req.need("admin")
    row = _user_row(req, uid)
    return activity_feed(req.conn, row["id"], req.query.get("type", ""), _int(req.query.get("page"), 1, 1, 1000), True,
                         _str(req.query.get("q"), 80), req.query.get("period", ""))


@route("POST", r"/api/admin/users/(\d+)/reset-password")
def admin_reset_password(req, uid):
    req.need("admin")
    admin = req.sudo()
    row = _user_row(req, uid)
    if row["role"] == "admin" and row["id"] != admin["id"]:
        raise ApiError(403, "Le mot de passe d’un autre administrateur ne peut pas être réinitialisé : rétrogradez-le d’abord.")
    temp = secrets.token_urlsafe(9)
    req.conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(temp), row["id"]))
    req.conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
    req.audit("Mot de passe réinitialisé", row["username"])
    log_activity(req.conn, row["id"], "admin", "Mot de passe réinitialisé")
    alert_admins(req, f"Mot de passe de {row['username']} réinitialisé", f"Par {admin['username']} (IP {req.ip}).")
    out = {"password": temp, "emailed": False}
    if _bool(req.body.get("email")):
        smtp, pwd = get_settings(req.conn)["smtp"], get_secret(req.conn, "smtp")
        req.conn.commit()
        try:
            send_mail(smtp, pwd, [(row["email"], row["username"])], "Votre nouveau mot de passe rhetora",
                      f"Votre mot de passe a été réinitialisé par l’administration.\n\nMot de passe temporaire : {temp}\n\n"
                      "Connectez-vous puis changez-le depuis votre profil.")
            out["emailed"] = True
        except (ApiError, OSError, smtplib.SMTPException) as e:
            out["email_error"] = str(e)[:200]
    return out


@route("DELETE", r"/api/admin/users/(\d+)")
def admin_delete_user(req, uid):
    req.need("admin")
    admin = req.sudo()
    row = _user_row(req, uid)
    if row["id"] == admin["id"]:
        raise ApiError(400, "Vous ne pouvez pas supprimer votre propre compte ici.")
    if row["role"] == "admin":
        raise ApiError(400, "Rétrogradez d’abord cet administrateur avant de supprimer son compte.")
    reason = _str(req.query.get("reason"), 300)
    bundle = trash_bundle(req.conn, user_id=row["id"]) | {"roots": {"users": [row["id"]]}}
    tid = move_to_trash(req, "user", f"{row['username']} ({row['email']})", row, bundle, reason)
    req.conn.execute("DELETE FROM users WHERE id = ?", (row["id"],))
    _purge_reactions(req.conn)
    req.audit("Compte supprimé (corbeille)", f"{row['username']} ({row['email']})" + (f" — {reason}" if reason else ""))
    alert_admins(req, f"Compte supprimé : {row['username']}", f"Par {admin['username']} — restaurable depuis la corbeille.")
    return {"ok": True, "trash_id": tid}


# ---------- E-mails & messages ----------
def _oneline(value):
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def send_mail(smtp, password, recipients, subject, body):
    """Envoie un e-mail texte à chaque (adresse, nom) ; renvoie (envoyés, erreurs)"""
    if not smtp.get("host") or not smtp.get("from_email"):
        raise ApiError(400, "Serveur d’e-mail non configuré (Administration › Paramètres › E-mails).")
    ctx = ssl.create_default_context()
    sent, errors = 0, []
    if smtp["security"] == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=20)
    with server:
        if smtp["security"] == "starttls":
            server.starttls(context=ctx)
        if smtp["username"]:
            server.login(smtp["username"], password)
        for email, name in recipients:
            msg = EmailMessage()
            msg["From"] = formataddr((_oneline(smtp["from_name"]) or "rhetora", _oneline(smtp["from_email"])))
            msg["To"] = _oneline(email)
            msg["Subject"] = _oneline(subject)
            msg.set_content(f"Bonjour {_oneline(name)},\n\n{body}\n\n— L’équipe rhetora")
            try:
                server.send_message(msg)
                sent += 1
            except (smtplib.SMTPException, ValueError) as e:
                errors.append(f"{email} : {e}")
    return sent, errors


def _mail_job(bid, smtp, password, recipients, subject, body):
    sent = 0
    try:
        sent, errors = send_mail(smtp, password, recipients, subject, body)
        status = f"{sent} envoyé{'s' if sent > 1 else ''}" + (f", {len(errors)} échec(s)" if errors else "")
    except (ApiError, OSError, smtplib.SMTPException) as e:
        status = f"Échec : {e}"[:300]
    with tx() as conn:
        conn.execute("UPDATE broadcasts SET emailed = ?, email_status = ? WHERE id = ?", (sent, status, bid))


MESSAGE_TARGETS = {"all": "status = 'active'", "active": "status = 'active' AND last_seen >= :week",
                   "admin": "status = 'active' AND role = 'admin'", "moderator": "status = 'active' AND role = 'moderator'",
                   "user": "status = 'active' AND role = 'user'"}


@route("POST", "/api/admin/messages")
def admin_send_message(req):
    admin = req.need("admin")
    b = req.body
    subject, body, target = _oneline(_str(b.get("subject"), 150)), _str(b.get("body"), 5000), b.get("target")
    if not subject or not body:
        raise ApiError(400, "Objet et message obligatoires.")
    if target == "users":
        ids = [i for i in (_int(x) for x in _list(b.get("user_ids"))[:500]) if i]
        rows = req.conn.execute(f"SELECT * FROM users WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
    elif target in MESSAGE_TARGETS:
        rows = req.conn.execute(f"SELECT * FROM users WHERE {MESSAGE_TARGETS[target]}", {"week": now() - 7 * 86400}).fetchall()
        rows = [r for r in rows if r["id"] != admin["id"]]
    else:
        raise ApiError(400, "Destinataires invalides.")
    if not rows:
        raise ApiError(400, "Aucun destinataire.")
    email = _bool(b.get("email"))
    smtp, pwd = get_settings(req.conn)["smtp"], get_secret(req.conn, "smtp")
    if email and (not smtp["host"] or not smtp["from_email"]):
        raise ApiError(400, "Serveur d’e-mail non configuré (Administration › Paramètres › E-mails).")
    _limit(f"broadcast:{admin['id']}", 30, 3600, "Trop d’envois : patientez un peu.")
    for r in rows:
        req.conn.execute("INSERT INTO notifications (user_id, kind, actor_id, title, body, created_at) VALUES (?,?,?,?,?,?)",
                         (r["id"], "message", admin["id"], subject, body, now()))
    mail_to = [(r["email"], r["username"]) for r in rows if r["email_ok"]] if email else []
    label = (", ".join(r["username"] for r in rows[:5]) + ("…" if len(rows) > 5 else "")) if target == "users" else target
    status = f"envoi de {len(mail_to)} e-mail(s)…" if mail_to else ("aucun destinataire n’accepte les e-mails" if email else "")
    cur = req.conn.execute("""INSERT INTO broadcasts (sender, target, subject, body, recipients, email_status, created_at)
                              VALUES (?,?,?,?,?,?,?)""", (admin["username"], label, subject, body, len(rows), status, now()))
    req.audit("Message envoyé", f"« {subject[:60]} » → {label} ({len(rows)})")
    req.conn.commit()
    if mail_to:
        threading.Thread(target=_mail_job, args=(cur.lastrowid, smtp, pwd, mail_to, subject, body), daemon=True).start()
    return {"ok": True, "recipients": len(rows), "emails": len(mail_to)}


@route("GET", "/api/admin/messages")
def admin_messages(req):
    req.need("admin")
    return {"messages": [dict(r) for r in req.conn.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 50")]}


@route("POST", "/api/admin/smtp-test")
def admin_smtp_test(req):
    admin = req.need("admin")
    smtp, pwd = get_settings(req.conn)["smtp"], get_secret(req.conn, "smtp")
    req.conn.commit()
    try:
        sent, errors = send_mail(smtp, pwd, [(admin["email"], admin["username"])], "Test d’envoi rhetora",
                                 "Si vous lisez ce message, l’envoi d’e-mails de rhetora fonctionne.")
    except (OSError, smtplib.SMTPException) as e:
        raise ApiError(502, f"Échec de l’envoi : {str(e)[:200]}")
    if errors:
        raise ApiError(502, errors[0][:200])
    return {"ok": True, "to": admin["email"]}


@route("POST", "/api/admin/llm/test")
def admin_llm_test(req):
    req.need("admin")
    llm = get_settings(req.conn)["llm"]
    if not llm_status(req.conn, llm)["ready"]:
        raise ApiError(400, "Aucune clé API pour le moteur d’analyse choisi.")
    if "test" not in HOOKS:
        raise ApiError(503, "Test indisponible.")
    cfg = llm_config(req.conn, llm)
    req.conn.commit()
    try:
        return HOOKS["test"](cfg)
    except Exception as e:
        raise ApiError(502, f"Le modèle n’a pas répondu : {str(e)[:200]}")


# ---------- Notifications ----------
@route("GET", "/api/me/notifications")
def my_notifications(req):
    uid = req.need()["id"]
    req.conn.execute("""DELETE FROM notifications WHERE user_id = ?1 AND id NOT IN
                        (SELECT id FROM notifications WHERE user_id = ?1 ORDER BY id DESC LIMIT 200)""", (uid,))
    rows = req.conn.execute("""SELECT n.*, u.username AS actor, u.avatar AS actor_avatar FROM notifications n LEFT JOIN users u ON u.id = n.actor_id
                               WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 60""", (uid,)).fetchall()
    return {"notifications": [{k: r[k] for k in ("id", "kind", "actor", "actor_avatar", "post_id", "comment_id", "title", "body", "created_at")} | {"seen": bool(r["seen"])}
                              for r in rows],
            "unread": req.conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND seen = 0", (uid,)).fetchone()[0]}


@route("POST", "/api/me/notifications/read")
def read_notifications(req):
    uid = req.need()["id"]
    if _bool(req.body.get("all")):
        req.conn.execute("UPDATE notifications SET seen = 1 WHERE user_id = ?", (uid,))
    else:
        for nid in [_int(x) for x in _list(req.body.get("ids"))[:200]]:
            req.conn.execute("UPDATE notifications SET seen = 1 WHERE id = ? AND user_id = ?", (nid, uid))
    return {"ok": True}


@route("DELETE", r"/api/me/notifications/(\d+)")
def delete_notification(req, nid):
    uid = req.need()["id"]
    req.conn.execute("DELETE FROM notifications WHERE id = ? AND user_id = ?", (int(nid), uid))
    return {"ok": True}


@route("GET", "/api/admin/reports")
def admin_reports(req):
    req.need("moderator")
    out = []
    for r in req.conn.execute("""SELECT r.*, u.username FROM reports r LEFT JOIN users u ON u.id = r.user_id
                                 ORDER BY r.resolved, r.created_at DESC LIMIT 200"""):
        item = {"id": r["id"], "type": r["target_type"], "target_id": r["target_id"], "reason": r["reason"], "resolved": bool(r["resolved"]),
                "created_at": r["created_at"], "reporter": r["username"] or "(compte supprimé)", "exists": False}
        if r["target_type"] == "post":
            t = req.conn.execute("SELECT p.id, p.title, p.hidden, u.username FROM posts p JOIN users u ON u.id = p.user_id WHERE p.id = ?", (r["target_id"],)).fetchone()
            if t:
                item |= {"exists": True, "post_id": t["id"], "excerpt": t["title"], "hidden": bool(t["hidden"]), "author": t["username"]}
        else:
            t = req.conn.execute("SELECT c.id, c.post_id, c.body, c.hidden, u.username FROM comments c JOIN users u ON u.id = c.user_id WHERE c.id = ?", (r["target_id"],)).fetchone()
            if t:
                item |= {"exists": True, "post_id": t["post_id"], "excerpt": t["body"][:200], "hidden": bool(t["hidden"]), "author": t["username"]}
        out.append(item)
    return {"reports": out}


@route("PATCH", r"/api/admin/reports/(\d+)")
def admin_resolve_report(req, rid):
    req.need("moderator")
    resolved = _bool(req.body.get("resolved", True))
    if not req.conn.execute("UPDATE reports SET resolved = ? WHERE id = ?", (int(resolved), int(rid))).rowcount:
        raise ApiError(404, "Signalement introuvable.")
    req.audit("Signalement " + ("classé" if resolved else "rouvert"), f"#{rid}")
    return {"ok": True}


def _clean_ad(ad):
    if not isinstance(ad, dict):
        raise ApiError(400, "Annonce invalide.")
    url = _safe_url(ad.get("url"))
    if not url:
        raise ApiError(400, f"Annonce « {_str(ad.get('title'), 40) or 'sans titre'} » : lien http(s) obligatoire.")
    ad_id = _str(ad.get("id"), 40)
    return {"id": ad_id if re.fullmatch(r"[\w-]{1,40}", ad_id) else secrets.token_hex(4), "title": _str(ad.get("title"), 80),
            "text": _str(ad.get("text"), 220), "sponsor": _str(ad.get("sponsor"), 60), "url": url, "image": _safe_url(ad.get("image")),
            "cta": _str(ad.get("cta"), 30) or "Découvrir", "placement": ad.get("placement") if ad.get("placement") in PLACEMENTS else "all",
            "active": _bool(ad.get("active"))}


@route("GET", "/api/admin/settings")
def admin_get_settings(req):
    req.need("admin")
    c = req.conn
    clicks = {r["ad_id"]: r["clicks"] for r in c.execute("SELECT * FROM ad_clicks")}
    week = now() - 7 * 86400
    top = [dict(r) for r in c.execute("""SELECT u.username, COUNT(*) AS n FROM llm_usage l JOIN users u ON u.id = l.user_id
                                         WHERE l.created_at >= ? GROUP BY l.user_id ORDER BY n DESC LIMIT 8""", (week,))]
    kinds = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) AS n FROM llm_usage WHERE created_at >= ? GROUP BY kind", (week,))}
    settings = get_settings(c)
    return {"settings": settings, "clicks": clicks, "secrets": {k: secret_info(c, k) for k in SECRETS}, "status": llm_status(c, settings["llm"]),
            "catalog": {"analysis_engines": ANALYSIS_ENGINES, "transcribe_engines": TRANSCRIBE_ENGINES, "groq_models": GROQ_MODELS, "permissions": PERMISSIONS},
            "llm_usage": {"today": c.execute("SELECT COUNT(*) FROM llm_usage WHERE created_at >= ?", (_midnight(),)).fetchone()[0],
                          "week": sum(kinds.values()), "kinds": kinds, "top": top}}


def _clean_llm(raw, current):
    if not isinstance(raw, dict):
        raise ApiError(400, "Réglages IA invalides.")
    out = dict(current)
    if raw.get("analysis_engine") in ANALYSIS_ENGINES:
        out["analysis_engine"] = raw["analysis_engine"]
    if raw.get("transcribe_engine") in TRANSCRIBE_ENGINES:
        out["transcribe_engine"] = raw["transcribe_engine"]
    if "groq_models" in raw:
        models = []
        for m in _list(raw["groq_models"])[:12]:
            m = _str(m, 80)
            if not re.fullmatch(r"[\w./:-]{2,80}", m):
                raise ApiError(400, f"Nom de modèle invalide : « {m[:40]} ».")
            if m not in models:
                models.append(m)
        if not models:
            raise ApiError(400, "Indiquez au moins un modèle Groq.")
        out["groq_models"] = models
    if "temperature" in raw:
        try:
            out["temperature"] = round(min(max(float(raw["temperature"]), 0.0), 1.5), 2)
        except (TypeError, ValueError):
            raise ApiError(400, "Température invalide.")
    for key in ("require_account", "comment_ai"):
        if key in raw:
            out[key] = _bool(raw[key])
    if "daily_quota" in raw:
        out["daily_quota"] = _int(raw["daily_quota"], 20, 0, 10_000)
    return out


def _clean_smtp(raw, current):
    if not isinstance(raw, dict):
        raise ApiError(400, "Réglages e-mail invalides.")
    out = dict(current)
    if "host" in raw:
        host = _oneline(_str(raw["host"], 200))
        if host and not re.fullmatch(r"[\w.-]+", host):
            raise ApiError(400, "Serveur SMTP invalide.")
        out["host"] = host
    if "port" in raw:
        out["port"] = _int(raw["port"], 587, 1, 65535)
    if raw.get("security") in ("starttls", "ssl", "none"):
        out["security"] = raw["security"]
    if "username" in raw:
        out["username"] = _oneline(_str(raw["username"], 200))
    if "from_email" in raw:
        email = _oneline(_str(raw["from_email"], 254))
        if email and not EMAIL_RE.match(email):
            raise ApiError(400, "Adresse d’expédition invalide.")
        out["from_email"] = email
    if "from_name" in raw:
        out["from_name"] = _oneline(_str(raw["from_name"], 60))
    return out


@route("PUT", "/api/admin/settings")
def admin_put_settings(req):
    req.need("admin")
    current, body, changed = get_settings(req.conn), req.body, []
    sec = body.get("secrets") if isinstance(body.get("secrets"), dict) else {}
    if any(_str(sec.get(n), 300) for n in SECRETS) or any(n in SECRETS for n in _list(body.get("clear_secrets"))):
        req.sudo()  # les clés d'API et mots de passe SMTP sont des secrets : re-confirmation exigée
    for key in ("registrations_open", "publishing_enabled", "comments_enabled", "quiz_enabled", "ads_enabled", "lives_enabled"):
        if key in body:
            current[key] = _bool(body[key])
    if "trash_days" in body:
        current["trash_days"] = _int(body["trash_days"], 30, 1, 365)
    if "announcement" in body:
        current["announcement"] = _str(body["announcement"], 300)
    if body.get("announcement_tone") in TONES:
        current["announcement_tone"] = body["announcement_tone"]
    if "ad_frequency" in body:
        current["ad_frequency"] = _int(body["ad_frequency"], 6, 3, 30)
    if "ads" in body:
        ads = [_clean_ad(a) for a in _list(body["ads"])[:30]]
        if len({a["id"] for a in ads}) != len(ads):
            raise ApiError(400, "Identifiants d’annonces en double.")
        current["ads"] = ads
    if "llm" in body:
        current["llm"] = _clean_llm(body["llm"], current["llm"])
    if "smtp" in body:
        current["smtp"] = _clean_smtp(body["smtp"], current["smtp"])
    for key, value in current.items():
        if key in body:
            changed.append(key)
            req.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
    for name in SECRETS:
        value = _str(sec.get(name), 300)
        if value:
            if name != "smtp" and not re.fullmatch(r"[\x21-\x7e]{8,300}", value):
                raise ApiError(400, f"Clé {name} invalide.")
            set_secret(req.conn, name, value)
            changed.append(f"clé {name}")
    for name in _list(body.get("clear_secrets")):
        if name in SECRETS:
            set_secret(req.conn, name, "")
            changed.append(f"clé {name} retirée")
    req.audit("Paramètres modifiés", ", ".join(changed))
    return admin_get_settings(req)


AUDIT_CATS = {
    "content": "(action LIKE 'Publication%' OR action LIKE 'Commentaire%' OR action LIKE '%direct%')",
    "reports": "action LIKE 'Signalement%'",
    "trash": "(action LIKE 'Restauré%' OR action LIKE 'Supprimé définitivement%' OR action LIKE 'Corbeille%')",
    "system": "(action LIKE 'Paramètres%' OR action = 'Message envoyé')",
}
AUDIT_CATS["accounts"] = "NOT (" + " OR ".join(AUDIT_CATS.values()) + ")"


@route("GET", "/api/admin/audit")
def admin_audit(req):
    """Journal d'audit paginé (curseur `before` = id), avec recherche et compteurs par catégorie"""
    req.need("admin")
    q, cat, before = _str(req.query.get("q"), 80), req.query.get("cat", ""), _int(req.query.get("before"), 0, 0)
    where, args = ["1"], []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(username LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\')")
        args += [like] * 3
    sums = ", ".join(f"COALESCE(SUM(CASE WHEN {cond} THEN 1 ELSE 0 END), 0)" for cond in AUDIT_CATS.values())
    row = req.conn.execute(f"SELECT COUNT(*), {sums} FROM audit WHERE {' AND '.join(where)}", args).fetchone()
    counts = {"": row[0]} | dict(zip(AUDIT_CATS, row[1:]))
    if cat in AUDIT_CATS:
        where.append(AUDIT_CATS[cat])
    if before:
        where.append("id < ?")
        args.append(before)
    rows = req.conn.execute(f"SELECT * FROM audit WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 61", args).fetchall()
    return {"entries": [dict(r) for r in rows[:60]], "hasMore": len(rows) > 60, "counts": counts}


ROLE_LABELS = {"user": "membre", "moderator": "modérateur", "admin": "administrateur"}


def alert_admins(req, title, body=""):
    """Alerte de sécurité envoyée à tous les autres administrateurs actifs"""
    actor = req.user["id"] if req.user else None
    for r in req.conn.execute("SELECT id FROM users WHERE role = 'admin' AND status = 'active' AND id IS NOT ?", (actor,)).fetchall():
        notify(req.conn, r["id"], "security", actor, title=title, body=body)


def _marks(seq):
    return ",".join("?" * len(seq))


# ---------- Identifiants jamais réutilisés (une restauration ne doit pas entrer en collision) ----------
def _floor(conn, table):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"idfloor:{table}",)).fetchone()
    return _int(row["value"], 0) if row else 0


def _next_id(conn, table):
    return max(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0], _floor(conn, table)) + 1


def _raise_floor(conn, table, ids):
    ids = [i for i in ids if isinstance(i, int)]
    if ids and max(ids) > _floor(conn, table):
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"idfloor:{table}", str(max(ids))))


# ---------- Corbeille ----------
TRASH_TABLES = ("users", "media", "posts", "comments", "follows", "verdicts", "quiz_answers", "daily_answers", "reactions",
                "notifications", "activity", "post_views", "post_feedback", "lives", "live_chat", "dm_messages", "dm_blocks")
TRASH_ROOT_ERRORS = {
    "users": "Ce pseudo ou cet e-mail est désormais utilisé par un autre compte : impossible de restaurer.",
    "posts": "L’auteur de cette publication n’existe plus : restaurez d’abord son compte.",
    "comments": "La publication (ou le commentaire parent) n’existe plus : restaurez-la d’abord.",
}


def _dump(conn, table, where, args):
    return [{k: {"$b64": base64.b64encode(r[k]).decode()} if isinstance(r[k], bytes) else r[k] for k in r.keys()}
            for r in conn.execute(f"SELECT * FROM {table} WHERE {where}", args)]


def trash_bundle(conn, user_id=None, post_ids=(), comment_ids=()):
    """Photographie complète d'un contenu et de tout ce qui en dépend, pour pouvoir le restaurer à l'identique"""
    tables = {t: [] for t in TRASH_TABLES}
    relink = {}
    post_ids, comment_ids = list(post_ids), list(comment_ids)
    if user_id:
        u = (user_id,)
        tables["users"] = _dump(conn, "users", "id = ?", u)
        post_ids += [r[0] for r in conn.execute("SELECT id FROM posts WHERE user_id = ?", u)]
        comment_ids += [r[0] for r in conn.execute("SELECT id FROM comments WHERE user_id = ?", u)]
        for table in ("media", "verdicts", "quiz_answers", "daily_answers", "reactions", "notifications", "activity",
                      "post_views", "post_feedback", "lives", "live_chat"):
            tables[table] += _dump(conn, table, "user_id = ?", u)
        tables["follows"] += _dump(conn, "follows", "follower_id = ?1 OR followee_id = ?1", u)
        tables["dm_messages"] += _dump(conn, "dm_messages", "sender_id = ?1 OR recipient_id = ?1", u)
        tables["dm_blocks"] += _dump(conn, "dm_blocks", "blocker_id = ?1 OR blocked_id = ?1", u)
        tables["live_chat"] += _dump(conn, "live_chat", "live_id IN (SELECT id FROM lives WHERE user_id = ?)", u)
        for table, col in (("llm_usage", "user_id"), ("notifications", "actor_id"), ("reports", "user_id")):
            relink[f"{table}.{col}"] = [r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {col} = ?", u)]
    if post_ids:
        m = _marks(post_ids)
        tables["posts"] += _dump(conn, "posts", f"id IN ({m})", post_ids)
        comment_ids += [r[0] for r in conn.execute(f"SELECT id FROM comments WHERE post_id IN ({m})", post_ids)]
        for table in ("verdicts", "quiz_answers", "notifications", "post_views", "post_feedback"):
            tables[table] += _dump(conn, table, f"post_id IN ({m})", post_ids)
        tables["reactions"] += _dump(conn, "reactions", f"target_type = 'post' AND target_id IN ({m})", post_ids)
        images = [i for r in tables["posts"] for i in _images(r.get("images"))]
        if images:
            tables["media"] += _dump(conn, "media", f"id IN ({_marks(images)})", images)
    if comment_ids:
        m = _marks(comment_ids)
        ids = [r[0] for r in conn.execute(f"""WITH RECURSIVE d(id) AS (SELECT id FROM comments WHERE id IN ({m})
                   UNION SELECT c.id FROM comments c JOIN d ON c.parent_id = d.id) SELECT id FROM d""", comment_ids)]
        m = _marks(ids)
        tables["comments"] += _dump(conn, "comments", f"id IN ({m})", ids)
        tables["reactions"] += _dump(conn, "reactions", f"target_type = 'comment' AND target_id IN ({m})", ids)
        tables["notifications"] += _dump(conn, "notifications", f"comment_id IN ({m})", ids)
    for table, rows in tables.items():
        seen, unique = set(), []
        for row in rows:
            key = json.dumps(row, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        tables[table] = sorted(unique, key=lambda r: r["id"]) if unique and isinstance(unique[0].get("id"), int) else unique
    return {"tables": tables, "relink": relink}


def move_to_trash(req, kind, label, owner, bundle, reason=""):
    c = req.conn
    days = _int(get_settings(c)["trash_days"], 30, 1, 365)
    data = json.dumps(bundle, ensure_ascii=False)
    summary = {t: len(r) for t, r in bundle["tables"].items() if r}
    actor = req.user
    for table in ("users", "posts", "comments", "lives"):
        _raise_floor(c, table, [r["id"] for r in bundle["tables"][table]])
    cur = c.execute("""INSERT INTO trash (kind, label, owner_id, owner_name, deleted_by, deleted_by_name, by_staff, reason, summary, data, size,
                       created_at, purge_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (kind, label[:200], owner["id"], owner["username"], actor["id"], actor["username"], int(actor["id"] != owner["id"]),
                     reason[:300], json.dumps(summary), data, len(data), now(), now() + days * 86400))
    return cur.lastrowid


def _table_columns(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def restore_bundle(conn, bundle):
    """Réinsère les lignes archivées (parents d'abord) ; une ligne racine qui échoue annule toute la restauration"""
    tables, roots, skipped = bundle.get("tables") or {}, bundle.get("roots") or {}, 0
    for table in TRASH_TABLES:  # noms de tables issus d'une constante, colonnes validées par PRAGMA
        allowed = _table_columns(conn, table)
        for row in tables.get(table) or []:
            cols = [k for k in row if k in allowed]
            vals = [base64.b64decode(row[k]["$b64"]) if isinstance(row[k], dict) else row[k] for k in cols]
            try:
                conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({_marks(cols)})", vals)
            except sqlite3.IntegrityError:
                if row.get("id") in roots.get(table, ()):
                    raise ApiError(409, TRASH_ROOT_ERRORS.get(table, "Restauration impossible."))
                skipped += 1
    users = tables.get("users") or []
    if users:
        for key, ids in (bundle.get("relink") or {}).items():
            table, col = key.split(".")
            if (table, col) in (("llm_usage", "user_id"), ("notifications", "actor_id"), ("reports", "user_id")) and ids:
                ids = [i for i in ids if isinstance(i, int)]
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} IS NULL AND id IN ({_marks(ids)})", [users[0]["id"]] + ids)
    for mid in [r["id"] for r in tables.get("media") or [] if r.get("kind") == "post"]:
        conn.execute("UPDATE media SET used = 1 WHERE id = ?", (mid,))
    return skipped


TRASH_COLS = "id, kind, label, owner_id, owner_name, deleted_by_name, by_staff, reason, summary, size, created_at, purge_at"


def _trash_item(r):
    return dict(r) | {"by_staff": bool(r["by_staff"]), "summary": _json_or_none(r["summary"]) or {}}


def _restore_trash(req, row):
    bundle = json.loads(row["data"])
    skipped = restore_bundle(req.conn, bundle)
    req.conn.execute("DELETE FROM trash WHERE id = ?", (row["id"],))
    out = {"ok": True, "skipped": skipped, "kind": row["kind"]}
    if row["kind"] == "post":
        out["post_id"] = bundle["roots"]["posts"][0]
    elif row["kind"] == "comment":
        out["post_id"] = bundle.get("post_id")
    else:
        out["username"] = row["owner_name"]
    return out


@route("GET", "/api/me/trash")
def my_trash(req):
    user = req.need()
    rows = req.conn.execute(f"SELECT {TRASH_COLS} FROM trash WHERE owner_id = ? AND deleted_by = ? AND kind != 'user' ORDER BY id DESC LIMIT 200",
                            (user["id"], user["id"])).fetchall()
    return {"items": [_trash_item(r) for r in rows], "days": get_settings(req.conn)["trash_days"]}


def _my_trash_row(req, tid):
    user = req.need()
    row = req.conn.execute("SELECT * FROM trash WHERE id = ? AND owner_id = ? AND deleted_by = ? AND kind != 'user'",
                           (int(tid), user["id"], user["id"])).fetchone()
    if not row:
        raise ApiError(404, "Élément introuvable dans votre corbeille.")
    return user, row


@route("POST", r"/api/me/trash/(\d+)/restore")
def my_trash_restore(req, tid):
    user, row = _my_trash_row(req, tid)
    out = _restore_trash(req, row)
    log_activity(req.conn, user["id"], "restore", f"{'Publication' if row['kind'] == 'post' else 'Commentaire'} « {row['label'][:80]} »", req.ip)
    return out


@route("DELETE", r"/api/me/trash/(\d+)")
def my_trash_purge(req, tid):
    user, row = _my_trash_row(req, tid)
    req.conn.execute("DELETE FROM trash WHERE id = ?", (row["id"],))
    log_activity(req.conn, user["id"], "purge", f"Supprimé définitivement : « {row['label'][:80]} »", req.ip)
    return {"ok": True}


@route("GET", "/api/admin/trash")
def admin_trash(req):
    req.need("admin")
    where, args = [], []
    if req.query.get("kind") in ("post", "comment", "user"):
        where.append("kind = ?")
        args.append(req.query["kind"])
    if req.query.get("q"):
        where.append("(label LIKE ? OR owner_name LIKE ? OR deleted_by_name LIKE ?)")
        args += [f"%{_str(req.query['q'], 80)}%"] * 3
    rows = req.conn.execute(f"SELECT {TRASH_COLS} FROM trash{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY id DESC LIMIT 300", args).fetchall()
    totals = {r["kind"]: {"n": r["n"], "size": r["size"]} for r in req.conn.execute("SELECT kind, COUNT(*) AS n, SUM(size) AS size FROM trash GROUP BY kind")}
    return {"items": [_trash_item(r) for r in rows], "totals": totals, "days": get_settings(req.conn)["trash_days"]}


def _admin_trash_row(req, tid):
    row = req.conn.execute("SELECT * FROM trash WHERE id = ?", (int(tid),)).fetchone()
    if not row:
        raise ApiError(404, "Élément introuvable dans la corbeille.")
    return row


@route("POST", r"/api/admin/trash/(\d+)/restore")
def admin_trash_restore(req, tid):
    admin = req.need("admin")
    row = _admin_trash_row(req, tid)
    if row["kind"] == "user":
        req.sudo()
    out = _restore_trash(req, row)
    req.audit("Restauré depuis la corbeille", f"{row['kind']} : {row['label'][:120]}")
    if row["owner_id"] and row["owner_id"] != admin["id"]:
        if row["kind"] == "user":
            log_activity(req.conn, row["owner_id"], "restore", f"Compte restauré par {admin['username']}")
            alert_admins(req, f"Compte restauré : {row['owner_name']}", f"Par {admin['username']}.")
        else:
            notify(req.conn, row["owner_id"], "moderation", admin["id"], out.get("post_id"),
                   title=f"Votre contenu « {row['label'][:60]} » a été rétabli par l’administration")
    return out


@route("DELETE", r"/api/admin/trash/(\d+)")
def admin_trash_purge(req, tid):
    req.need("admin")
    req.sudo()
    row = _admin_trash_row(req, tid)
    req.conn.execute("DELETE FROM trash WHERE id = ?", (row["id"],))
    req.audit("Supprimé définitivement", f"{row['kind']} : {row['label'][:120]}")
    if row["kind"] == "user":
        alert_admins(req, f"Compte supprimé définitivement : {row['owner_name']}", f"Par {req.user['username']} (IP {req.ip}).")
    return {"ok": True}


@route("POST", "/api/admin/trash/empty")
def admin_trash_empty(req):
    req.need("admin")
    req.sudo()
    kind = req.body.get("kind") if req.body.get("kind") in ("post", "comment", "user") else None
    n = req.conn.execute("DELETE FROM trash" + (" WHERE kind = ?" if kind else ""), (kind,) if kind else ()).rowcount
    req.audit("Corbeille vidée", f"{n} élément(s)" + (f" ({kind})" if kind else ""))
    alert_admins(req, "La corbeille a été vidée", f"{n} élément(s) supprimé(s) définitivement par {req.user['username']}.")
    return {"ok": True, "deleted": n}


@route("POST", "/api/me/delete")
def delete_me(req):
    """Suppression de son propre compte : mot de passe + pseudo exigés ; le compte reste restaurable par l'administration"""
    user = req.need()
    key = f"sudo:{user['id']}"
    if _blocked(key, 5, 600):
        raise ApiError(429, "Trop de tentatives : réessayez dans quelques minutes.")
    pw = req.body.get("password")
    if not isinstance(pw, str) or not check_password(pw, user["pw_hash"]):
        _record(key)
        log_activity(req.conn, user["id"], "sudo_failed", "Suppression du compte : mot de passe erroné", req.ip)
        req.conn.commit()
        raise ApiError(403, "Mot de passe incorrect.")
    if _str(req.body.get("confirm"), 40) != user["username"]:
        raise ApiError(400, "Recopiez exactement votre pseudo pour confirmer.")
    if user["role"] == "admin" and req.conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'").fetchone()[0] <= 1:
        raise ApiError(400, "Vous êtes le dernier administrateur : nommez-en un autre avant de supprimer votre compte.")
    bundle = trash_bundle(req.conn, user_id=user["id"]) | {"roots": {"users": [user["id"]]}}
    move_to_trash(req, "user", f"{user['username']} ({user['email']})", user, bundle, "Suppression demandée par le titulaire")
    req.audit("Compte supprimé par son titulaire", user["username"])
    req.conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    _purge_reactions(req.conn)
    req.out_headers.append(_cookie("", 0))
    return {"ok": True}


_maint = {"at": 0}


def maintenance(conn, force=False):
    """Purge de la corbeille expirée, des images orphelines et des directs abandonnés (au plus toutes les 10 min)"""
    t = now()
    if not force and t - _maint["at"] < 600:
        return
    _maint["at"] = t
    conn.execute("DELETE FROM trash WHERE purge_at < ?", (t,))
    conn.execute("UPDATE lives SET status = 'ended', ended_at = last_push WHERE status = 'live' AND last_push < ?", (t - LIVE_STALE,))
    conn.execute("DELETE FROM media WHERE kind = 'post' AND used = 0 AND created_at < ?", (t - 86400,))
    conn.execute("""DELETE FROM media WHERE kind = 'post' AND used = 1 AND created_at < ?
                    AND NOT EXISTS (SELECT 1 FROM posts p WHERE instr(p.images, media.id))
                    AND NOT EXISTS (SELECT 1 FROM trash t WHERE instr(t.data, media.id))""", (t - 3600,))
    conn.execute("DELETE FROM post_views WHERE at < ?", (t - 180 * 86400,))
    conn.execute("DELETE FROM live_chat WHERE live_id IN (SELECT id FROM lives WHERE status = 'ended' AND ended_at < ?)", (t - 30 * 86400,))


# ---------- Sécurité : re-confirmation et sessions ----------
@route("POST", "/api/me/sudo")
def sudo(req):
    user = req.need()
    key = f"sudo:{user['id']}"
    if _blocked(key, 5, 600):
        raise ApiError(429, "Trop de tentatives : réessayez dans quelques minutes.")
    pw = req.body.get("password")
    if not isinstance(pw, str) or not check_password(pw, user["pw_hash"]):
        _record(key)
        log_activity(req.conn, user["id"], "sudo_failed", "Mot de passe erroné (action sensible)", req.ip)
        req.conn.commit()
        raise ApiError(403, "Mot de passe incorrect.")
    until = now() + SUDO_TTL
    req.conn.execute("UPDATE sessions SET sudo_until = ? WHERE rowid = ?", (until, user["sid"]))
    log_activity(req.conn, user["id"], "sudo", f"Mode sécurisé activé ({SUDO_TTL // 60} min)", req.ip)
    return {"until": until}


@route("GET", "/api/me/sessions")
def my_sessions(req):
    user = req.need()
    rows = req.conn.execute("""SELECT rowid AS id, created_at, expires_at, ip, device, seen_at, sudo_until FROM sessions
                               WHERE user_id = ? AND expires_at > ? ORDER BY seen_at DESC""", (user["id"], now())).fetchall()
    return {"sessions": [{k: r[k] for k in ("id", "created_at", "expires_at", "ip", "device", "seen_at")}
                         | {"current": r["id"] == user["sid"], "sudo": r["sudo_until"] > now()} for r in rows]}


@route("DELETE", r"/api/me/sessions/(\d+)")
def revoke_session(req, sid):
    user = req.need()
    if int(sid) == user["sid"]:
        raise ApiError(400, "Utilisez « Se déconnecter » pour fermer la session en cours.")
    row = req.conn.execute("SELECT device, ip FROM sessions WHERE rowid = ? AND user_id = ?", (int(sid), user["id"])).fetchone()
    if not row:
        raise ApiError(404, "Session introuvable.")
    req.conn.execute("DELETE FROM sessions WHERE rowid = ?", (int(sid),))
    log_activity(req.conn, user["id"], "session_revoked", f"{row['device'] or 'Appareil inconnu'} ({row['ip'] or '?'})", req.ip)
    return {"ok": True}


@route("POST", "/api/me/sessions/revoke-others")
def revoke_other_sessions(req):
    user = req.need()
    n = req.conn.execute("DELETE FROM sessions WHERE user_id = ? AND rowid != ?", (user["id"], user["sid"])).rowcount
    log_activity(req.conn, user["id"], "session_revoked", f"{n} autre(s) session(s) fermée(s)", req.ip)
    return {"ok": True, "closed": n}


# ---------- Images : photo de profil et illustrations de publications ----------
def _image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


@route("POST", "/api/me/media")
def upload_media(req):
    kind = req.body.get("kind")
    if kind not in ("avatar", "post"):
        raise ApiError(400, "Type d’image inconnu.")
    user = req.can("profile" if kind == "avatar" else "publish")
    raw = req.body.get("data")
    m = re.fullmatch(r"data:image/[a-z+.-]{2,20};base64,([A-Za-z0-9+/=]+)", raw if isinstance(raw, str) else "")
    if not m:
        raise ApiError(400, "Image illisible.")
    try:
        data = base64.b64decode(m.group(1), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "Image illisible.")
    if len(data) > IMAGE_MAX:
        raise ApiError(413, "Image trop lourde (1,5 Mo maximum).")
    mime = _image_mime(data)
    if not mime:
        raise ApiError(400, "Format non pris en charge : PNG, JPEG, WebP ou GIF uniquement.")
    _limit(f"media:{user['id']}", 40, 3600, "Trop d’images envoyées : patientez un peu.")
    mid = secrets.token_urlsafe(12)
    req.conn.execute("INSERT INTO media (id, user_id, kind, mime, data, size, used, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (mid, user["id"], kind, mime, data, len(data), int(kind == "avatar"), now()))
    if kind == "avatar":
        req.conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (mid, user["id"]))
        if user["avatar"]:
            req.conn.execute("DELETE FROM media WHERE id = ? AND user_id = ?", (user["avatar"], user["id"]))
        log_activity(req.conn, user["id"], "profile", "Photo de profil modifiée", req.ip)
        return 201, {"id": mid, "user": me_payload(req.conn, user["id"])}
    stale = req.conn.execute("SELECT id FROM media WHERE user_id = ? AND kind = 'post' AND used = 0 ORDER BY created_at DESC LIMIT -1 OFFSET 12",
                             (user["id"],)).fetchall()
    for r in stale:
        req.conn.execute("DELETE FROM media WHERE id = ?", (r["id"],))
    return 201, {"id": mid}


@route("DELETE", "/api/me/avatar")
def delete_avatar(req):
    user = req.need()
    if user["avatar"]:
        req.conn.execute("UPDATE users SET avatar = NULL WHERE id = ?", (user["id"],))
        req.conn.execute("DELETE FROM media WHERE id = ? AND user_id = ?", (user["avatar"], user["id"]))
        log_activity(req.conn, user["id"], "profile", "Photo de profil retirée", req.ip)
    return {"user": me_payload(req.conn, user["id"])}


@route("GET", r"/api/media/([A-Za-z0-9_-]{16,24})")
def get_media(req, mid):
    row = req.conn.execute("SELECT m.mime, m.data FROM media m JOIN users u ON u.id = m.user_id WHERE m.id = ? AND u.status = 'active'", (mid,)).fetchone()
    if not row:
        raise ApiError(404, "Image introuvable.")
    return Binary(row["mime"], bytes(row["data"]))


# ---------- Débats en direct ----------
_presence, _presence_lock = {}, threading.Lock()


def _listeners(live_id, key=None):
    """Auditeurs présents (signe de vie depuis moins de 20 s), l'hôte n'est pas compté"""
    t = time.time()
    with _presence_lock:
        room = _presence.setdefault(live_id, {})
        if key:
            room[key] = t
        for k in [k for k, v in room.items() if t - v > 20]:
            del room[k]
        return len(room)


def _live_row(req, lid):
    row = req.conn.execute("""SELECT l.*, u.username, u.avatar, u.verified, u.xp, u.status AS host_status FROM lives l
                              JOIN users u ON u.id = l.user_id WHERE l.id = ?""", (int(lid),)).fetchone()
    if not row or (row["host_status"] != "active" and not req.is_mod()):
        raise ApiError(404, "Direct introuvable.")
    return row


def _is_live(row):
    return row["status"] == "live" and row["last_push"] >= now() - LIVE_STALE


def _live_public(row, listeners):
    entries = _json_or_none(row["entries"]) or []
    last = entries[-1] if entries else None
    return {"id": row["id"], "title": row["title"], "status": "live" if _is_live(row) else "ended", "state": row["state"],
            "host": {"username": row["username"], "avatar": row["avatar"], "verified": bool(row["verified"]), "title": level_info(row["xp"])["title"]},
            "speakers": _json_or_none(row["speakers"]) or [], "started_at": row["started_at"], "ended_at": row["ended_at"],
            "listeners": listeners, "peak": max(row["peak"], listeners), "cheers": row["cheers"], "version": row["version"],
            "count": len(entries), "last": last and {"speaker": last.get("speaker"), "text": (last.get("text") or "")[:160]}}


def _live_chat(conn, lid, since):
    rows = conn.execute("""SELECT m.id, m.body, m.created_at, m.user_id, u.username, u.avatar, u.verified FROM live_chat m
                           JOIN users u ON u.id = m.user_id WHERE m.live_id = ? AND m.id > ? ORDER BY m.id DESC LIMIT 60""", (lid, since)).fetchall()
    return [{"id": r["id"], "body": r["body"], "at": r["created_at"], "username": r["username"], "avatar": r["avatar"],
             "verified": bool(r["verified"])} for r in reversed(rows)]


def _clean_speakers(raw):
    return [s for s in (_str(x, 60) for x in _list(raw)[:8]) if s]


def _clean_entries(raw):
    out = []
    for e in _list(raw)[-1500:]:
        if isinstance(e, dict):
            text = _str(e.get("text"), 4000)
            if text:
                out.append({"speaker": _str(e.get("speaker"), 60) or "?", "time": _str(e.get("time"), 12), "text": text})
    return out


def active_lives(conn, limit=20):
    rows = conn.execute("""SELECT l.*, u.username, u.avatar, u.verified, u.xp FROM lives l JOIN users u ON u.id = l.user_id
                           WHERE l.status = 'live' AND l.last_push >= ? AND u.status = 'active' ORDER BY l.started_at DESC LIMIT ?""",
                        (now() - LIVE_STALE, limit)).fetchall()
    return sorted((_live_public(r, _listeners(r["id"])) for r in rows), key=lambda x: -x["listeners"])


@route("GET", "/api/community/lives")
def list_lives(req):
    return {"lives": active_lives(req.conn)}


@route("POST", "/api/community/lives")
def start_live(req):
    user = req.can("publish")
    s = get_settings(req.conn)
    if not req.is_mod() and (not s["lives_enabled"] or not s["publishing_enabled"]):
        raise ApiError(403, "La diffusion en direct est désactivée pour le moment.")
    title = _str(req.body.get("title"), 140) or "Débat en direct"
    if len(title) < 3:
        raise ApiError(400, "Donnez un titre d’au moins 3 caractères.")
    _limit(f"live:{user['id']}", 10, 3600, "Vous avez lancé beaucoup de directs : patientez un moment.")
    req.conn.execute("UPDATE lives SET status = 'ended', ended_at = ? WHERE user_id = ? AND status = 'live'", (now(), user["id"]))
    lid = _next_id(req.conn, "lives")
    req.conn.execute("INSERT INTO lives (id, user_id, title, speakers, started_at, last_push) VALUES (?,?,?,?,?,?)",
                     (lid, user["id"], title, json.dumps(_clean_speakers(req.body.get("speakers")), ensure_ascii=False), now(), now()))
    for r in req.conn.execute("SELECT follower_id FROM follows WHERE followee_id = ? LIMIT 2000", (user["id"],)).fetchall():
        notify(req.conn, r[0], "live", user["id"], title=f"{user['username']} est en direct", body=title)
    return 201, {"id": lid}


@route("PUT", r"/api/community/lives/(\d+)")
def push_live(req, lid):
    user = req.need()
    row = _live_row(req, lid)
    if row["user_id"] != user["id"]:
        raise ApiError(403, "Seul l’hôte peut diffuser ce direct.")
    if row["status"] != "live":
        raise ApiError(409, "Ce direct est terminé.", code="ended")
    _limit(f"livepush:{user['id']}", 120, 60, "Synchronisation trop fréquente.")
    b, sets = req.body, {"last_push": now()}
    if "entries" in b:
        data = json.dumps(_clean_entries(b["entries"]), ensure_ascii=False)
        if len(data) > 1_500_000:
            raise ApiError(413, "Transcription trop longue pour la diffusion.")
        if data != row["entries"]:
            sets |= {"entries": data, "version": row["version"] + 1}
    if isinstance(b.get("now"), dict):
        sets["now_text"] = json.dumps({"speaker": _str(b["now"].get("speaker"), 60), "text": _str(b["now"].get("text"), 600)}, ensure_ascii=False)
    if b.get("state") in ("recording", "paused"):
        sets["state"] = b["state"]
    if "speakers" in b:
        sets["speakers"] = json.dumps(_clean_speakers(b["speakers"]), ensure_ascii=False)
    if _str(b.get("title"), 140):
        sets["title"] = _str(b.get("title"), 140)
    req.conn.execute(f"UPDATE lives SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", [*sets.values(), row["id"]])
    listeners = _listeners(row["id"])
    return {"version": sets.get("version", row["version"]), "listeners": listeners, "peak": max(row["peak"], listeners),
            "cheers": row["cheers"], "chat": _live_chat(req.conn, row["id"], _int(b.get("chat_since"), 0))}


@route("GET", r"/api/community/lives/(\d+)")
def watch_live(req, lid):
    row = _live_row(req, lid)
    q, uid = req.query, _uid(req)
    live, listeners = _is_live(row), _listeners(row["id"])
    viewer = _str(q.get("viewer"), 40)
    if live and row["user_id"] != uid and (req.user or viewer):
        key = f"u{uid}" if req.user else "a" + hashlib.sha256(f"{req.ip}|{viewer}".encode()).hexdigest()[:16]
        listeners = _listeners(row["id"], key)
        if listeners > row["peak"]:
            req.conn.execute("UPDATE lives SET peak = MAX(peak, ?) WHERE id = ?", (listeners, row["id"]))
    data = _live_public(row, listeners) | {"mine": row["user_id"] == uid, "now": (_json_or_none(row["now_text"]) if live else None),
                                           "chat": _live_chat(req.conn, row["id"], _int(q.get("chat"), 0))}
    if _int(q.get("v"), -1) != row["version"]:
        data["entries"] = _json_or_none(row["entries"]) or []
    return data


@route("POST", r"/api/community/lives/(\d+)/chat")
def live_chat_post(req, lid):
    user = req.can("comment")
    row = _live_row(req, lid)
    if not _is_live(row):
        raise ApiError(409, "Ce direct est terminé.", code="ended")
    body = re.sub(r"\s+", " ", _str(req.body.get("body"), 300))
    if not body:
        raise ApiError(400, "Message vide.")
    _limit(f"livechat:{user['id']}", 12, 30, "Doucement : un message toutes les quelques secondes.")
    cur = req.conn.execute("INSERT INTO live_chat (live_id, user_id, body, created_at) VALUES (?,?,?,?)", (row["id"], user["id"], body, now()))
    return 201, {"id": cur.lastrowid}


@route("DELETE", r"/api/community/lives/(\d+)/chat/(\d+)")
def live_chat_delete(req, lid, mid):
    user = req.need()
    row = _live_row(req, lid)
    msg = req.conn.execute("SELECT * FROM live_chat WHERE id = ? AND live_id = ?", (int(mid), row["id"])).fetchone()
    if not msg:
        raise ApiError(404, "Message introuvable.")
    if user["id"] not in (msg["user_id"], row["user_id"]):
        req.need("moderator")
        req.audit("Message de direct supprimé", f"#{row['id']} : {msg['body'][:80]}")
    req.conn.execute("DELETE FROM live_chat WHERE id = ?", (msg["id"],))
    return {"ok": True}


@route("POST", r"/api/community/lives/(\d+)/cheer")
def live_cheer(req, lid):
    row = _live_row(req, lid)
    if not _is_live(row):
        raise ApiError(409, "Ce direct est terminé.", code="ended")
    _limit(f"cheer:{req.user['id'] if req.user else req.ip}", 20, 60, "Merci pour l’enthousiasme ! Patientez un instant.")
    req.conn.execute("UPDATE lives SET cheers = cheers + 1 WHERE id = ?", (row["id"],))
    return {"cheers": row["cheers"] + 1}


@route("POST", r"/api/community/lives/(\d+)/end")
def end_live(req, lid):
    user = req.need()
    row = _live_row(req, lid)
    if row["user_id"] != user["id"]:
        req.need("moderator")
        req.audit("Direct interrompu", f"#{row['id']} {row['title']} (de {row['username']})")
        notify(req.conn, row["user_id"], "moderation", user["id"], title=f"Votre direct « {row['title'][:60]} » a été interrompu par la modération")
    req.conn.execute("UPDATE lives SET status = 'ended', ended_at = ? WHERE id = ? AND status = 'live'", (now(), row["id"]))
    with _presence_lock:
        _presence.pop(row["id"], None)
    return {"ok": True, "peak": row["peak"]}


# ---------- Fil « Pour vous » ----------
STOPWORDS = set("""avec dans pour plus moins sans sous entre vers chez comme mais donc alors ainsi aussi tout tous toute toutes cette celle celui ceux
elles leur leurs nous vous etre avoir fait faire faut sont etait quoi quel quelle quels quelles quand comment pourquoi debat debats contre selon
apres avant encore tres bien peut doit cela ceci notre votre orateur orateurs intervenant speaker video analyse direct sujet question this that
with from have what about will would should could there their they""".split())


def _tokens(*texts):
    out = set()
    for t in texts:
        out |= {w for w in re.findall(r"[a-z0-9]{4,}", _norm(t or "")) if w not in STOPWORDS and not w.isdigit()}
    return out


def _post_tokens(row):
    return _tokens(row["title"], row["topic"])


def interest_profile(conn, user):
    """Profil d'intérêts : mots-clés et auteurs pondérés par les interactions récentes, préférences déclarées, abonnements"""
    uid, since = user["id"], now() - 120 * 86400
    signals = conn.execute("""
        SELECT target_id, 2.0 FROM reactions WHERE user_id = :u AND target_type = 'post' AND created_at >= :s
        UNION ALL SELECT post_id, 3.0 FROM comments WHERE user_id = :u AND created_at >= :s
        UNION ALL SELECT post_id, 2.0 FROM verdicts WHERE user_id = :u AND created_at >= :s
        UNION ALL SELECT DISTINCT post_id, 1.5 FROM quiz_answers WHERE user_id = :u AND created_at >= :s
        UNION ALL SELECT post_id, 0.6 FROM post_views WHERE user_id = :u AND at >= :s
        UNION ALL SELECT id, 1.0 FROM posts WHERE user_id = :u AND created_at >= :s
        UNION ALL SELECT post_id, -5.0 FROM post_feedback WHERE user_id = :u AND value < 0""", {"u": uid, "s": since}).fetchall()
    per = {}
    for pid, w in signals:
        per[pid] = per.get(pid, 0) + w
    weights, authors, kinds = {}, {}, {}
    ids = list(per)[:800]
    if ids:
        for p in conn.execute(f"SELECT id, user_id, kind, title, topic FROM posts WHERE id IN ({_marks(ids)})", ids):
            w = per[p["id"]]
            for tok in _post_tokens(p):
                weights[tok] = weights.get(tok, 0) + w
            if p["user_id"] != uid:
                authors[p["user_id"]] = authors.get(p["user_id"], 0) + w
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + w
    prefs = user_prefs(user)
    for topic in prefs["topics"]:
        for tok in _tokens(topic):
            weights[tok] = weights.get(tok, 0) + 6
    for k in prefs["kinds"]:
        kinds[k] = kinds.get(k, 0) + 6
    return {"weights": weights, "authors": authors, "kinds": kinds,
            "follows": {r[0] for r in conn.execute("SELECT followee_id FROM follows WHERE follower_id = ?", (uid,))},
            "seen": {r[0] for r in conn.execute("SELECT post_id FROM post_views WHERE user_id = ?", (uid,))},
            "hidden": {r[0] for r in conn.execute("SELECT post_id FROM post_feedback WHERE user_id = ? AND value < 0", (uid,))}}


def rank_for_you(rows, profile, uid):
    """Score = qualité (engagement) × fraîcheur × affinité (intérêts, auteurs, abonnements, format) × nouveauté, puis diversité des auteurs"""
    t, seed = now(), f"{uid}:{today()}"
    total_kind = sum(max(0, v) for v in profile["kinds"].values()) if profile else 0
    scored = []
    for r in rows:
        if profile and r["id"] in profile["hidden"]:
            continue
        age_h = max(0, (t - r["created_at"]) / 3600)
        pop = math.log1p(r["likes"] + 2 * r["comments"] + 1.5 * r["votes"] + 0.05 * r["views"])
        score = (0.6 + 0.5 * pop) * (1 + age_h / 18) ** -0.9
        reasons = []
        if profile:
            matched = sorted(((profile["weights"].get(k, 0), k) for k in _post_tokens(r)), reverse=True)
            pos = sum(w for w, _ in matched if w > 0)
            neg = -sum(w for w, _ in matched if w < 0)
            interest = pos / (pos + 8)
            a = max(0, profile["authors"].get(r["user_id"], 0))
            aff = a / (a + 6)
            follows = r["user_id"] in profile["follows"]
            kind_aff = max(0, profile["kinds"].get(r["kind"], 0)) / total_kind if total_kind else 0.5
            score *= (1 + 1.8 * interest + 1.1 * aff + 0.9 * follows + 0.4 * kind_aff) / (1 + neg / 10)
            if r["id"] in profile["seen"]:
                score *= 0.45
            if r["user_id"] == uid:
                score *= 0.35
            if follows:
                reasons.append((0.9, f"Vous suivez {r['username']}"))
            if interest > 0.2 and matched and matched[0][0] > 0:
                reasons.append((1.8 * interest, f"Parce que vous aimez « {matched[0][1]} »"))
            if aff > 0.2:
                reasons.append((1.1 * aff, f"Vous échangez souvent avec {r['username']}"))
        if pop >= 2.3:
            reasons.append((0.4 + pop / 10, "Populaire en ce moment"))
        if age_h < 24:
            reasons.append((0.3, "Nouveau"))
        score *= 0.9 + 0.2 * random.Random(f"{seed}:{r['id']}").random()
        scored.append([score, r, max(reasons)[1] if reasons else "Suggestion pour vous"])
    scored.sort(key=lambda s: -s[0])
    out, per_author = [], {}
    while scored:  # diversité : chaque nouvelle publication d'un même auteur est pénalisée
        i = max(range(min(len(scored), 30)), key=lambda j: scored[j][0] * 0.7 ** per_author.get(scored[j][1]["user_id"], 0))
        item = scored.pop(i)
        per_author[item[1]["user_id"]] = per_author.get(item[1]["user_id"], 0) + 1
        out.append(item)
    return out


@route("POST", r"/api/community/posts/(\d+)/feedback")
def post_feedback(req, pid):
    user = req.need()
    row = _fetch_post(req, int(pid))
    value = _int(req.body.get("value"), -1, -1, 1)
    if value:
        req.conn.execute("INSERT OR REPLACE INTO post_feedback (post_id, user_id, value, created_at) VALUES (?,?,?,?)", (row["id"], user["id"], value, now()))
    else:
        req.conn.execute("DELETE FROM post_feedback WHERE post_id = ? AND user_id = ?", (row["id"], user["id"]))
    return {"ok": True}


@route("GET", "/api/me/interests")
def my_interests(req):
    user = req.need()
    p = interest_profile(req.conn, user)
    top = sorted(((w, k) for k, w in p["weights"].items() if w > 0), reverse=True)[:15]
    counts = {}
    for r in req.conn.execute("SELECT title, topic FROM posts WHERE hidden = 0 ORDER BY id DESC LIMIT 300"):
        for tok in _post_tokens(r):
            counts[tok] = counts.get(tok, 0) + 1
    suggested = [k for k, n in sorted(counts.items(), key=lambda x: -x[1]) if n > 1 and k not in p["weights"]][:12]
    return {"topics": [{"word": k, "score": round(w, 1)} for w, k in top], "prefs": user_prefs(user), "suggested": suggested,
            "hidden": len(p["hidden"]), "seen": len(p["seen"]), "follows": len(p["follows"])}


@route("DELETE", "/api/me/interests")
def reset_interests(req):
    user = req.need()
    req.conn.execute("DELETE FROM post_views WHERE user_id = ?", (user["id"],))
    req.conn.execute("DELETE FROM post_feedback WHERE user_id = ?", (user["id"],))
    log_activity(req.conn, user["id"], "prefs", "Signaux de recommandation réinitialisés", req.ip)
    return {"ok": True}


# ---------- Synchronisation légère (bandeau « nouveautés », directs en cours, notifications) ----------
@route("GET", "/api/community/pulse")
def pulse(req):
    since = _int(req.query.get("since"), now(), 0, now())
    data = {"at": now(), "lives": active_lives(req.conn, 12),
            "new_posts": req.conn.execute("SELECT COUNT(*) FROM posts WHERE created_at > ? AND hidden = 0 AND user_id != ?", (since, _uid(req))).fetchone()[0]}
    if req.user:
        data["unread"] = req.conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ? AND seen = 0", (req.user["id"],)).fetchone()[0]
        data["dm_unread"] = _dm_unread(req.conn, req.user["id"])
    return data
