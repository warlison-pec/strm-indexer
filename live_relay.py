"""TV ao vivo da lista M3U para o StreamBox Web.

  GET /jf/livetv?m3u=URL&epg=URL[&force=1]   canais + programa atual/próximo (token Jellyfin no cabeçalho)
  GET /jf/relay?t=TOKEN&u=URL                repasse do canal (o <video> não envia cabeçalhos)

O navegador não consegue tocar canais http:// numa página https nem ler servidores que bloqueiam
outros domínios; por isso o fluxo passa pelo indexador. Somente endereços públicos (mesma proteção
contra SSRF usada na validação de fontes). Listas HLS (.m3u8) são reescritas para que os segmentos
também passem pelo repasse.
"""
import gzip
import hashlib
import io
import json
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request

import media_control as media

CACHE_DIR = media.DATA / 'livetv_cache'
CACHE_SECONDS = 6 * 3600
MAX_M3U = 150 * 1024 * 1024
MAX_EPG = 400 * 1024 * 1024
MAX_CHANNELS = 8000
UA = 'StreamBox/1.0'
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
VOD = re.compile(r'\.(mp4|mkv|avi|mov|wmv|flv)(\?.*)?$', re.I)


def _norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r'\b(hd|fhd|sd|4k|uhd|h265|hevc)\b', '', s)
    return re.sub(r'[^a-z0-9]+', '', s)


def _check(url):
    p = urlsplit(url or '')
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError('Endereço inválido')
    media._public_host(url)  # bloqueia localhost/rede interna


def _download(url, limit):
    _check(url)
    req = Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with media.OPENER.open(req, timeout=60) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Arquivo grande demais')
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data


def _parse_m3u(text):
    out, attrs, title = [], {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('#EXTINF'):
            head, _, name = line.partition(',')
            title = name.strip()
            attrs = {k.lower(): v for k, v in ATTR.findall(head)}
        elif not line or line.startswith('#'):
            continue
        elif title is not None:
            url = line
            if url.startswith(('http://', 'https://')) and not VOD.search(url) and '/movie/' not in url and '/series/' not in url:
                name = title or attrs.get('tvg-name') or 'Canal'
                out.append({'id': attrs.get('tvg-id') or _norm(name), 'tvg': attrs.get('tvg-id') or '', 'name': name,
                            'group': attrs.get('group-title') or 'Sem grupo', 'logo': attrs.get('tvg-logo') or None, 'url': url})
                if len(out) >= MAX_CHANNELS:
                    break
            title, attrs = None, {}
    return out


def _ts(v):
    v = (v or '').strip()
    for fmt in ('%Y%m%d%H%M%S %z', '%Y%m%d%H%M%S'):
        try:
            return datetime.strptime(v if '%z' in fmt else v[:14], fmt).timestamp()
        except ValueError:
            continue
    return None


def _parse_epg(data, channels):
    by_tvg = {c['tvg'].lower(): c['id'] for c in channels if c['tvg']}
    by_name = {_norm(c['name']): c['id'] for c in channels}
    xml_to_ch = dict(by_tvg)
    now, horizon = time.time(), time.time() + 12 * 3600
    progs = {}
    for _, el in ET.iterparse(io.BytesIO(data), events=('end',)):
        if el.tag == 'channel':
            xid = (el.get('id') or '').lower()
            if xid and xid not in xml_to_ch:
                for dn in el.findall('display-name'):
                    cid = by_name.get(_norm(dn.text or ''))
                    if cid:
                        xml_to_ch[xid] = cid
                        break
            el.clear()
        elif el.tag == 'programme':
            cid = xml_to_ch.get((el.get('channel') or '').lower())
            start, stop = _ts(el.get('start')), _ts(el.get('stop'))
            if cid and start and stop and stop > now and start < horizon:
                t = el.find('title')
                if t is not None and t.text:
                    progs.setdefault(cid, []).append([int(start * 1000), int(stop * 1000), t.text.strip()[:120]])
            el.clear()
    for v in progs.values():
        v.sort()
    return progs


def livetv(m3u, epg, force=False):
    if not m3u:
        raise ValueError('Informe a lista M3U')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1((m3u + '|' + (epg or '')).encode()).hexdigest()[:20]
    file = CACHE_DIR / (key + '.json')
    data = None
    if not force and file.exists() and time.time() - file.stat().st_mtime < CACHE_SECONDS:
        try:
            data = json.loads(file.read_text(encoding='utf-8'))
        except ValueError:
            data = None
    if data is None:
        channels = _parse_m3u(_download(m3u, MAX_M3U).decode('utf-8', errors='replace'))
        progs = {}
        if epg:
            try:
                progs = _parse_epg(_download(epg, MAX_EPG), channels)
            except Exception as exc:
                print('LIVETV: guia indisponível:', type(exc).__name__, flush=True)
        data = {'channels': channels, 'programmes': progs, 'updated': int(time.time())}
        tmp = file.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(file)
    now = time.time() * 1000
    out = []
    for c in data['channels']:
        p = data['programmes'].get(c['id'], [])
        cur = next((x for x in p if x[0] <= now < x[1]), None)
        nxt = next((x for x in p if x[0] > now), None)
        out.append({'id': c['id'], 'name': c['name'], 'group': c['group'], 'logo': c['logo'], 'url': c['url'],
                    'now': {'start': cur[0], 'stop': cur[1], 'title': cur[2]} if cur else None,
                    'next': {'start': nxt[0], 'stop': nxt[1], 'title': nxt[2]} if nxt else None})
    return {'channels': out, 'updated': data['updated']}


URI_ATTR = re.compile(r'URI="([^"]+)"')


def relay(handler, url, token):
    """Repassa um canal. Reescreve playlists HLS; o resto é transmitido em blocos."""
    _check(url)
    req = Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    resp = media.OPENER.open(req, timeout=20)
    try:
        final = resp.geturl()
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        first = resp.read(7)
        is_hls = first.startswith(b'#EXTM3U') or 'mpegurl' in ctype or urlsplit(final).path.lower().endswith('.m3u8')
        prefix = 'relay?t=' + quote(token, safe='') + '&u='
        if is_hls:
            body = (first + resp.read(4 * 1024 * 1024)).decode('utf-8', errors='replace')
            lines = []
            for line in body.splitlines():
                s = line.strip()
                if s and not s.startswith('#'):
                    line = prefix + quote(urljoin(final, s), safe='')
                elif 'URI="' in s:
                    line = URI_ATTR.sub(lambda m: 'URI="' + prefix + quote(urljoin(final, m.group(1)), safe='') + '"', line)
                lines.append(line)
            data = ('\n'.join(lines) + '\n').encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            handler.send_header('Content-Length', str(len(data)))
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            handler.wfile.write(data)
            return
        handler.send_response(200)
        handler.send_header('Content-Type', ctype or 'video/mp2t')
        length = resp.headers.get('Content-Length')
        if length and length.isdigit():
            handler.send_header('Content-Length', length)
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
        handler.wfile.write(first)
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass  # o usuário fechou o player
    finally:
        resp.close()
