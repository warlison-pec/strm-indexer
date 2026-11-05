"""Guia de programação (EPG/XMLTV) da lista de canais.

- O administrador cadastra fontes XMLTV (.xml ou .xml.gz); o indexador baixa e atualiza a cada 12 h.
- Cada canal da "Minha lista" é associado a um canal do guia: automático (por tvg-id e depois pelo
  nome) ou escolhido à mão. Também dá para marcar "sem guia".
- O indexador publica UM guia só com os canais selecionados (/strm-api/channels.xml?t=…), e a
  lista M3U gerada já aponta para ele no cabeçalho (x-tvg-url).
"""
import gzip
import os
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from urllib.request import Request
from xml.sax.saxutils import escape, quoteattr

import media_control as media
import channels
from live_relay import _ts, _norm

DIR = media.DATA / 'epg'
GUIDE = DIR / 'guide.xml'
MAX_SOURCE = 600 * 1024 * 1024      # tamanho máximo de cada fonte baixada
REFRESH_EVERY = 12 * 3600
PAST_HOURS, FUTURE_DAYS = 6, 3      # janela de programas publicada
UA = 'STRMIndexer-EPG/1.0'

_lock = threading.Lock()            # uma geração/atualização por vez
_wake = threading.Event()
state = {'running': False, 'message': '', 'dirty': True}


# ------------------------------------------------------------------ banco
def setup():
    with channels.connect() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS epg_sources(
                id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending', message TEXT NOT NULL DEFAULT '',
                channels INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0, fetched_at REAL);
            CREATE TABLE IF NOT EXISTS epg_channels(
                source_id INTEGER NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
                n TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '', PRIMARY KEY(source_id, id));
            CREATE INDEX IF NOT EXISTS epg_ch_n ON epg_channels(n);
            CREATE INDEX IF NOT EXISTS epg_ch_id ON epg_channels(id COLLATE NOCASE);
        ''')
        cols = {r['name'] for r in c.execute('PRAGMA table_info(selected)')}
        for col, ddl in (('epg_mode', "TEXT NOT NULL DEFAULT 'auto'"), ('epg_source', 'INTEGER'), ('epg_channel', 'TEXT')):
            if col not in cols:
                c.execute(f'ALTER TABLE selected ADD COLUMN {col} {ddl}')
    DIR.mkdir(parents=True, exist_ok=True)


def _iter(path):
    """Percorre um XMLTV grande sem acumular memória: devolve cada elemento ao fechar e
    descarta o que já foi lido."""
    it = ET.iterparse(path, events=('start', 'end'))
    _, root = next(it)
    for ev, el in it:
        if ev == 'end' and el.tag in ('channel', 'programme'):
            yield el
            root.clear()


def _src_path(sid):
    return DIR / f'src-{int(sid)}.xml'


# ------------------------------------------------------------------ fontes
def sources():
    with channels.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM epg_sources ORDER BY id')]


def add_source(url, name=''):
    url = (url or '').strip()
    if not re.match(r'^https?://', url) or len(url) > 1000:
        raise ValueError('Informe um endereço http:// ou https:// de um guia XMLTV')
    media._public_host(url)
    with channels.connect() as c:
        if c.execute('SELECT 1 FROM epg_sources WHERE url=?', (url,)).fetchone():
            raise ValueError('Essa fonte já foi adicionada')
        c.execute('INSERT INTO epg_sources(url, name) VALUES(?,?)', (url, (name or '').strip()[:120]))
    refresh_async()
    return {'sources': sources()}


def remove_source(sid):
    sid = int(sid)
    with channels.connect() as c:
        c.execute('DELETE FROM epg_sources WHERE id=?', (sid,))
        c.execute('DELETE FROM epg_channels WHERE source_id=?', (sid,))
        c.execute("UPDATE selected SET epg_source=NULL, epg_channel=NULL WHERE epg_source=?", (sid,))
    _src_path(sid).unlink(missing_ok=True)
    auto_match()
    return {'sources': sources()}


def _download(row):
    """Baixa a fonte para o disco (descompacta .gz) e indexa os canais dela."""
    url = row['url']
    media._public_host(url)
    tmp = DIR / f'src-{row["id"]}.part'
    req = Request(url, headers={'User-Agent': UA, 'Accept': '*/*', 'Accept-Encoding': 'gzip'})
    with media.OPENER.open(req, timeout=90) as resp, open(tmp, 'wb') as out:
        total = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SOURCE:
                raise ValueError('Arquivo maior que 600 MB')
            out.write(chunk)
    with open(tmp, 'rb') as fh:
        gz = fh.read(2) == b'\x1f\x8b'
    if gz:
        plain = DIR / f'src-{row["id"]}.plain'
        with gzip.open(tmp, 'rb') as src, open(plain, 'wb') as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        tmp.unlink()
        tmp = plain
    found = []
    for el in _iter(tmp):
        if el.tag == 'channel':
            cid = (el.get('id') or '').strip()
            names = [d.text.strip() for d in el.findall('display-name') if d.text and d.text.strip()]
            icon = el.find('icon')
            if cid:
                found.append((cid[:200], (names[0] if names else cid)[:200], ' '.join(sorted({_norm(n) for n in names + [cid]})),
                              (icon.get('src') if icon is not None else '' or '')[:500]))
    if not found:
        tmp.unlink(missing_ok=True)
        raise ValueError('Nenhum canal encontrado (o arquivo é um XMLTV?)')
    os.replace(tmp, _src_path(row['id']))
    with channels.connect() as c:
        c.execute('DELETE FROM epg_channels WHERE source_id=?', (row['id'],))
        c.executemany('INSERT OR REPLACE INTO epg_channels(source_id,id,name,n,icon) VALUES(?,?,?,?,?)',
                      [(row['id'], *f) for f in found])
        c.execute("UPDATE epg_sources SET status='ok', message='', channels=?, size=?, fetched_at=? WHERE id=?",
                  (len(found), _src_path(row['id']).stat().st_size, time.time(), row['id']))


def refresh(force=False):
    with _lock:
        state.update(running=True, message='Atualizando guias…')
        try:
            for row in sources():
                due = force or not row['fetched_at'] or time.time() - row['fetched_at'] > REFRESH_EVERY or not _src_path(row['id']).is_file()
                if not due:
                    continue
                try:
                    _download(row)
                except Exception as exc:
                    with channels.connect() as c:
                        c.execute("UPDATE epg_sources SET status='fail', message=? WHERE id=?", (str(exc)[:200], row['id']))
            auto_match()
            state['dirty'] = True
            _generate()
            state['message'] = ''
        finally:
            state['running'] = False


def refresh_async(force=True):
    threading.Thread(target=refresh, kwargs={'force': force}, daemon=True, name='epg-refresh').start()


def loop(stop):
    setup()
    while not stop.is_set():
        try:
            refresh(force=False)
        except Exception as exc:
            print('EPG:', type(exc).__name__, exc, flush=True)
        _wake.wait(3600)
        _wake.clear()


# ------------------------------------------------------------------ associação canal ↔ guia
_STRIP = re.compile(r'\b(hd|fhd|sd|4k|uhd|h265|hevc|alt|backup|br|\d{3,4}p)\b', re.I)


def _candidates_for(c, tvg_id, name):
    if tvg_id:
        row = c.execute('SELECT source_id, id FROM epg_channels WHERE id=? COLLATE NOCASE ORDER BY source_id LIMIT 1', (tvg_id,)).fetchone()
        if row:
            return row
    key = _norm(_STRIP.sub(' ', name or ''))
    if len(key) >= 2:
        # n guarda os nomes normalizados separados por espaço
        row = c.execute("SELECT source_id, id FROM epg_channels WHERE ' ' || n || ' ' LIKE ? ORDER BY source_id LIMIT 1",
                        ('% ' + key + ' %',)).fetchone()
        if row:
            return row
    return None


def auto_match():
    with channels.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT id, tvg_id, name FROM selected WHERE epg_mode='auto'")]
        for r in rows:
            m = _candidates_for(c, r['tvg_id'], r['name'])
            c.execute('UPDATE selected SET epg_source=?, epg_channel=? WHERE id=?',
                      (m['source_id'] if m else None, m['id'] if m else None, r['id']))
    state['dirty'] = True


def search(q, limit=40):
    key = _norm(q or '')
    with channels.connect() as c:
        if not key:
            rows = c.execute('SELECT e.source_id, e.id, e.name, s.name sname, s.url FROM epg_channels e JOIN epg_sources s ON s.id=e.source_id ORDER BY e.name LIMIT ?', (limit,))
        else:
            rows = c.execute('''SELECT e.source_id, e.id, e.name, s.name sname, s.url FROM epg_channels e JOIN epg_sources s ON s.id=e.source_id
                                WHERE e.n LIKE ? OR e.id LIKE ? ORDER BY LENGTH(e.name) LIMIT ?''', ('%' + key + '%', '%' + q.strip() + '%', limit))
        return [{'source_id': r['source_id'], 'id': r['id'], 'name': r['name'],
                 'source': r['sname'] or re.sub(r'^https?://', '', r['url'])[:40]} for r in rows]


def assign(cid, mode, source_id=None, channel=None):
    if mode not in ('auto', 'manual', 'none'):
        raise ValueError('Modo inválido')
    with channels.connect() as c:
        if mode == 'manual':
            if not c.execute('SELECT 1 FROM epg_channels WHERE source_id=? AND id=?', (int(source_id), str(channel))).fetchone():
                raise ValueError('Canal do guia não encontrado')
            c.execute("UPDATE selected SET epg_mode='manual', epg_source=?, epg_channel=? WHERE id=?", (int(source_id), str(channel), str(cid)))
        elif mode == 'none':
            c.execute("UPDATE selected SET epg_mode='none', epg_source=NULL, epg_channel=NULL WHERE id=?", (str(cid),))
        else:
            c.execute("UPDATE selected SET epg_mode='auto' WHERE id=?", (str(cid),))
    if mode == 'auto':
        auto_match()
    state['dirty'] = True
    return {'ok': True}


def labels():
    """Nome do canal do guia associado, para mostrar no painel."""
    with channels.connect() as c:
        return {r['sid']: {'name': r['name'], 'source': r['sname'] or re.sub(r'^https?://', '', r['url'])[:40]}
                for r in c.execute('''SELECT s.id sid, e.name, src.name sname, src.url FROM selected s
                                      JOIN epg_channels e ON e.source_id=s.epg_source AND e.id=s.epg_channel
                                      JOIN epg_sources src ON src.id=s.epg_source''')}


# ------------------------------------------------------------------ guia publicado
def out_id(row):
    """id usado no guia e no tvg-id da lista M3U."""
    return row['epg_channel'] if row.get('epg_channel') else (row.get('tvg_id') or '')


def _generate():
    with channels.connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM selected WHERE epg_channel IS NOT NULL ORDER BY position')]
    wanted = {}      # (fonte, id do guia) -> canal da lista
    for r in rows:
        wanted.setdefault((r['epg_source'], r['epg_channel']), r)
    now = time.time()
    lo, hi = now - PAST_HOURS * 3600, now + FUTURE_DAYS * 86400
    tmp = DIR / 'guide.part'
    with open(tmp, 'w', encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="STRM Indexer">\n')
        for (_, xid), r in wanted.items():
            out.write(f'  <channel id={quoteattr(xid)}><display-name>{escape(r["name"])}</display-name>')
            if r['logo']:
                out.write(f'<icon src={quoteattr(r["logo"])}/>')
            out.write('</channel>\n')
        written = set()
        for sid in sorted({k[0] for k in wanted}):
            path = _src_path(sid)
            if not path.is_file():
                continue
            ids = {k[1] for k in wanted if k[0] == sid} - written
            for el in _iter(path):
                if el.tag == 'programme' and el.get('channel') in ids:
                    start, stop = _ts(el.get('start')), _ts(el.get('stop'))
                    if start and stop and stop >= lo and start <= hi:
                        el.tail = None
                        out.write('  ' + ET.tostring(el, encoding='unicode') + '\n')
            written |= ids
        out.write('</tv>\n')
    os.replace(tmp, GUIDE)
    state['dirty'] = False


def guide_file():
    """Arquivo do guia, gerado de novo se a seleção mudou ou passou da validade."""
    stale = not GUIDE.is_file() or state['dirty'] or time.time() - GUIDE.stat().st_mtime > 6 * 3600
    if stale:
        with _lock:
            if not GUIDE.is_file() or state['dirty'] or time.time() - GUIDE.stat().st_mtime > 6 * 3600:
                _generate()
    return GUIDE


def summary():
    with channels.connect() as c:
        total = c.execute('SELECT COUNT(*) FROM selected').fetchone()[0]
        mapped = c.execute('SELECT COUNT(*) FROM selected WHERE epg_channel IS NOT NULL').fetchone()[0]
    return {'sources': sources(), 'mapped': mapped, 'total': total, 'running': state['running'], 'message': state['message']}
