"""Opt-in metadata library. Never contacts remote services while browsing."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from indexer import norm

DATA=Path(os.environ.get('INDEXER_STATE_DIR','/app/data')).resolve()
DB=DATA/'enrichment.sqlite'
ART=DATA/'artwork'

def setup():
    DATA.mkdir(parents=True,exist_ok=True)
    ART.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS metadata(
          content_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
          provider TEXT NOT NULL, provider_id TEXT NOT NULL, year INTEGER,
          overview TEXT, poster_file TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        # nota e votos (TMDB/TVmaze/Seerr/Jellyfin) para ordenar o catálogo pelos melhor avaliados
        cols={r[1] for r in c.execute('PRAGMA table_info(metadata)')}
        if 'rating' not in cols: c.execute('ALTER TABLE metadata ADD COLUMN rating REAL')
        if 'votes' not in cols: c.execute('ALTER TABLE metadata ADD COLUMN votes INTEGER')

def canonical(kind,title):
    if kind not in ('movie','episode'): raise ValueError('Tipo inválido')
    if not isinstance(title,str) or not 2 <= len(title) <= 180: raise ValueError('Título inválido')
    title=re.sub(r'\s*\((?:19|20)\d{2}\)\s*$','',title).strip()
    return kind+':'+norm(title)

def _image_fetch(url):
    if not url: return None
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname not in ('image.tmdb.org','artworks.thetvdb.com','static.tvmaze.com') or p.username or p.password or p.port not in (None,443):
        return None
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    try:
        with build_opener(NoRedirect).open(Request(url,headers={'User-Agent':'STRMIndexer/0.7'}),timeout=12) as r:
            ctype=r.headers.get('Content-Type','').split(';')[0].lower()
            if ctype not in ('image/jpeg','image/png','image/webp'): return None
            maximum=3*1024*1024
            data=r.read(maximum+1)
            if not data or len(data)>maximum: return None
            ext={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}[ctype]
            name=hashlib.sha256(data).hexdigest()[:32]+ext
            target=ART/name
            if not target.exists():
                temp=ART/(name+'.tmp'); temp.write_bytes(data); os.replace(temp,target)
            return name
    except Exception: return None

def _num(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def save(kind,title,provider,provider_id,matched_title,year,overview,poster,rating=None,votes=None):
    if provider not in ('tmdb','tvdb','tvmaze','jellyfin','seerr'): raise ValueError('Provedor inválido')
    if provider == 'jellyfin':
        provider_id = str(provider_id)
        if not re.fullmatch(r'[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}', provider_id): raise ValueError('ID Jellyfin inválido')
    else:
        provider_id=str(int(provider_id))
        if int(provider_id)<1: raise ValueError('ID inválido')
    if not matched_title or not isinstance(matched_title,str): raise ValueError('Título externo inválido')
    key=canonical(kind,title)
    art=_image_fetch(poster)
    with sqlite3.connect(DB) as c:
        old=c.execute('SELECT poster_file FROM metadata WHERE content_key=?',(key,)).fetchone()
        rating=_num(rating,0,10); votes=_num(votes,0,1e9)
        c.execute('''INSERT INTO metadata(content_key,kind,title,provider,provider_id,year,overview,poster_file,rating,votes)
          VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(content_key) DO UPDATE SET
          title=excluded.title,provider=excluded.provider,provider_id=excluded.provider_id,
          year=excluded.year,overview=excluded.overview,poster_file=COALESCE(excluded.poster_file,metadata.poster_file),
          rating=COALESCE(excluded.rating,metadata.rating),votes=COALESCE(excluded.votes,metadata.votes),
          updated_at=CURRENT_TIMESTAMP''',
          (key,kind,matched_title,provider,provider_id,int(year) if year else None,(overview or '')[:2000],art,
           rating,int(votes) if votes is not None else None))
    return {'ok':True,'provider':provider,'id':provider_id,'poster_saved':bool(art)}

def for_cards(kind,titles):
    keys=[]
    for title in titles:
        try: keys.append(canonical(kind,title))
        except ValueError: keys.append('')
    with sqlite3.connect(DB) as c:
        c.row_factory=sqlite3.Row
        records={r['content_key']:dict(r) for r in c.execute('SELECT * FROM metadata WHERE kind=?',(kind,)).fetchall()}
    out={}
    for t,k in zip(titles,keys):
        item=records.get(k)
        if item:
            item = dict(item)  # preserve cache rows; do not mutate source
            item['poster']='/artwork/'+item.get('poster_file') if item.get('poster_file') else None
            item.pop('poster_file',None)
            out[t]=item
    return out

def artwork(name):
    if not re.fullmatch(r'[a-f0-9]{32}\.(?:jpg|png|webp)',name): return None
    file=ART/name
    return file if file.is_file() else None

def publish_poster(kind, title):
    """Write Jellyfin-compatible local posters for Indexer-owned files only.
    Never overwrite user images. Called only after explicit metadata selection.
    """
    import shutil
    import media_control as media
    key=canonical(kind,title)
    with sqlite3.connect(DB) as c:
        row=c.execute('SELECT poster_file FROM metadata WHERE content_key=?',(key,)).fetchone()
    if not row or not row[0]:return 0
    image=artwork(row[0])
    if not image:return 0
    with media.connect() as c:
        all_rows=c.execute('SELECT kind,title,relative_path FROM managed').fetchall()
    files=[]
    for r in all_rows:
        if r['kind']!=kind:continue
        try:
            if canonical(kind,r['title']) != key:continue
            target=media.safe_dest(r['relative_path'])
            if kind=='movie':
                files.append(target.with_name(target.stem+'-poster'+image.suffix))
            else:
                files.append(target.parent.parent/('poster'+image.suffix))
        except ValueError:continue
    count=0
    for dest in set(files):
        if not dest.parent.is_dir() or dest.exists():continue
        # Respect an existing poster in any supported format.
        if any(dest.with_suffix(x).exists() for x in ('.jpg','.png','.webp')):continue
        shutil.copyfile(image,dest)
        count+=1
    return count


def save_poster_bytes(kind, title, data, mime):
    """Persist verified JPEG/PNG/WebP returned by a configured Jellyfin instance."""
    if mime not in ('image/jpeg', 'image/png', 'image/webp') or not 0 < len(data) <= 3*1024*1024:
        return False
    valid = ((mime=='image/jpeg' and data.startswith(b'\xff\xd8\xff')) or
             (mime=='image/png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or
             (mime=='image/webp' and data.startswith(b'RIFF') and data[8:12]==b'WEBP'))
    if not valid:
        return False
    ext = {'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}[mime]
    name = hashlib.sha256(data).hexdigest()[:32] + ext
    ART.mkdir(parents=True, exist_ok=True)
    target = ART / name
    if not target.exists():
        import secrets
        temp = ART / (name + '.' + secrets.token_hex(4) + '.tmp')
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
    with sqlite3.connect(DB, timeout=15) as db:
        db.execute('UPDATE metadata SET poster_file=COALESCE(poster_file, ?) WHERE content_key=?',
                   (name, canonical(kind,title)))
    return True
