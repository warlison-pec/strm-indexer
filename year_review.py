"""Revisão manual de filmes com o mesmo nome e anos diferentes.

"O Morro dos Ventos Uivantes" existe em 1939, 1992, 2011 e 2026. Quando as listas trazem o mesmo nome com
anos diferentes, o indexador não junta nada sozinho:

* cada ano vira um card próprio, com metadados (pôster, nota, ano) buscados para aquele ano;
* as fontes SEM ano desse título ficam fora do catálogo até alguém dizer de qual versão são;
* o título aparece na aba "Revisão", onde cada fonte pode ser atribuída a um ano.

As decisões ficam em data/year_review.sqlite (pela URL da fonte) e valem também nas próximas indexações.
"""
import sqlite3
import threading
import time
from pathlib import Path

from indexer import DB as CATALOG

DB = CATALOG.parent / 'year_review.sqlite'
LOCK = threading.RLock()
KEEP_NO_YEAR = 0          # ano 0 = "sem ano" escolhido de propósito


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE IF NOT EXISTS assign (
            media_url TEXT PRIMARY KEY, normalized TEXT NOT NULL, year INTEGER NOT NULL, decided_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS assign_norm ON assign(normalized);
        CREATE TABLE IF NOT EXISTS reviewed (
            normalized TEXT PRIMARY KEY, signature TEXT NOT NULL, reviewed_at REAL NOT NULL);
    ''')
    return c


def assignments():
    """{media_url: ano} para aplicar na indexação (ano None = sem ano)."""
    if not DB.exists():
        return {}
    with LOCK, connect() as c:
        return {r['media_url']: (r['year'] or None) for r in c.execute('SELECT media_url, year FROM assign')}


def reviewed():
    if not DB.exists():
        return {}
    with LOCK, connect() as c:
        return {r['normalized']: r['signature'] for r in c.execute('SELECT normalized, signature FROM reviewed')}


# ---------------------------------------------------------------- indexação

CONFLICTS_SQL = '''
    CREATE TABLE IF NOT EXISTS year_conflicts (normalized TEXT PRIMARY KEY, signature TEXT NOT NULL, pending INTEGER NOT NULL);
    DELETE FROM year_conflicts;
    INSERT INTO year_conflicts (normalized, signature, pending)
    SELECT normalized,
           GROUP_CONCAT(DISTINCT COALESCE(year, 'x')),
           0
    FROM entries
    WHERE kind = 'movie'
    GROUP BY normalized
    HAVING COUNT(DISTINCT year) >= 2;
'''


def _signature(years):
    """Assinatura das versões de um título: muda se aparecer um ano novo ou fontes sem ano."""
    return ','.join(sorted({str(y) if y else 'x' for y in years}))


def refresh_conflicts(conn):
    """Recalcula a tabela year_conflicts do catálogo (chamado na indexação e depois de cada revisão)."""
    conn.executescript(CONFLICTS_SQL)
    done = reviewed()
    rows = conn.execute('SELECT normalized FROM year_conflicts').fetchall()
    updates = []
    for (n,) in rows:
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM entries WHERE kind='movie' AND normalized=?", (n,))]
        sig = _signature(years)
        updates.append((sig, 0 if done.get(n) == sig else 1, n))
    conn.executemany('UPDATE year_conflicts SET signature=?, pending=? WHERE normalized=?', updates)


def apply(conn):
    """Aplica as decisões salvas nas entradas recém-indexadas."""
    decided = assignments()
    if decided:
        conn.executemany("UPDATE entries SET year=? WHERE kind='movie' AND media_url=?",
                         [(y, u) for u, y in decided.items()])
    return len(decided)


def hidden_condition():
    """Condição SQL do catálogo: esconde fontes sem ano de títulos com versões de anos diferentes ainda não revisados."""
    return ("NOT (kind='movie' AND year IS NULL AND normalized IN "
            "(SELECT normalized FROM year_conflicts WHERE pending=1))")


def has_table(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='year_conflicts'").fetchone())


# ---------------------------------------------------------------- painel

def _cat():
    c = sqlite3.connect(CATALOG, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def listing(page=1, limit=20, show='pending', q=''):
    from indexer import norm
    page, limit = max(1, int(page)), max(1, min(50, int(limit)))
    q = norm(str(q or ''))[:80]
    if not CATALOG.exists():
        return {'items': [], 'total': 0, 'pending': 0}
    with _cat() as db:
        if not has_table(db):
            refresh_conflicts(db)
            db.commit()
        pending = db.execute('SELECT COUNT(*) FROM year_conflicts WHERE pending=1').fetchone()[0]
        conds, qp = [], []
        if show == 'pending':
            conds.append('y.pending=1')
        for word in q.split()[:6]:          # todas as palavras, em qualquer ordem ("morro ventos")
            conds.append("y.normalized LIKE ? ESCAPE '\\'")
            qp.append('%' + word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        where = ('WHERE ' + ' AND '.join(conds)) if conds else ''
        total = db.execute(f'SELECT COUNT(*) FROM year_conflicts y {where}', qp).fetchone()[0]
        rows = db.execute(f'''
            SELECT y.normalized, y.pending, MIN(e.title) title, COUNT(*) n,
                   SUM(e.year IS NULL) no_year
            FROM year_conflicts y JOIN entries e ON e.normalized = y.normalized AND e.kind='movie'
            {where}
            GROUP BY y.normalized
            ORDER BY (SUM(e.year IS NULL) > 0) DESC, COUNT(*) DESC, y.normalized
            LIMIT ? OFFSET ?''', qp + [limit, (page - 1) * limit]).fetchall()
        items = []
        for r in rows:
            entries = db.execute('''
                SELECT id, title, original_title, source, host, year FROM entries
                WHERE kind='movie' AND normalized=? ORDER BY year IS NULL, year, source, original_title LIMIT 120''',
                (r['normalized'],)).fetchall()
            variants = {}
            for e in entries:
                v = variants.setdefault(e['year'], {'year': e['year'], 'count': 0, 'lists': set()})
                v['count'] += 1
                v['lists'].add(e['source'])
            items.append({
                'normalized': r['normalized'], 'title': r['title'], 'entries_total': r['n'],
                'no_year': r['no_year'], 'pending': bool(r['pending']),
                'variants': [{'year': v['year'], 'count': v['count'], 'lists': len(v['lists'])}
                             for v in sorted(variants.values(), key=lambda v: (v['year'] is None, v['year'] or 0))],
                'entries': [dict(e) for e in entries],
            })
    return {'items': items, 'total': total, 'pending': pending, 'page': page, 'limit': limit}


def _valid_year(y):
    if y in (None, '', 0, '0'):
        return None
    y = int(y)
    if not 1888 <= y <= time.localtime().tm_year + 2:
        raise ValueError('Ano inválido')
    return y


def save(normalized, items=None, merge_year=None):
    """Grava a revisão de um título.

    items: [{id, year}] — year None/0 = sem ano.  merge_year: põe TODAS as fontes do título nesse ano.
    Depois de salvar, o título sai da lista de pendentes (até aparecer uma versão nova).
    """
    normalized = str(normalized or '').strip()
    if not normalized:
        raise ValueError('Título inválido')
    now = time.time()
    with LOCK, _cat() as db, connect() as rv:
        if merge_year is not None:
            y = _valid_year(merge_year)
            rows = db.execute("SELECT id, media_url FROM entries WHERE kind='movie' AND normalized=?", (normalized,)).fetchall()
            changes = [(r['id'], r['media_url'], y) for r in rows]
        else:
            changes = []
            for it in items or []:
                row = db.execute("SELECT id, media_url FROM entries WHERE kind='movie' AND normalized=? AND id=?",
                                 (normalized, int(it.get('id')))).fetchone()
                if row:
                    changes.append((row['id'], row['media_url'], _valid_year(it.get('year'))))
        for eid, url, y in changes:
            db.execute('UPDATE entries SET year=? WHERE id=?', (y, eid))
            rv.execute('INSERT OR REPLACE INTO assign (media_url, normalized, year, decided_at) VALUES (?,?,?,?)',
                       (url, normalized, y or KEEP_NO_YEAR, now))
        years = [r[0] for r in db.execute("SELECT DISTINCT year FROM entries WHERE kind='movie' AND normalized=?", (normalized,))]
        rv.execute('INSERT OR REPLACE INTO reviewed (normalized, signature, reviewed_at) VALUES (?,?,?)',
                   (normalized, _signature(years), now))
        rv.commit()
        refresh_conflicts(db)
        db.commit()
    return {'ok': True, 'changed': len(changes)}


def remove(entry_id):
    """Retira uma fonte que não abre: sai do catálogo agora e das próximas indexações (reversível)."""
    import link_pruner
    with LOCK, _cat() as db:
        row = db.execute("SELECT id, normalized, media_url, source, original_title FROM entries WHERE id=? AND kind='movie'",
                         (int(entry_id),)).fetchone()
        if not row:
            raise ValueError('Fonte não encontrada (reindexada?). Atualize a página.')
        if link_pruner.is_protected(row['media_url']):
            raise ValueError('Esta fonte é usada por um STRM no Jellyfin. Troque a fonte em "Gerenciados" antes de retirar.')
        link_pruner.remove_manual(row['media_url'], 'movie', row['source'], row['original_title'])
        db.execute('DELETE FROM entries WHERE id=?', (row['id'],))
        refresh_conflicts(db)
        db.commit()
    return {'ok': True}


def dismiss(normalized):
    """"Está certo assim": mantém as versões como estão e mostra as fontes sem ano no catálogo."""
    return save(normalized, items=[])


def reopen(normalized):
    with LOCK, connect() as rv:
        rv.execute('DELETE FROM reviewed WHERE normalized=?', (normalized,))
    with _cat() as db:
        refresh_conflicts(db)
        db.commit()
    return {'ok': True}


def meta_title(title, year, conflict):
    """Título usado para buscar/guardar metadados: com versões de anos diferentes, cada ano tem os seus."""
    return f'{title} #{year}' if conflict and year else title
