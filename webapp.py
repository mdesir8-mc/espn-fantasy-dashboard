#!/usr/bin/env python3
"""Multi-user web app: anyone can create an account, paste their ESPN team links and get their own dashboard.

    python3 webapp.py                              # http://localhost:8765, data in ./data
    PORT=8080 DATA_DIR=/data python3 webapp.py     # hosted (Railway sets PORT)

Accounts are a username and password (scrypt hashes in SQLite). ESPN cookies for private leagues never reach the
database: the browser keeps them and sends them with each dashboard request, and the server only holds them in
memory long enough to call ESPN. Standard library only.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fantasy_tracker as ft
import metrics

HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or HERE / "data")
DB_PATH = DATA_DIR / "app.db"
ft.CACHE_DIR = Path(os.environ.get("CACHE_DIR") or DATA_DIR / "cache")
INDEX = HERE / "index.html"

HOSTED = "PORT" in os.environ
MAX_USERS = int(os.environ.get("MAX_USERS") or 200)
MAX_LEAGUES = 12
SESSION_SECONDS = 30 * 86400
MAX_BODY = 64 * 1024
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
OTHER_SPORTS = ("basketball", "baseball", "hockey", "soccer", "golf")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  salt BLOB NOT NULL, pw_hash BLOB NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS leagues (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  league_id INTEGER NOT NULL, team_id INTEGER NOT NULL, year INTEGER NOT NULL,
  name TEXT, team_name TEXT, private INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
  UNIQUE (user_id, league_id, year));
"""


# ---------------------------------------------------------------- storage

@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)


def hash_pw(pw, salt):
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


_DUMMY_SALT = secrets.token_bytes(16)


def check_login(username, pw):
    with db() as c:
        u = c.execute("SELECT id, salt, pw_hash FROM users WHERE username = ?", (username,)).fetchone()
    if not u:
        hash_pw(pw, _DUMMY_SALT)  # same cost either way, so timing doesn't reveal which usernames exist
        return None
    return u["id"] if hmac.compare_digest(hash_pw(pw, u["salt"]), u["pw_hash"]) else None


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        c.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_th(token), user_id, time.time() + SESSION_SECONDS))
    return token


def _th(token):
    return hashlib.sha256(token.encode()).hexdigest()


def league_rows(user_id):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, league_id, team_id, year, name, team_name, private FROM leagues WHERE user_id = ? ORDER BY id",
            (user_id,))]


# ---------------------------------------------------------------- rate limits

class Limiter:
    """At most n hits per key in a sliding window of `window` seconds."""

    def __init__(self, n, window):
        self.n, self.window, self.hits, self.lock = n, window, {}, threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.lock:
            if len(self.hits) > 10000:
                self.hits = {k: q for k, q in self.hits.items() if q and q[-1] > now - self.window}
            q = self.hits.setdefault(key, deque())
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


AUTH_LIMIT = Limiter(10, 600)      # sign-in and sign-up attempts per IP
SIGNUP_LIMIT = Limiter(3, 3600)    # new accounts per IP
LOOKUP_LIMIT = Limiter(40, 3600)   # league lookups (each one calls ESPN) per user
REFRESH_LIMIT = Limiter(1, 10)     # forced dashboard refreshes per user


# ---------------------------------------------------------------- ESPN links

def default_season():
    t = time.gmtime()
    return t.tm_year if t.tm_mon >= 3 else t.tm_year - 1   # the NFL season runs September to early January


def parse_espn_link(text):
    """(league_id, team_id or None, season or None) from an ESPN fantasy football link or a bare league id."""
    text = (text or "").strip()
    if text.isdigit():
        return int(text), None, None
    u = urlparse(text if "://" in text else "https://" + text)
    host = (u.hostname or "").lower()
    if host != "espn.com" and not host.endswith(".espn.com"):
        raise ValueError("That isn't an ESPN link. Open your team on fantasy.espn.com and copy the address.")
    if any(s in u.path.lower() for s in OTHER_SPORTS):
        raise ValueError("Only ESPN fantasy football is supported.")
    q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
    lid, tid, yr = q.get("leagueid", ""), q.get("teamid", ""), q.get("seasonid", "")
    if not lid.isdigit():
        raise ValueError("That link has no leagueId. Open your team page on ESPN and copy its address.")
    return int(lid), int(tid) if tid.isdigit() else None, int(yr) if yr.isdigit() else None


def espn_cfg(row, cookies):
    c = (cookies or {}).get(str(row["league_id"])) or {}
    cfg = {"name": row["name"] or f"League {row['league_id']}", "sport": "football", "league_id": row["league_id"],
           "team_id": row["team_id"], "year": row["year"]}
    s2, swid = str(c.get("espn_s2") or "").strip(), str(c.get("swid") or "").strip()
    if s2 and swid:
        cfg.update(espn_s2=s2, swid=swid if swid.startswith("{") else "{" + swid.strip("{}") + "}")
    return cfg


def lookup_league(league_id, year, cookies):
    """League name and teams, using a light ESPN view. Raises LookupError('private') for private leagues."""
    url = ft.LEAGUE.format(year=year, id=league_id) + "?view=mTeam&view=mSettings"
    try:
        raw = ft.get_json(url, ft.cookie_header(cookies))
    except RuntimeError as e:
        if "private" in str(e):
            raise LookupError("private")
        if "not found" in str(e).lower():
            raise LookupError(f"ESPN has no football league {league_id} for the {year} season.")
        raise LookupError(f"Couldn't reach ESPN ({e}).")
    teams = [{"id": t["id"], "name": metrics.team_name(t), "abbrev": t.get("abbrev")} for t in raw.get("teams") or []]
    return (raw.get("settings") or {}).get("name") or f"League {league_id}", sorted(teams, key=lambda t: t["name"].lower())


# ---------------------------------------------------------------- dashboard data

_pool = ThreadPoolExecutor(max_workers=8)
_data_cache, _data_lock = {}, threading.Lock()


def dashboard(user_id, cookies, force):
    rows = league_rows(user_id)
    key = (user_id, hashlib.sha256(json.dumps([cookies, rows], sort_keys=True).encode()).hexdigest())
    with _data_lock:
        hit = _data_cache.get(key)
    if hit and not force and time.time() - hit[0] < 20:
        return hit[1]

    def one(row):
        cfg = espn_cfg(row, cookies)
        try:
            cfg2, bundle = ft.gather(cfg)
            out = ft.build(cfg2, bundle)
        except Exception as e:
            out = {"label": cfg["name"], "error": str(e)}
        if "error" in out and "private" in out["error"]:
            out["error"] = ("This league is private and this browser doesn't have its ESPN cookies. "
                            "Add them under Leagues, or remove and re-add the league.")
        out["rowId"] = row["id"]
        return out

    data = {"fetchedAt": time.time(), "leagues": list(_pool.map(one, rows))}
    with _data_lock:
        if len(_data_cache) > 500:
            _data_cache.clear()
        _data_cache[key] = (time.time(), data)
    return data


# ---------------------------------------------------------------- HTTP

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
}


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


class Handler(BaseHTTPRequestHandler):
    server_version = "fantasy"
    sys_version = ""

    def log_message(self, *a):  # request lines can carry nothing sensitive, but keep logs quiet anyway
        pass

    # ---- plumbing
    def _send(self, code, body, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, headers=None):
        self._send(code, json.dumps(obj).encode(), "application/json", headers)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "Request too large.")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "Bad JSON.")

    def _ip(self):
        if HOSTED:
            real = self.headers.get("X-Real-IP")
            if real:
                return real.strip()
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[-1].strip()
        return self.client_address[0]

    def _user(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        tok = c.get("sid")
        if not tok:
            return None
        with db() as conn:
            r = conn.execute("SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
                             "WHERE s.token_hash = ? AND s.expires > ?", (_th(tok.value), time.time())).fetchone()
        return dict(r) if r else None

    def _session_cookie(self, token, max_age=SESSION_SECONDS):
        return {"Set-Cookie": f"sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
                              + ("; Secure" if HOSTED else "")}

    def _require_user(self):
        u = self._user()
        if not u:
            raise ApiError(401, "Sign in first.")
        return u

    # ---- routing
    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        path = urlparse(self.path).path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if not path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            # Every state-changing call must carry a custom header, which cross-site forms can't send.
            if method != "GET" and self.headers.get("X-Requested-With") != "fetch":
                raise ApiError(403, "Missing request header.")
            fn = ROUTES.get((method, path))
            if fn is None and method == "DELETE" and re.fullmatch(r"/api/leagues/\d+", path):
                fn = Handler.delete_league
            if fn is None:
                raise ApiError(404, "No such endpoint.")
            fn(self)
        except ApiError as e:
            self._json(e.status, {"error": e.message, **e.extra})
        except Exception as e:  # never leak a traceback to the page
            print(f"error on {method} {path}: {type(e).__name__}: {e}", file=sys.stderr)
            self._json(500, {"error": "Something went wrong on the server."})

    # ---- account
    def me(self):
        u = self._require_user()
        self._json(200, {"username": u["username"], "leagues": league_rows(u["id"]), "maxLeagues": MAX_LEAGUES})

    def _credentials(self):
        b = self._body()
        username, pw = str(b.get("username") or "").strip(), str(b.get("password") or "")
        if not AUTH_LIMIT.allow(self._ip()):
            raise ApiError(429, "Too many attempts. Wait a few minutes and try again.")
        return username, pw

    def signup(self):
        username, pw = self._credentials()
        if not USERNAME_RE.match(username):
            raise ApiError(400, "Usernames are 3 to 32 letters, numbers, dots, dashes or underscores.")
        if len(pw) < 8 or len(pw) > 200:
            raise ApiError(400, "Use a password of at least 8 characters.")
        with db() as c:
            if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= MAX_USERS:
                raise ApiError(403, "This dashboard is full right now. It's a small hobby deployment; check back later.")
            if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise ApiError(409, "That username is taken.")
        if not SIGNUP_LIMIT.allow(self._ip()):
            raise ApiError(429, "Too many new accounts from this network. Try again later.")
        salt = secrets.token_bytes(16)
        try:
            with db() as c:
                uid = c.execute("INSERT INTO users (username, salt, pw_hash, created) VALUES (?, ?, ?, ?)",
                                (username, salt, hash_pw(pw, salt), time.time())).lastrowid
        except sqlite3.IntegrityError:
            raise ApiError(409, "That username is taken.")
        self._json(200, {"username": username, "leagues": [], "maxLeagues": MAX_LEAGUES},
                   self._session_cookie(new_session(uid)))

    def login(self):
        username, pw = self._credentials()
        uid = check_login(username, pw)
        if not uid:
            raise ApiError(401, "Wrong username or password.")
        with db() as c:
            name = c.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()[0]
        self._json(200, {"username": name, "leagues": league_rows(uid), "maxLeagues": MAX_LEAGUES},
                   self._session_cookie(new_session(uid)))

    def logout(self):
        tok = SimpleCookie(self.headers.get("Cookie") or "").get("sid")
        if tok:
            with db() as c:
                c.execute("DELETE FROM sessions WHERE token_hash = ?", (_th(tok.value),))
        self._json(200, {"ok": True}, self._session_cookie("", 0))

    def delete_account(self):
        u = self._require_user()
        b = self._body()
        if not AUTH_LIMIT.allow(self._ip()):
            raise ApiError(429, "Too many attempts. Wait a few minutes and try again.")
        if check_login(u["username"], str(b.get("password") or "")) != u["id"]:
            raise ApiError(401, "Wrong password.")
        with db() as c:
            c.execute("DELETE FROM users WHERE id = ?", (u["id"],))
        self._json(200, {"ok": True}, self._session_cookie("", 0))

    # ---- leagues
    def add_league(self):
        u = self._require_user()
        b = self._body()
        try:
            league_id, team_id, year = parse_espn_link(b.get("link"))
        except ValueError as e:
            raise ApiError(400, str(e))
        if b.get("teamId") is not None:
            try:
                team_id = int(b["teamId"])
            except (TypeError, ValueError):
                raise ApiError(400, "Bad team id.")
        year = year or default_season()
        rows = league_rows(u["id"])
        if any(r["league_id"] == league_id and r["year"] == year for r in rows):
            raise ApiError(409, "That league is already on your dashboard.")
        if len(rows) >= MAX_LEAGUES:
            raise ApiError(400, f"You can follow up to {MAX_LEAGUES} leagues.")
        if not LOOKUP_LIMIT.allow(u["id"]):
            raise ApiError(429, "Too many lookups. Try again in a while.")
        cookies = espn_cfg({"league_id": league_id, "team_id": 0, "year": year, "name": ""},
                           {str(league_id): b.get("cookies") or {}})
        try:
            name, teams = lookup_league(league_id, year, cookies)
        except LookupError as e:
            if str(e) == "private":
                had = bool(cookies.get("espn_s2"))
                raise ApiError(403, "Those ESPN cookies didn't work for this league. They may have expired."
                               if had else "This league is private. Add your ESPN cookies to look it up.",
                               code="private")
            raise ApiError(400, str(e))
        team = next((t for t in teams if t["id"] == team_id), None)
        if team is None:
            # no teamId in the link (or a wrong one): let the person pick their team
            return self._json(200, {"needTeam": True, "leagueId": league_id, "year": year, "league": name,
                                    "teams": teams})
        with db() as c:
            c.execute("INSERT INTO leagues (user_id, league_id, team_id, year, name, team_name, private, created) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (u["id"], league_id, team_id, year, name, team["name"], int(bool(cookies.get("espn_s2"))),
                       time.time()))
        self._json(200, {"added": {"leagueId": league_id, "league": name, "team": team["name"], "year": year},
                         "leagues": league_rows(u["id"])})

    def delete_league(self):
        u = self._require_user()
        rid = int(urlparse(self.path).path.rsplit("/", 1)[1])
        with db() as c:
            c.execute("DELETE FROM leagues WHERE id = ? AND user_id = ?", (rid, u["id"]))
        self._json(200, {"leagues": league_rows(u["id"])})

    def data(self):
        u = self._require_user()
        b = self._body()
        cookies = b.get("cookies") if isinstance(b.get("cookies"), dict) else {}
        force = "refresh" in (urlparse(self.path).query or "") and REFRESH_LIMIT.allow(u["id"])
        self._json(200, dashboard(u["id"], cookies, force))


ROUTES = {
    ("GET", "/api/me"): Handler.me,
    ("POST", "/api/signup"): Handler.signup,
    ("POST", "/api/login"): Handler.login,
    ("POST", "/api/logout"): Handler.logout,
    ("POST", "/api/account/delete"): Handler.delete_account,
    ("POST", "/api/leagues"): Handler.add_league,
    ("POST", "/api/data"): Handler.data,
}


def main():
    init_db()
    port = int(os.environ.get("PORT") or 8765)
    host = "0.0.0.0" if HOSTED else "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"Fantasy dashboard on http://localhost:{port}  (data in {DATA_DIR})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
