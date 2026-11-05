"""Optional TheTVDB v4 search and confirmed metadata application.
Requires project API access under TheTVDB licensing terms.
"""
import json
import os
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from pathlib import Path
import media_control as media
import catalog_enrich as enrich

API_KEY=os.getenv('INDEXER_TVDB_API_KEY','').strip()
PIN=os.getenv('INDEXER_TVDB_PIN','').strip()
TOKEN=''; EXPIRES=0; LOCK=threading.Lock()
BASE='https://api4.thetvdb.com/v4/'

def configured(): return bool(API_KEY)

def _read(request):
    with urlopen(request,timeout=15) as r:
        data=r.read(750001)
        if len(data)>750000: raise ValueError('Resposta TVDB muito grande')
        return json.loads(data)

def _token():
    global TOKEN,EXPIRES
    with LOCK:
        if TOKEN and time.time()<EXPIRES: return TOKEN
        if not API_KEY: raise ValueError('TheTVDB não configurado')
        body={'apikey':API_KEY}
        if PIN: body['pin']=PIN
        data=_read(Request(BASE+'login',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Accept':'application/json'},method='POST'))
        token=(data.get('data') or {}).get('token')
        if not token: raise ValueError('Autenticação TheTVDB recusada')
        TOKEN=token;EXPIRES=time.time()+25*86400
        return TOKEN

def _fetch(route,params=None):
    url=BASE+route+('?' + urlencode(params) if params else '')
    return _read(Request(url,headers={'Authorization':'Bearer '+_token(),'Accept':'application/json'})).get('data')

def _yr(x):
    m=re.search(r'(?:19|20)\d{2}',str(x or ''))
    return int(m.group()) if m else None

def search(title,kind):
    if kind not in ('movie','episode') or not isinstance(title,str) or not 2<=len(title)<=150:
        raise ValueError('Consulta inválida')
    matches=_fetch('search',{'query':title,'type':'movie' if kind=='movie' else 'series'}) or []
    if isinstance(matches,dict):matches=matches.get('items',[])
    out=[]
    for r in matches[:12]:
        out.append({'id':r.get('tvdb_id') or r.get('id'),'title':r.get('name') or r.get('title'),
          'year':_yr(r.get('year') or r.get('first_air_time')),
          'overview':str(r.get('overview') or '')[:450],
          'poster':r.get('image_url') if str(r.get('image_url') or '').startswith('https://artworks.thetvdb.com/') else None})
    return {'items':[x for x in out if x['id'] and x['title']]}

def apply(kind,title,tvdb_id,key=None):
    if kind not in ('movie','episode'):raise ValueError('Tipo inválido')
    mid=int(tvdb_id)
    if not 1<=mid<=999999999:raise ValueError('ID inválido')
    resource='movies' if kind=='movie' else 'series'
    d=_fetch(resource+'/'+str(mid))
    if not isinstance(d,dict) or int(d.get('id',0))!=mid:raise ValueError('Identificação TVDB divergente')
    label=d.get('name') or d.get('slug')
    if not label: raise ValueError('Título ausente no TheTVDB')
    yr=_yr(d.get('year') or d.get('firstAired'))
    plot=d.get('overview') or ''
    poster=d.get('image') or ''
    if kind=='movie':
        if not key:raise ValueError('Informe o STRM gerenciado')
        with media.LOCK:
            with media.connect() as c: row=c.execute('SELECT * FROM managed WHERE key=? AND kind=?',(key,'movie')).fetchone()
            if not row:raise ValueError('Filme não gerenciado')
            if media.key_for('movie',title) and media.key_for('movie',title) != media.key_for('movie',row['title']):
                raise ValueError('O título não corresponde ao arquivo gerenciado')
            f=media.safe_dest(row['relative_path'])
            if row['status']=='quarantined': f=(media.DATA/'quarantine'/(key+'.strm')).resolve()
            nfo=f.with_suffix('.nfo')
            root=ET.Element('movie')
            ET.SubElement(root,'title').text=label
            if yr: ET.SubElement(root,'year').text=str(yr)
            ET.SubElement(root,'plot').text=plot
            ET.SubElement(root,'uniqueid',{'type':'tvdb','default':'true'}).text=str(mid)
            _save_xml(nfo,root)
            _poster_local(poster,f)
    else:
        from indexer import norm
        with media.connect() as c:
            found=c.execute("SELECT 1 FROM managed WHERE kind='episode' AND title=? LIMIT 1",(title,)).fetchone()
        if not found:raise ValueError('Adicione ao menos um episódio antes de identificar')
        folder=(media.EXPORT_ROOT/'Series'/media.clean_name(title)).resolve()
        if not folder.is_relative_to(media.EXPORT_ROOT):raise ValueError('Destino inválido')
        folder.mkdir(parents=True,exist_ok=True)
        root=ET.Element('tvshow')
        ET.SubElement(root,'title').text=label
        if yr: ET.SubElement(root,'year').text=str(yr)
        ET.SubElement(root,'plot').text=plot
        ET.SubElement(root,'uniqueid',{'type':'tvdb','default':'true'}).text=str(mid)
        _save_xml(folder/'tvshow.nfo',root)
        _poster_local(poster,folder/'poster.strm')
    enrich.save(kind,title,'tvdb',mid,label,yr,plot,poster)
    return {'ok':True,'title':label,'year':yr,'tvdb_id':mid}

def _save_xml(target,root):
    import secrets
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() and b'<uniqueid type="tvdb"' not in target.read_bytes() and b'<uniqueid type="tmdb"' not in target.read_bytes():
        raise ValueError('NFO existente não foi sobrescrito')
    tmp=target.with_name(target.name+'.'+secrets.token_hex(4)+'.tmp')
    try:
        tmp.write_bytes(ET.tostring(root,encoding='utf-8',xml_declaration=True));os.replace(tmp,target)
    finally:tmp.unlink(missing_ok=True)

def _poster_local(url,media_path):
    # Poster is saved in the Indexer's catalog metadata cache only; Jellyfin
    # metadata can retrieve imagery from TVDB via an installed provider.
    return
