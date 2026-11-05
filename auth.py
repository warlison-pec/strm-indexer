"""Minimal local administrator account and signed, CSRF-protected sessions.

Setup: python3 auth.py set-admin (interactive; password is never part of argv).
Requires only the Python standard library. State survives Docker restarts.
"""
import argparse
import base64
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

STATE_DIR = Path(os.environ.get('INDEXER_STATE_DIR', '/app/data'))
# For interactive setup on the host without an env var, prefer local ./data.
if not STATE_DIR.exists() and Path('data').is_dir():
    STATE_DIR = Path('data')
DB = STATE_DIR / 'auth.sqlite'
KEY_FILE = STATE_DIR / '.session_secret'
TTL = 12 * 3600
PBKDF2_ITERATIONS = 600_000
_LOCK = threading.RLock()
_ATTEMPTS = {}


def setup():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=15) as db:
        db.execute('CREATE TABLE IF NOT EXISTS admins (username TEXT PRIMARY KEY, salt TEXT NOT NULL, digest TEXT NOT NULL, iterations INTEGER NOT NULL)')
    if DB.exists():
        try: os.chmod(DB, 0o600)
        except OSError: pass
    if not KEY_FILE.exists():
        try:
            fd = os.open(KEY_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass


def has_admin():
    with sqlite3.connect(DB, timeout=10) as db:
        return db.execute('SELECT EXISTS(SELECT 1 FROM admins)').fetchone()[0] == 1


def set_admin(username, password):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', username):
        raise ValueError('Usuário deve conter 3 a 40 caracteres: letras, números, ponto, hífen ou underline')
    if len(password) < 12 or len(password) > 256:
        raise ValueError('Senha deve conter entre 12 e 256 caracteres')
    setup()
    salt = secrets.token_bytes(24)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS)
    with sqlite3.connect(DB, timeout=15) as db:
        db.execute('INSERT INTO admins(username,salt,digest,iterations) VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET salt=excluded.salt,digest=excluded.digest,iterations=excluded.iterations',
                   (username, salt.hex(), digest.hex(), PBKDF2_ITERATIONS))


def verify(username, password):
    if not username or not isinstance(password, str) or len(password) > 256:
        return False
    with sqlite3.connect(DB, timeout=10) as db:
        row = db.execute('SELECT salt,digest,iterations FROM admins WHERE username=?', (username,)).fetchone()
    if not row:
        # Equalize approximate workload for unknown usernames.
        hashlib.pbkdf2_hmac('sha256', b'not-a-real-password', b'\0'*24, PBKDF2_ITERATIONS)
        return False
    expected = bytes.fromhex(row[1]); salt = bytes.fromhex(row[0])
    calculated = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, row[2])
    return hmac.compare_digest(calculated, expected)


def _key():
    return bytes.fromhex(KEY_FILE.read_text().strip())


def _b64(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def create_session(username):
    payload = {'u': username, 'exp': int(time.time()) + TTL, 'sid': secrets.token_hex(16)}
    packed = _b64(json.dumps(payload, separators=(',', ':')).encode())
    signature = _b64(hmac.new(_key(), packed.encode(), hashlib.sha256).digest())
    return packed + '.' + signature


def decode_session(token):
    try:
        if not token or len(token) > 600: return None
        packed, signature = token.split('.', 1)
        expected = _b64(hmac.new(_key(), packed.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected): return None
        data = json.loads(base64.urlsafe_b64decode(packed + '=' * (-len(packed) % 4)))
        if data.get('exp', 0) < time.time() or not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', data.get('u','')):
            return None
        with sqlite3.connect(DB, timeout=10) as db:
            if not db.execute('SELECT 1 FROM admins WHERE username=?', (data['u'],)).fetchone(): return None
        return data
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def csrf_for(token):
    return _b64(hmac.new(_key(), ('csrf:' + token).encode(), hashlib.sha256).digest())


def allow_login(ip):
    with _LOCK:
        now = time.monotonic()
        attempts = [t for t in _ATTEMPTS.get(ip, []) if now-t < 300]
        _ATTEMPTS[ip] = attempts
        return len(attempts) < 5


def note_login_failure(ip):
    with _LOCK:
        _ATTEMPTS.setdefault(ip, []).append(time.monotonic())


def clear_login_failures(ip):
    with _LOCK:
        _ATTEMPTS.pop(ip, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['set-admin'])
    args = parser.parse_args()
    username = input('Nome de usuário: ').strip()
    password = getpass.getpass('Senha (mínimo 12 caracteres): ')
    confirmation = getpass.getpass('Confirme a senha: ')
    if password != confirmation:
        raise SystemExit('As senhas não coincidem.')
    try: set_admin(username, password)
    except ValueError as exc: raise SystemExit(str(exc))
    print('Conta configurada. Nenhuma senha ou token foi exibido.')


if __name__ == '__main__': main()
