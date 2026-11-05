"""Canonical content identities, independent of playlist source and stream URL."""
import re
from indexer import norm

def key_for(kind, title, year=None, season=None, episode=None):
    title = str(title or '').strip()
    if not title: return None
    if kind == 'episode':
        if season is None or episode is None: return None
        return 'episode:' + norm(title) + ':' + str(int(season)) + ':' + str(int(episode))
    if kind == 'movie':
        match = re.search(r'\(\s*((?:19|20)\d{2})\s*\)\s*$', title)
        if match:
            year = year or int(match.group(1))
            title = title[:match.start()].strip()
        return 'movie:' + norm(title) + ':' + (str(int(year)) if year else '?')
    return None

def from_managed(row):
    kind, title = row['kind'], row['title']
    if kind == 'episode':
        match = re.search(r'S(\d{1,3})E(\d{1,3})', row['relative_path'], re.I)
        if not match: return None
        return key_for(kind, title, season=int(match.group(1)), episode=int(match.group(2)))
    return key_for(kind,title)

def compatible(a,b):
    if a == b: return True
    if not a or not b or not a.startswith('movie:') or not b.startswith('movie:'): return False
    x,y=a.rsplit(':',1),b.rsplit(':',1)
    return x[0]==y[0] and ('?' in (x[1], y[1]))
