#!/usr/bin/env python3
"""Local M3U catalog index. Does not fetch or play media."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PLAYLISTS = Path(os.environ.get('INDEXER_PLAYLIST_DIR', ROOT / 'playlists')).resolve()
DB = Path(os.environ.get('INDEXER_DB', ROOT / 'data' / 'catalog.sqlite'))
SUPPORTED = {'.m3u', '.m3u8'}
EPISODE = re.compile(r'(?i)(?:^|[^a-z0-9])S(\d{1,3})\s*E(\d{1,4})(?:\b|[^a-z0-9])')
YEAR = re.compile(r'(?<!\d)(?:19|20)\d{2}(?!\d)')
EXTINF_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r'\[[^\]]{0,70}\]', ' ', s)
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def parse_title(s, category):
    ep = EPISODE.search(s)
    if ep:
        series = s[:ep.start()].strip(' -._')
        series = re.sub(r'\s*[\[(](?:19|20)\d{2}[\])]\s*$', '', series).strip()
        years = YEAR.findall(s[:ep.start()])
        return 'episode', series or s, norm(series or s), int(years[-1]) if years else None, int(ep[1]), int(ep[2])
    kind = 'movie' if category.upper().startswith('FILMES') else ('unknown' if not category else 'other')
    years = YEAR.findall(s)
    title = re.sub(r'[\[(]\s*(?:19|20)\d{2}\s*[\])]', ' ', s) if kind == 'movie' else s
    title = re.sub(r'\[[^\]]{0,70}\]', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(' -._')
    return kind, title or s, norm(title or s), int(years[-1]) if years else None, None, None


def iter_entries(path):
    title = None
    attrs = {}
    with path.open('r', encoding='utf-8-sig', errors='replace') as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith('#EXTINF:'):
                header, sep, display = line.partition(',')
                title = (display if sep else '').strip()
                attrs = dict(EXTINF_ATTR.findall(header))
                if not title:
                    title = attrs.get('tvg-name', '').strip()
            elif line.startswith('#') or not line:
                continue
            elif title:
                url = line
                title0, attrs0 = title, attrs
                title, attrs = None, {}
                parts = urlsplit(url)
                if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                    continue
                yield title0, attrs0.get('group-title', ''), url


def build():
    if not PLAYLISTS.is_dir():
        raise SystemExit(f'Pasta de listas inexistente: {PLAYLISTS}')
    sources = sorted(p for p in PLAYLISTS.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    if not sources:
        raise SystemExit(f'Nenhum arquivo .m3u ou .m3u8 em {PLAYLISTS}')
    DB.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp = tempfile.mkstemp(suffix='.sqlite', dir=DB.parent)
    os.close(tmp_fd)
    conn = sqlite3.connect(tmp)
    stats = Counter()
    try:
        conn.executescript('''
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_order INTEGER NOT NULL,
                original_title TEXT NOT NULL, title TEXT NOT NULL, normalized TEXT NOT NULL,
                category TEXT, kind TEXT NOT NULL, year INTEGER, season INTEGER, episode INTEGER,
                host TEXT, media_url TEXT NOT NULL
            );
            CREATE TABLE info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        batch = []
        for order, path in enumerate(sources):
            count = 0
            for display, cat, url in iter_entries(path):
                kind, title, normal, year, season, episode = parse_title(display, cat)
                batch.append((path.name, order, display, title, normal, cat, kind, year, season, episode, urlsplit(url).hostname, url))
                stats[kind] += 1
                count += 1
                if len(batch) >= 5000:
                    conn.executemany('''INSERT INTO entries
                        (source,source_order,original_title,title,normalized,category,kind,year,season,episode,host,media_url)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', batch)
                    batch.clear()
            print(f'{path.name}: {count} entradas', flush=True)
        if batch:
            conn.executemany('''INSERT INTO entries
                (source,source_order,original_title,title,normalized,category,kind,year,season,episode,host,media_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', batch)
        conn.executescript('''
            CREATE INDEX idx_search ON entries(normalized, kind, year, season, episode);
            CREATE INDEX idx_group ON entries(kind, normalized, season, episode);
            CREATE INDEX idx_source ON entries(source);
        ''')
        metadata = {'updated_at': datetime.now(timezone.utc).isoformat(), 'sources': [p.name for p in sources], 'counts': dict(stats)}
        conn.execute('INSERT INTO info VALUES (?,?)', ('metadata', json.dumps(metadata, ensure_ascii=False)))
        conn.commit()
        conn.close()
        os.replace(tmp, DB)
        print('Catálogo atualizado:', json.dumps(metadata, ensure_ascii=False), flush=True)
    except BaseException:
        conn.close()
        Path(tmp).unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    cli = argparse.ArgumentParser()
    cli.add_argument('command', choices=['import'])
    args = cli.parse_args()
    if args.command == 'import':
        build()
