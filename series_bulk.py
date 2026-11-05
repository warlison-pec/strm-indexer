"""Sequential, restart-resumable episodic import and daily recovery checks.

Only administrator-triggered jobs. Never invents episode matches or bypasses
source access control. Pending episodes remain unexported until validated.
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import media_control as media
from indexer import DB, norm

PENDING_RETRY = max(3600, int(__import__('os').environ.get('INDEXER_PENDING_RETRY', '86400')))
PROCESS_LOCK = threading.Lock()
JOB_LOCK = threading.Lock()
WAKE = threading.Event()


def db_connect():
    c = media.connect()
    c.execute('PRAGMA busy_timeout=15000')
    return c


def setup():
    with db_connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS series_jobs(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, normalized TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '', selected_season INTEGER,
            state TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
            already INTEGER NOT NULL DEFAULT 0, pending INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL, updated_at REAL NOT NULL,
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS series_pending(
            id INTEGER PRIMARY KEY AUTOINCREMENT, normalized TEXT NOT NULL,
            title TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
            season INTEGER NOT NULL, episode INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', last_checked REAL,
            next_check REAL NOT NULL DEFAULT 0,
            added_key TEXT NOT NULL DEFAULT '',
            UNIQUE(normalized,source,season,episode)
        );
        CREATE INDEX IF NOT EXISTS idx_series_pending_due
          ON series_pending(state,next_check);
        ''')
        # Jobs interrupted by container restart are continued automatically.
        c.execute("UPDATE series_jobs SET state='queued',completed=0,added=0,already=0,pending=0,updated_at=? WHERE state='running'", (time.time(),))


def _candidates(title, source='', season=None):
    title = title.strip()
    normalized = norm(title)
    if not normalized:
        raise ValueError('Título não identificado')
    where = "kind='episode' AND normalized=? AND season IS NOT NULL AND episode IS NOT NULL"
    params = [normalized]
    if source:
        where += ' AND source=?'
        params.append(source)
    if season is not None:
        where += ' AND season=?'
        params.append(season)
    # Categorias bloqueadas (ex.: adultos) nunca viram episódios no Jellyfin,
    # a menos que o administrador tenha liberado a exibição delas no painel.
    import content_filter
    block_sql, block_params = content_filter.where(for_panel=True)
    where += ' AND ' + block_sql
    params.extend(block_params)
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=20) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(row) for row in c.execute(
            'SELECT id,title,source,season,episode FROM entries WHERE '+where+
            ' ORDER BY season,episode,source,id LIMIT 10001', params)]
    grouped = {}
    for r in rows:
        grouped.setdefault((r['season'], r['episode']), []).append(r['id'])
    if len(grouped) > 1000 or len(rows) > 10000:
        raise ValueError('Série muito extensa; selecione uma temporada')
    return normalized, grouped


def start(title, source='', season=None):
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 180:
        raise ValueError('Título inválido')
    if not isinstance(source, str) or len(source) > 200:
        raise ValueError('Origem inválida')
    if season not in (None, ''):
        season = int(season)
        if not 0 <= season <= 999:
            raise ValueError('Temporada inválida')
    else:
        season = None
    normalized, grouped = _candidates(title, source, season)
    if not grouped:
        raise ValueError('Nenhum episódio numerado encontrado')
    with JOB_LOCK, db_connect() as c:
        existing = c.execute("SELECT id FROM series_jobs WHERE state IN ('running','queued') AND normalized=? AND source=? AND selected_season IS ? LIMIT 1", (normalized,source,season)).fetchone()
        if existing:
            return {'job_id': existing['id'], 'already_running': True, 'episodes': len(grouped)}
        job_id = uuid.uuid4().hex
        now=time.time()
        c.execute('INSERT INTO series_jobs(id,title,normalized,source,selected_season,state,total,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                  (job_id,title.strip(),normalized,source,season,'queued',len(grouped),now,now))
    WAKE.set()
    return {'job_id':job_id,'episodes':len(grouped),'already_running':False}


def _mark_pending(title, normalized, source, season, episode, success, key='', reason=''):
    with db_connect() as c:
        old = c.execute('SELECT attempts FROM series_pending WHERE normalized=? AND source=? AND season=? AND episode=?', (normalized,source,season,episode)).fetchone()
        attempts = 0 if success else ((old['attempts'] if old else 0)+1)
        was_pending = bool(old and c.execute("SELECT state FROM series_pending WHERE normalized=? AND source=? AND season=? AND episode=?", (normalized,source,season,episode)).fetchone()['state']=='pending')
        c.execute('''INSERT INTO series_pending(normalized,title,source,season,episode,state,attempts,last_error,last_checked,next_check,added_key)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(normalized,source,season,episode) DO UPDATE SET
          title=excluded.title,state=excluded.state,attempts=excluded.attempts,last_error=excluded.last_error,
          last_checked=excluded.last_checked,next_check=excluded.next_check,added_key=excluded.added_key''',
          (normalized,title,source,season,episode,'added' if success else 'pending',attempts,
           '' if success else reason[:180],time.time(),time.time()+PENDING_RETRY,key))
        if success and was_pending:
            c.execute('INSERT INTO alerts (key,title,event,message,created_at) VALUES (?,?,?,?,?)',
                      (key,title,'restored',f'Episódio S{season:02}E{episode:02} recuperado e adicionado',media.utc_now()))


def _resolve_one(title, normalized, source, season, episode, candidates=None):
    if candidates is None:
        try:
            _, grouped = _candidates(title, source, season)
            candidates = grouped.get((season, episode), [])
        except Exception as exc:
            _mark_pending(title,normalized,source,season,episode,False,reason=str(exc))
            return 'pending' 
    if not candidates:
        _mark_pending(title,normalized,source,season,episode,False,reason='Episódio ausente nas listas atuais')
        return 'pending'
    problems=[]
    for entry_id in candidates:
        try:
            # add_entry validates the selected source, deduplicates and writes
            # only into the Indexer's configured managed export directory.
            result = media.add_entry(entry_id)
            _mark_pending(title,normalized,source,season,episode,True,key=result['key'])
            return 'already' if result.get('already_added') else 'added'
        except (ValueError, OSError, sqlite3.Error) as exc:
            problems.append(str(exc)[:90])
    _mark_pending(title,normalized,source,season,episode,False,reason='; '.join(problems[:2]) or 'Sem fonte reproduzível')
    return 'pending'


def _run_job(job):
    job_id=job['id']
    with db_connect() as c:
        c.execute('UPDATE series_jobs SET state=?,updated_at=? WHERE id=?', ('running',time.time(),job_id))
    try:
        _, grouped=_candidates(job['title'],job['source'],job['selected_season'])
        with db_connect() as c:
            c.execute('UPDATE series_jobs SET total=?,updated_at=? WHERE id=?', (len(grouped),time.time(),job_id))
        for (season,episode) in sorted(grouped):
            # On restart, successfully imported entries are detected by add_entry.
            status=_resolve_one(job['title'],job['normalized'],job['source'],season,episode,grouped[(season,episode)])
            with db_connect() as c:
                c.execute('''UPDATE series_jobs SET completed=completed+1,
                  added=added+?,already=already+?,pending=pending+?,updated_at=? WHERE id=?''',
                  (int(status=='added'),int(status=='already'),int(status=='pending'),time.time(),job_id))
        with db_connect() as c:
            c.execute('UPDATE series_jobs SET state=?,updated_at=? WHERE id=?',('finished',time.time(),job_id))
    except Exception as exc:
        with db_connect() as c:
            c.execute('UPDATE series_jobs SET state=?,error=?,updated_at=? WHERE id=?',('failed',type(exc).__name__,time.time(),job_id))


def get_job(job_id):
    if not isinstance(job_id,str) or len(job_id)>80:
        raise ValueError('ID inválido')
    with db_connect() as c:
        job=c.execute('SELECT * FROM series_jobs WHERE id=?',(job_id,)).fetchone()
        if not job: raise ValueError('Processamento não encontrado')
        failures=c.execute('''SELECT season,episode,last_error,attempts FROM series_pending
          WHERE normalized=? AND source=? AND state='pending' AND (? IS NULL OR season=?)
          ORDER BY season,episode LIMIT 100''',
          (job['normalized'],job['source'],job['selected_season'],job['selected_season'])).fetchall()
    return {'job':dict(job),'unavailable':[dict(x) for x in failures]}


def summary(title,source=''):
    if not isinstance(title,str) or not 2<=len(title)<=180: raise ValueError('Título inválido')
    if not isinstance(source,str) or len(source)>200: raise ValueError('Origem inválida')
    with db_connect() as c:
        items=c.execute('''SELECT season,episode,state,attempts,last_error,next_check FROM series_pending
          WHERE normalized=? AND source=? ORDER BY season,episode LIMIT 1000''',(norm(title),source)).fetchall()
    return {'items':[dict(x) for x in items]}


def _retry_due():
    now=time.time()
    with db_connect() as c:
        due=[dict(r) for r in c.execute('''SELECT * FROM series_pending WHERE state='pending' AND next_check<=?
          ORDER BY next_check LIMIT 10''',(now,))]
    for r in due:
        _resolve_one(r['title'],r['normalized'],r['source'],r['season'],r['episode'])


def loop(stop):
    # One worker across series batch + pending retries; avoids chromium and TMDB floods.
    while not stop.is_set():
        with db_connect() as c:
            job=c.execute("SELECT * FROM series_jobs WHERE state='queued' ORDER BY started_at LIMIT 1").fetchone()
        if job:
            with PROCESS_LOCK:
                _run_job(job)
        else:
            with PROCESS_LOCK:
                try: _retry_due()
                except Exception as exc: print('SERIES_PENDING:',type(exc).__name__,flush=True)
            WAKE.wait(30)
            WAKE.clear()
