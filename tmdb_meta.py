"""Opt-in TMDB match and local Jellyfin NFO generation. No automatic guessing."""
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import media_control as media

TMDB_TOKEN = os.environ.get('INDEXER_TMDB_TOKEN', '').strip()
TMDB_API_KEY = os.environ.get('INDEXER_TMDB_API_KEY', '').strip()


def configured():
    return bool(TMDB_TOKEN or TMDB_API_KEY)


def _fetch(path, params=None):
    if not configured():
        raise ValueError('Configure INDEXER_TMDB_TOKEN ou INDEXER_TMDB_API_KEY')
    params = dict(params or {})
    if TMDB_API_KEY or (TMDB_TOKEN and len(TMDB_TOKEN) == 32):
        params['api_key'] = TMDB_API_KEY or TMDB_TOKEN
    url = 'https://api.themoviedb.org/3/' + path + ('?' + urlencode(params) if params else '')
    headers = {'Accept': 'application/json', 'User-Agent': 'STRMIndexer/0.5'}
    if TMDB_TOKEN and len(TMDB_TOKEN) != 32:
        headers['Authorization'] = 'Bearer ' + TMDB_TOKEN
    with urlopen(Request(url, headers=headers), timeout=12) as response:
        if response.status != 200:
            raise ValueError('TMDB HTTP inesperado')
        data = response.read(262145)
        if len(data) > 262144:
            raise ValueError('Resposta TMDB muito grande')
    return json.loads(data)


def search(title, kind='movie', year=None):
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 150:
        raise ValueError('Título inválido')
    if kind not in ('movie', 'tv'):
        raise ValueError('Tipo inválido')
    args = {'query': title.strip(), 'language': 'pt-BR', 'include_adult': 'false'}
    if year is not None:
        year = int(year)
        if not 1900 <= year <= 2100:
            raise ValueError('Ano inválido')
        args['year' if kind == 'movie' else 'first_air_date_year'] = str(year)
    data = _fetch('search/' + kind, args)
    items = []
    for x in data.get('results', [])[:12]:
        date = x.get('release_date' if kind == 'movie' else 'first_air_date', '') or ''
        items.append({'id': x['id'], 'title': x.get('title' if kind == 'movie' else 'name'),
                      'original_title': x.get('original_title' if kind == 'movie' else 'original_name'),
                      'year': int(date[:4]) if re.fullmatch(r'\d{4}', date[:4]) else None,
                      'overview': (x.get('overview') or '')[:450],
                      'rating': x.get('vote_average'), 'votes': x.get('vote_count'),
                      'poster': ('https://image.tmdb.org/t/p/w500' + x['poster_path']) if x.get('poster_path') else None})
    return {'items': items, 'configured': True}


def _text(node, name, value):
    if value is not None and value != '':
        ET.SubElement(node, name).text = str(value)


def _atomic_xml(target, xmlroot):
    if target.exists():
        # Do not silently overwrite metadata edited by Jellyfin / the user.
        existing = target.read_bytes()
        if b'<uniqueid type="tmdb"' not in existing and b'<tmdbid>' not in existing:
            raise ValueError('NFO já existente sem identificador TMDB; não foi sobrescrito')
    temp = target.with_name(target.name + '.' + secrets.token_hex(4) + '.tmp')
    try:
        data = ET.tostring(xmlroot, encoding='utf-8', xml_declaration=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def apply(key, tmdb_id):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key):
        raise ValueError('Chave gerenciada inválida')
    mid = int(tmdb_id)
    if mid < 1 or mid > 999999999:
        raise ValueError('ID TMDB inválido')
    with media.LOCK:
        with media.connect() as c:
            row = c.execute('SELECT * FROM managed WHERE key=?', (key,)).fetchone()
        if not row:
            raise ValueError('STRM não gerenciado')
        if row['kind'] != 'movie':
            raise ValueError('Esta versão gera NFO para filmes; séries virão depois')
        details = _fetch('movie/' + str(mid), {'language': 'pt-BR'})
        if details.get('id') != mid:
            raise ValueError('Identificador TMDB divergente')
        title = details.get('title') or details.get('original_title')
        date = details.get('release_date') or ''
        year = int(date[:4]) if re.fullmatch(r'\d{4}', date[:4]) else None
        if not title:
            raise ValueError('TMDB não retornou título')
        destination = media.safe_dest(row['relative_path'])
        if row['status'] == 'quarantined':
            destination = (media.DATA / 'quarantine' / (key + '.strm')).resolve()
        nfo = destination.with_suffix('.nfo')
        root = ET.Element('movie')
        _text(root, 'title', title)
        _text(root, 'originaltitle', details.get('original_title'))
        _text(root, 'sorttitle', title)
        _text(root, 'year', year)
        _text(root, 'premiered', date)
        _text(root, 'plot', details.get('overview'))
        _text(root, 'runtime', details.get('runtime'))
        _text(root, 'tmdbid', mid)
        ET.SubElement(root, 'uniqueid', {'type': 'tmdb', 'default': 'true'}).text = str(mid)
        if details.get('imdb_id'):
            ET.SubElement(root, 'uniqueid', {'type': 'imdb', 'default': 'false'}).text = details['imdb_id']
        for genre in details.get('genres') or []:
            _text(root, 'genre', genre.get('name'))
        _atomic_xml(nfo, root)
        # This registry is separate from the M3U SQLite (which is rebuilt on import).
        with media.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS tmdb_matches (
                key TEXT PRIMARY KEY, tmdb_id INTEGER NOT NULL,
                title TEXT NOT NULL, year INTEGER, nfo_path TEXT NOT NULL
            )''')
            c.execute('INSERT OR REPLACE INTO tmdb_matches VALUES (?,?,?,?,?)',
                      (key, mid, title, year, str(nfo.relative_to(media.EXPORT_ROOT)) if nfo.is_relative_to(media.EXPORT_ROOT) else 'quarantine/' + key + '.nfo'))
        return {'ok': True, 'title': title, 'year': year, 'tmdb_id': mid,
                'nfo': nfo.name, 'note': 'Atualize a biblioteca no Jellyfin para carregar o NFO'}

# v0.6: manual TV-series identity, shared tvshow.nfo for Jellyfin.
# Episode files already contain SxxExx in their names. Jellyfin can match them
# using the parent series' TMDB ID after its library scan.
def apply_series(title, tmdb_id):
    import sqlite3
    import time
    from indexer import DB, norm
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 180:
        raise ValueError('Título de série inválido')
    mid = int(tmdb_id)
    if not 1 <= mid <= 999999999:
        raise ValueError('ID TMDB inválido')
    details = _fetch('tv/' + str(mid), {'language':'pt-BR'})
    if int(details.get('id', 0)) != mid:
        raise ValueError('TMDB retornou outra série')
    label = details.get('name') or details.get('original_name')
    if not label:
        raise ValueError('Série sem título no TMDB')
    # Confirm series is present in the M3U catalog and is already managed.
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10) as c:
        found=c.execute("SELECT title FROM entries WHERE kind='episode' AND normalized=? LIMIT 1", (norm(title),)).fetchone()
    if not found:
        raise ValueError('Série ausente no catálogo')
    with media.connect() as c:
        rows=c.execute("SELECT * FROM managed WHERE kind='episode' AND title=? LIMIT 1",(found[0],)).fetchone()
    if not rows:
        raise ValueError('Adicione pelo menos um episódio antes de identificar a série')
    series_dir=media.EXPORT_ROOT / 'Series' / media.clean_name(found[0])
    if not series_dir.resolve().is_relative_to(media.EXPORT_ROOT):
        raise ValueError('Diretório de série inválido')
    series_dir.mkdir(parents=True,exist_ok=True)
    root=ET.Element('tvshow')
    _text(root,'title',label)
    _text(root,'originaltitle',details.get('original_name'))
    date=details.get('first_air_date','') or ''
    _text(root,'year',date[:4] if re.fullmatch(r'\d{4}',date[:4]) else None)
    _text(root,'premiered',date)
    _text(root,'plot',details.get('overview'))
    _text(root,'status',details.get('status'))
    _text(root,'tmdbid',mid)
    ET.SubElement(root,'uniqueid',{'type':'tmdb','default':'true'}).text=str(mid)
    for genre in details.get('genres',[]):
        _text(root,'genre',genre.get('name'))
    target=series_dir/'tvshow.nfo'
    _atomic_xml(target,root)
    with media.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS tmdb_series_matches (
         normalized TEXT PRIMARY KEY, tmdb_id INTEGER NOT NULL,
         title TEXT NOT NULL, year INTEGER, updated_at REAL NOT NULL)''')
        year=int(date[:4]) if re.fullmatch(r'\d{4}',date[:4]) else None
        c.execute('INSERT OR REPLACE INTO tmdb_series_matches VALUES(?,?,?,?,?)',
                  (norm(title),mid,label,year,time.time()))
    return {'ok':True,'title':label,'tmdb_id':mid,'year':year,
            'nfo':str(target.relative_to(media.EXPORT_ROOT)),
            'note':'Atualize ou identifique a biblioteca de séries no Jellyfin'}
