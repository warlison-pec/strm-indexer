"""Authenticated management of local M3U files and GitHub raw source URLs."""
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler

import indexer

ROOT = indexer.PLAYLISTS
STATE = Path(os.environ.get('INDEXER_STATE_DIR', '/app/data')).resolve()
REGISTRY = STATE / 'playlist_sources.json'
MAX_SIZE = 100 * 1024 * 1024
LOCK = threading.Lock()
JOB = {'state': 'idle', 'message': '', 'updated': 0}


def filename(value):
    if not isinstance(value, str):
        raise ValueError('Nome de arquivo inválido')
    name = Path(value.replace('\\', '/')).name
    if not re.fullmatch(r'[a-zA-Z0-9À-ÿ _().\-]{1,150}\.(?:m3u8|m3u)', name, re.I):
        raise ValueError('Use um nome .m3u ou .m3u8, sem caracteres especiais')
    return name


def _registry():
    try:
        data = json.loads(REGISTRY.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_registry(data):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_name(REGISTRY.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, REGISTRY)


def github_raw_url(raw):
    if not isinstance(raw, str) or len(raw) > 2000:
        raise ValueError('Endereço muito longo')
    p = urlsplit(raw.strip())
    if p.scheme != 'https' or p.username or p.password or p.query or p.fragment or p.port:
        raise ValueError('Use uma URL HTTPS pública do GitHub, sem parâmetros')
    host = (p.hostname or '').lower()
    seg = p.path.strip('/').split('/')
    if host == 'github.com' and len(seg) >= 5 and seg[2] == 'blob':
        url = 'https://raw.githubusercontent.com/' + '/'.join(seg[:2] + seg[3:])
    elif host == 'raw.githubusercontent.com' and len(seg) >= 4:
        url = raw.strip()
    else:
        raise ValueError('Use o link raw.githubusercontent.com ou github.com/.../blob/...')
    if not urlsplit(url).path.lower().endswith(('.m3u', '.m3u8')):
        raise ValueError('O arquivo do GitHub deve terminar em .m3u ou .m3u8')
    if any(s in ('', '.', '..') for s in urlsplit(url).path.strip('/').split('/')):
        raise ValueError('Caminho GitHub inválido')
    return url


class GitHubRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urlsplit(newurl)
        if p.scheme != 'https' or p.hostname != 'raw.githubusercontent.com' or p.username or p.password or p.port:
            raise ValueError('Redirecionamento fora do GitHub não permitido')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = build_opener(GitHubRedirect())


def _check_header(chunk):
    if not chunk.lstrip(b'\xef\xbb\xbf\r\n \t').startswith(b'#EXTM3U'):
        raise ValueError('O conteúdo não começa com #EXTM3U')


def _write_stream(target, fileobj, count=None):
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / ('.upload-' + secrets.token_hex(12) + '.tmp')
    try:
        total = 0
        with tmp.open('wb') as dst:
            first = True
            while True:
                remaining = MAX_SIZE - total
                if count is not None:
                    remaining = min(remaining, count - total)
                if remaining <= 0:
                    if count is not None and total == count:
                        break
                    raise ValueError('Lista excede 100 MiB')
                block = fileobj.read(min(65536, remaining + (0 if count is not None else 1)))
                if not block:
                    break
                if first:
                    _check_header(block)
                    first = False
                total += len(block)
                if total > MAX_SIZE:
                    raise ValueError('Lista excede 100 MiB')
                dst.write(block)
        if count is not None and total != count:
            raise ValueError('Upload incompleto')
        if total < 8:
            raise ValueError('Lista vazia')
        os.replace(tmp, target)
        return total
    finally:
        tmp.unlink(missing_ok=True)


def upload(rawname, stream, content_length):
    name = filename(unquote(rawname))
    if content_length <= 0 or content_length > MAX_SIZE:
        raise ValueError('Arquivo deve ter até 100 MiB')
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        dest = ROOT / name
        if dest.exists():
            raise ValueError('Já existe uma lista com esse nome; use outro nome')
        amount = _write_stream(dest, stream, content_length)
        return {'filename': name, 'bytes': amount}
    finally:
        LOCK.release()


def add_github(url):
    clean = github_raw_url(url)
    p = urlsplit(clean)
    # Prefix prevents a collision with locally uploaded files.
    name = filename('github-' + p.path.split('/')[-1])
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        registry = _registry()
        if name in registry or (ROOT / name).exists():
            raise ValueError('Arquivo já cadastrado; atualize-o ou escolha outra fonte')
        req = Request(clean, headers={'User-Agent': 'STRMIndexer/0.5', 'Accept': 'text/plain, */*'})
        with OPENER.open(req, timeout=25) as response:
            if response.status != 200:
                raise ValueError('O GitHub não retornou HTTP 200')
            amount = _write_stream(ROOT / name, response)
        registry[name] = clean
        _save_registry(registry)
        return {'filename': name, 'bytes': amount}
    finally:
        LOCK.release()


def refresh(name):
    name = filename(name)
    registry = _registry()
    if name not in registry:
        raise ValueError('Somente listas cadastradas pelo link GitHub podem ser atualizadas')
    clean = github_raw_url(registry[name])
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        req = Request(clean, headers={'User-Agent': 'STRMIndexer/0.5'})
        with OPENER.open(req, timeout=25) as response:
            amount = _write_stream(ROOT / name, response)
        return {'filename': name, 'bytes': amount}
    finally:
        LOCK.release()


def remove(name):
    """Apaga uma lista (arquivo e link cadastrado). O catálogo é reindexado em seguida."""
    name = filename(name)
    target = ROOT / name
    if not target.is_file():
        raise ValueError('Lista não encontrada')
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        target.unlink()
        registry = _registry()
        if registry.pop(name, None) is not None:
            _save_registry(registry)
        return {'filename': name, 'removed': True}
    finally:
        LOCK.release()


def edit(name, new_name=None, url=None):
    """Renomeia a lista e/ou troca o link do GitHub (baixando de novo). Reindexa em seguida."""
    name = filename(name)
    source = ROOT / name
    if not source.is_file():
        raise ValueError('Lista não encontrada')
    registry = _registry()
    final = name
    if new_name not in (None, ''):
        new_name = str(new_name).strip()
        if not new_name.lower().endswith(('.m3u', '.m3u8')):
            new_name += source.suffix          # mantém a extensão se o usuário não digitar
        final = filename(new_name)
    if url not in (None, '') and name not in registry:
        raise ValueError('Só listas importadas por link do GitHub têm link para editar')
    clean = github_raw_url(url) if url not in (None, '') else None
    if final == name and (clean is None or clean == registry.get(name)):
        return {'filename': name, 'changed': False}
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        if final != name:
            if (ROOT / final).exists():
                raise ValueError('Já existe uma lista com esse nome')
            os.replace(source, ROOT / final)
            if name in registry:
                registry[final] = registry.pop(name)
        if clean is not None:
            req = Request(clean, headers={'User-Agent': 'STRMIndexer/0.5', 'Accept': 'text/plain, */*'})
            with OPENER.open(req, timeout=25) as response:
                if response.status != 200:
                    raise ValueError('O GitHub não retornou HTTP 200')
                _write_stream(ROOT / final, response)
            registry[final] = clean
        _save_registry(registry)
    finally:
        LOCK.release()
    if final != name:
        # a seleção de canais guarda o nome da lista de origem só como informação
        try:
            import channels
            with channels.connect() as c:
                c.execute('UPDATE selected SET source=? WHERE source=?', (final, name))
        except Exception:
            pass
    return {'filename': final, 'changed': True}


def status():
    return {**JOB, 'files': [
        {'name': p.name, 'size': p.stat().st_size, 'github': p.name in _registry(), 'url': _registry().get(p.name, '')}
        for p in sorted(ROOT.iterdir()) if p.is_file() and p.suffix.lower() in indexer.SUPPORTED
    ] if ROOT.exists() else []}


def start_import():
    with LOCK:
        if JOB['state'] == 'running':
            raise ValueError('Importação já em execução')
        JOB.update(state='running', message='Indexando listas...', updated=time.time())
    def run():
        try:
            indexer.build()
            JOB.update(state='done', message='Catálogo atualizado', updated=time.time())
        except Exception as exc:
            JOB.update(state='error', message=str(exc)[:250], updated=time.time())
    threading.Thread(target=run, daemon=True, name='playlist-import').start()
    return {'started': True}
