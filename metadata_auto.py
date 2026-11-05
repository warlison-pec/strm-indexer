"""Conservative, bounded and persistent background enrichment of visible cards."""
import os
import sqlite3
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError

import catalog_enrich as enrich
import tmdb_meta as tmdb
import tvdb_meta as tvdb
import tvmaze_meta as tvmaze
import jellyfin_meta as jellyfin
import seerr_meta as seerr
from indexer import norm

DB = enrich.DATA / 'metadata_queue.sqlite'
LOCK = threading.RLock()
WAKE = threading.Event()
GAP = max(8., float(os.getenv('INDEXER_META_GAP_SECONDS', '12')))
COOLDOWN = 0.  # legacy alias for TMDB cooldown
TVDB_COOLDOWN = 0.
TVDB_GAP = max(8., float(os.getenv('INDEXER_TVDB_GAP_SECONDS', '15')))
TVDB_LAST = 0.
TVMAZE_COOLDOWN = 0.
JELLYFIN_COOLDOWN = 0.
SEERR_COOLDOWN = 0.
SEERR_LAST = 0.
SEERR_GAP = max(8., float(os.getenv('INDEXER_SEERR_GAP_SECONDS', '15')))



def setup():
    enrich.DATA.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=15) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS requests(
            content_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
            year INTEGER, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            updated_at REAL NOT NULL DEFAULT 0)''')
        # One-time local Jellyfin pass for queued entries even while public
        # providers have a cooldown or next_at is in the future.
        cols = {r[1] for r in db.execute('PRAGMA table_info(requests)')}
        if 'jellyfin_checked_at' not in cols:
            db.execute('ALTER TABLE requests ADD COLUMN jellyfin_checked_at REAL NOT NULL DEFAULT 0')
        db.execute('CREATE INDEX IF NOT EXISTS requests_pending ON requests(state,next_at)')
        db.execute("UPDATE requests SET state='queued',next_at=0 WHERE state='running'")


def enqueue(kind, items):
    if kind not in ('movie', 'episode'):
        return
    items = items[:12]
    known = enrich.for_cards(kind, [x['title'] for x in items])
    now = time.time()
    with LOCK, sqlite3.connect(DB, timeout=15) as db:
        for item in items:
            title = item['title']
            if title in known or len(norm(title)) < 5:
                continue
            try:
                key = enrich.canonical(kind, title)
            except ValueError:
                continue
            db.execute('''INSERT OR IGNORE INTO requests
                (content_key,kind,title,year,state,next_at,updated_at)
                VALUES(?,?,?,?, 'queued',?,?)''',
                (key,kind,title,item.get('year'),now,now))
    WAKE.set()


class ProvidersCoolingDown(Exception):
    """All configured providers are cooling down; do not count as a failed match."""
    def __init__(self, resume_at):
        self.resume_at = resume_at
        super().__init__('Provedores temporariamente indisponiveis')


def _exact_candidate(candidates, title, year, provider):
    query = norm(title)
    exact = {}
    for c in candidates:
        label = c.get('title') or ''
        aliases = [label]
        if provider in ('tmdb', 'jellyfin'):
            aliases.append(c.get('original_title') or '')
        if query not in {norm(a) for a in aliases if a}:
            continue
        if year and c.get('year') != int(year):
            continue
        exact[str(c['id'])] = c
    return next(iter(exact.values())) if len(exact) == 1 else None


import re as _re

# Rótulos de qualidade/idioma/versão que as listas costumam colar no nome.
_TAG_WORDS = (r'4k|uhd|fhd|full\s*hd|hd|sd|hdr10\+?|hdr|dolby\s*vision|dv|imax|3d|'
              r'2160p|1080p|720p|480p|x26[45]|h\.?26[45]|hevc|web-?dl|webrip|bluray|blu-ray|brrip|dvdrip|cam|ts|'
              r'dublado|dub|legendado|leg|l|dual(?:\s*[aá]udio)?|multi|nacional|'
              r'remasterizado|remastered|vers[aã]o\s+estendida|estendida|extended|'
              r"director'?s\s*cut|unrated|sem\s+cortes")
_BRACKET = _re.compile(r'\s*\[[^\]]{0,40}\]\s*')
_PAREN_TAG = _re.compile(r'\s*\((?:\s*(?:' + _TAG_WORDS + r')\s*[,/|+-]?\s*)+\)\s*', _re.I)
_TRAIL_TAG = _re.compile(r'(?:\s*[-–|:/]?\s*\b(?:' + _TAG_WORDS + r')\b\.?)+\s*$', _re.I)
_LEAD_NUM = _re.compile(r'^\s*(?:#|n[ºo°]\.?\s*)?\d{1,3}\s*[-–.:)|]\s+(?=\S)')
# Só ano entre parênteses: 'Blade Runner 2049' e '2012' são títulos reais.
_YEAR = _re.compile(r'\(\s*((?:19|20)\d{2})\s*\)\s*$')


def parse_metadata_title(title):
    """Return (clean_title, year) for provider lookups.

    Examples:
      '1 - Harry Potter E A Pedra Filosofal 4K' -> ('Harry Potter E A Pedra Filosofal', None)
      'Duna: Parte Dois (2024) [L]'             -> ('Duna: Parte Dois', 2024)
      '007 Sem Tempo para Morrer'               -> unchanged (no separator after the number)
    """
    raw = str(title or '').strip()
    t = _BRACKET.sub(' ', raw)
    year = None
    for _ in range(4):
        before = t
        t = _PAREN_TAG.sub(' ', t).strip()
        t = _TRAIL_TAG.sub('', t).strip(' -–|:/.')
        m = _YEAR.search(t)
        if m and len(t[:m.start()].strip()) >= 2:
            year = year or int(m.group(1))
            t = t[:m.start()].strip(' -–|:/.')
        if t == before:
            break
    t = _LEAD_NUM.sub('', t).strip()
    t = _re.sub(r'\s{2,}', ' ', t).strip(' -–|:/.')
    return (t if len(t) >= 2 else raw), year


def clean_metadata_title(title):
    """Compatibility wrapper: only the cleaned title."""
    return parse_metadata_title(title)[0]


def _identify(row):
    """Try local Jellyfin, TVmaze (series), TMDB and TVDB, preserving verified IDs.

    Read-only Jellyfin; TVmaze only for series. No guessing on ambiguous names.
    Failures of one provider do not prevent trying independent providers.
    """
    global COOLDOWN, TVDB_COOLDOWN, TVDB_LAST, TVMAZE_COOLDOWN, JELLYFIN_COOLDOWN, SEERR_COOLDOWN, SEERR_LAST
    original_title = row['title']
    title, parsed_year = parse_metadata_title(original_title)
    kind, year = row['kind'], row['year'] or parsed_year
    if original_title in enrich.for_cards(kind, [original_title]):
        return 'done', None
    blocked, errors = [], []
    jellyfin_saved = False

    # Prefer existing Jellyfin metadata; all requests are read-only.
    if jellyfin.configured():
        if time.time() < JELLYFIN_COOLDOWN:
            blocked.append(JELLYFIN_COOLDOWN)
        else:
            try:
                matches = jellyfin.search(title, kind).get('items', [])
                c = _exact_candidate(matches, title, year, 'jellyfin')
                if c:
                    enrich.save(kind, original_title, 'jellyfin', c['id'], c['title'], c.get('year'), c.get('overview'), None)
                    jellyfin_saved = True
                    if c.get('has_image'):
                        image = jellyfin.poster(c['id'])
                        if image and enrich.save_poster_bytes(kind, original_title, *image):
                            print(f'META_AUTO: Jellyfin cache; {title[:70]!r}', flush=True)
                            return 'done', None
                    # Jellyfin has no usable poster: try the public providers too.
                errors.append('Jellyfin sem correspondencia exata e unica')
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                JELLYFIN_COOLDOWN = time.time() + 600
                blocked.append(JELLYFIN_COOLDOWN)
                print(f'META_AUTO: Jellyfin indisponivel ({type(exc).__name__}); tenta outros provedores', flush=True)

    # Seerr: read-only search.  It provides movie/TV metadata and TMDB poster paths.
    # Use only exact title matches (and matching year when available).
    if seerr.configured():
        if time.time() < SEERR_COOLDOWN:
            blocked.append(SEERR_COOLDOWN)
        else:
            wait = max(0., SEERR_GAP - (time.monotonic() - SEERR_LAST)) if SEERR_LAST else 0.
            if wait:
                time.sleep(wait)
            SEERR_LAST = time.monotonic()
            try:
                matches = seerr.search(title, kind).get('items', [])
                c = _exact_candidate(matches, title, year, 'seerr')
                if c:
                    result = enrich.save(kind, original_title, 'seerr', c['id'],
                                         c['title'], c.get('year'), c.get('overview'), c.get('poster'))
                    print(f'META_AUTO: Seerr; {original_title[:70]!r}; capa={result["poster_saved"]}', flush=True)
                    return 'done', None
                errors.append('Seerr sem correspondencia exata e unica')
            except HTTPError as exc:
                SEERR_COOLDOWN = time.time() + (max(300., _delay(exc)) if exc.code == 429
                                                  else 600 if exc.code >= 500 else 1800)
                blocked.append(SEERR_COOLDOWN)
                print(f'META_AUTO: Seerr HTTP {exc.code}; tenta outros provedores', flush=True)
            except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
                SEERR_COOLDOWN = time.time() + 600
                blocked.append(SEERR_COOLDOWN)
                print(f'META_AUTO: Seerr indisponivel ({type(exc).__name__}); tenta outros provedores', flush=True)

    # TVmaze public API; /search/shows returns images and summaries in one call.
    if kind == 'episode':
        if time.time() < TVMAZE_COOLDOWN:
            blocked.append(TVMAZE_COOLDOWN)
        else:
            try:
                matches = tvmaze.search(title, kind).get('items', [])
                c = _exact_candidate(matches, title, year, 'tvmaze')
                if c:
                    enrich.save(kind, original_title, 'tvmaze', c['id'], c['title'], c.get('year'), c.get('overview'), c.get('poster'))
                    print(f'META_AUTO: TVmaze; {title[:70]!r}', flush=True)
                    return 'done', None
                errors.append('TVmaze sem correspondencia exata e unica')
            except HTTPError as exc:
                TVMAZE_COOLDOWN = time.time() + (max(30, _delay(exc)) if exc.code == 429 else 600)
                blocked.append(TVMAZE_COOLDOWN)
                print(f'META_AUTO: TVmaze HTTP {exc.code}; tenta outros provedores', flush=True)
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                TVMAZE_COOLDOWN = time.time() + 600
                blocked.append(TVMAZE_COOLDOWN)
                print(f'META_AUTO: TVmaze indisponivel ({type(exc).__name__}); tenta outros provedores', flush=True)

    if tmdb.configured():
        if time.time() < COOLDOWN:
            blocked.append(COOLDOWN)
        else:
            try:
                candidates = tmdb.search(title, 'movie' if kind == 'movie' else 'tv', year).get('items', [])
                c = _exact_candidate(candidates, title, year, 'tmdb')
                if c:
                    enrich.save(kind, original_title, 'tmdb', c['id'], c['title'], c.get('year'), c.get('overview'), c.get('poster'))
                    return 'done', None
                errors.append('TMDB sem correspondencia exata e unica')
            except HTTPError as exc:
                if exc.code == 429:
                    delay = max(_delay(exc), 300.)
                    COOLDOWN = time.time() + delay
                    blocked.append(COOLDOWN)
                    print(f'META_AUTO: TMDB HTTP 429; pausa {int(delay)}s; tentando outros provedores', flush=True)
                elif exc.code >= 500:
                    COOLDOWN = time.time() + 600
                    blocked.append(COOLDOWN)
                else:
                    errors.append(f'TMDB HTTP {exc.code}')
            except (URLError, TimeoutError, OSError) as exc:
                COOLDOWN = time.time() + 300
                blocked.append(COOLDOWN)

    if tvdb.configured():
        if time.time() < TVDB_COOLDOWN:
            blocked.append(TVDB_COOLDOWN)
        else:
            wait = max(0., TVDB_GAP - (time.monotonic() - TVDB_LAST)) if TVDB_LAST else 0.
            if wait:
                time.sleep(wait)
            TVDB_LAST = time.monotonic()
            try:
                candidates = tvdb.search(title, kind).get('items', [])
                c = _exact_candidate(candidates, title, year, 'tvdb')
                if c:
                    enrich.save(kind, original_title, 'tvdb', c['id'], c['title'], c.get('year'), c.get('overview'), c.get('poster'))
                    return 'done', None
                errors.append('TheTVDB sem correspondencia exata e unica')
            except HTTPError as exc:
                TVDB_COOLDOWN = time.time() + (max(_delay(exc), 300.) if exc.code == 429 else 600 if exc.code >= 500 else 3600)
                blocked.append(TVDB_COOLDOWN)
                print(f'META_AUTO: TheTVDB HTTP {exc.code}; pausa', flush=True)
            except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
                TVDB_COOLDOWN = time.time() + 600
                blocked.append(TVDB_COOLDOWN)
                print(f'META_AUTO: TheTVDB indisponivel ({type(exc).__name__}); pausa', flush=True)

    # Existing Jellyfin metadata is still useful even when no poster was returned.
    if jellyfin_saved:
        return 'done', None
    # Review only when all active providers have given definitive no-match results.
    if blocked:
        raise ProvidersCoolingDown(min(blocked))
    return 'review', (' / '.join(errors) or 'Sem provedores ativos') + '; selecao manual necessaria'


def _delay(err):
    raw = (err.headers.get('Retry-After') or '').strip()
    try:
        return max(30., min(7200., float(raw)))
    except (ValueError, TypeError):
        pass
    try:
        return max(30., min(7200., parsedate_to_datetime(raw).timestamp() - time.time()))
    except (ValueError, TypeError, OverflowError):
        return 300.


def loop(stop):
    """Process pending requests; log cooldown/retries and recover per-item errors."""
    global COOLDOWN
    print('META_AUTO: trabalhador iniciado', flush=True)
    last_pause_log = 0.0
    while not stop.is_set():
        now = time.time()
        row = None
        try:
            with LOCK, sqlite3.connect(DB, timeout=15) as db:
                db.row_factory = sqlite3.Row
                # Pending local-only check can run despite an external API pause.
                # After that first local pass, respect the original next_at.
                local_ready = jellyfin.configured() and now >= JELLYFIN_COOLDOWN
                row = db.execute('''SELECT * FROM requests
                    WHERE state='queued' AND (next_at<=? OR (? AND jellyfin_checked_at=0))
                    ORDER BY CASE WHEN jellyfin_checked_at=0 AND ? THEN 0 ELSE 1 END,
                             next_at, updated_at LIMIT 1''',
                    (now, int(local_ready), int(local_ready))).fetchone()
                if row:
                    db.execute("UPDATE requests SET state='running', updated_at=? WHERE content_key=?", (now, row['content_key']))
        except sqlite3.Error as exc:
            print(f'META_AUTO: falha temporaria ao ler fila: {type(exc).__name__}', flush=True)
            stop.wait(15)
            continue
        if row is None:
            WAKE.wait(10)
            WAKE.clear()
            continue
        attempts = int(row['attempts'])
        title = row['title']
        retry = 0
        try:
            state, error = _identify(row)
        except ProvidersCoolingDown as exc:
            state, error, retry = 'queued', 'Aguardando disponibilidade dos provedores', max(time.time()+30, exc.resume_at)
            if time.time()-last_pause_log > 60:
                print(f'META_AUTO: provedores em pausa; proxima tentativa em {int(retry-time.time())}s', flush=True)
                last_pause_log = time.time()
        except HTTPError as exc:
            if exc.code == 429:
                pause = max(_delay(exc), min(3600., 300. * (2 ** min(attempts, 4))))
                COOLDOWN = time.time() + pause
                state, error, retry = 'queued', 'TMDB HTTP 429', COOLDOWN
                print(f'META_AUTO: TMDB HTTP 429; pausa global {int(pause)}s; tentativa {attempts+1}', flush=True)
            elif exc.code >= 500:
                state, error, retry = 'queued', f'TMDB HTTP {exc.code}', time.time()+600
                print(f'META_AUTO: TMDB HTTP {exc.code}; reagendado em 600s', flush=True)
            else:
                state, error = 'review', f'TMDB HTTP {exc.code}'
                print(f'META_AUTO: HTTP {exc.code}; revisao manual necessaria', flush=True)
        except (URLError, TimeoutError, OSError) as exc:
            state, error, retry = 'queued', f'Conexao: {type(exc).__name__}', time.time()+600
            print(f'META_AUTO: conexao temporariamente indisponivel: {type(exc).__name__}', flush=True)
        except Exception as exc:
            state, error = 'review', type(exc).__name__
            print(f'META_AUTO: falha em item; {type(exc).__name__}; revisao necessaria', flush=True)
        try:
            with LOCK, sqlite3.connect(DB, timeout=15) as db:
                # Always mark the local pass so a future external cooldown
                # does not cause the same Jellyfin query in a hot loop.
                db.execute('''UPDATE requests SET state=?,last_error=?,next_at=?,attempts=attempts+1,
                    updated_at=?, jellyfin_checked_at=CASE WHEN ? THEN ? ELSE jellyfin_checked_at END
                    WHERE content_key=?''',
                    (state, error, retry, time.time(), int(jellyfin.configured()),
                     time.time(), row['content_key']))
            if state in ('done', 'review'):
                print(f'META_AUTO: {state}; {title[:70]!r}', flush=True)
        except sqlite3.Error as exc:
            print(f'META_AUTO: falha gravando resultado: {type(exc).__name__}', flush=True)
            stop.wait(20)
        stop.wait(GAP)


def stats():
    with sqlite3.connect(DB, timeout=10) as db:
        counts = dict(db.execute('SELECT state,COUNT(*) FROM requests GROUP BY state').fetchall())
    return {'counts': counts, 'cooldown_seconds': max(0, int(COOLDOWN-time.time())), 'tvdb_cooldown_seconds': max(0, int(TVDB_COOLDOWN-time.time())), 'tvmaze_cooldown_seconds': max(0, int(TVMAZE_COOLDOWN-time.time())), 'jellyfin_cooldown_seconds': max(0, int(JELLYFIN_COOLDOWN-time.time())), 'seerr_cooldown_seconds': max(0, int(SEERR_COOLDOWN-time.time()))}
