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


# ------------------------------------------------------------------ links de provedor (ex.: get.php do Xtream)
SECRET_PARAMS = re.compile(r'(?i)([?&](?:password|pass|pwd|token|key|senha)=)[^&#]*')
PROVIDER_UA = 'VLC/3.0.20 LibVLC/3.0.20'   # muitos provedores só entregam a lista para "players"


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)          # redirecionamento também não pode ir para a rede interna
        return super().redirect_request(req, fp, code, msg, headers, newurl)


PUBLIC_OPENER = build_opener(PublicRedirect())


def _check_public(url):
    import epg                          # mesma regra das fontes de guia: só endereços públicos ou liberados
    epg._check(url)


def list_url(raw):
    """Link de lista aceito: GitHub (convertido para raw) ou um link http(s) de provedor (get.php etc.)."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 2000:
        raise ValueError('Endereço inválido')
    raw = raw.strip()
    host = (urlsplit(raw).hostname or '').lower()
    if host in ('github.com', 'raw.githubusercontent.com'):
        return github_raw_url(raw)
    p = urlsplit(raw)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Use um link http(s) da lista (ex.: …/get.php?username=…&type=m3u_plus)')
    _check_public(raw)
    return raw


def masked(url):
    """Link para exibir no painel, sem senha/token."""
    return SECRET_PARAMS.sub(lambda m: m.group(1) + '••••', url or '')


def _open_list(url):
    if urlsplit(url).hostname == 'raw.githubusercontent.com':
        return OPENER.open(Request(url, headers={'User-Agent': 'STRMIndexer/0.5', 'Accept': 'text/plain, */*'}), timeout=25)
    # provedores costumam montar a lista na hora: espera mais
    return PUBLIC_OPENER.open(Request(url, headers={'User-Agent': PROVIDER_UA, 'Accept': '*/*'}), timeout=120)


def _link_name(url):
    p = urlsplit(url)
    if p.hostname == 'raw.githubusercontent.com':
        return filename('github-' + p.path.split('/')[-1])
    base = 'link-' + re.sub(r'[^a-z0-9.-]+', '-', (p.hostname or 'lista').lower()).strip('-.')
    name, n = filename(base + '.m3u'), 2
    while (ROOT / name).exists() or name in _registry():
        name, n = filename(f'{base}-{n}.m3u'), n + 1
    return name


def _check_header(chunk):
    if not chunk.lstrip(b'\xef\xbb\xbf\r\n \t').startswith(b'#EXTM3U'):
        raise ValueError('O link não devolveu uma lista M3U (teste expirado, usuário/senha errados ou o provedor bloqueou o acesso)')


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
            raise ValueError('O link devolveu uma lista vazia (teste expirado, usuário/senha errados ou o provedor bloqueou o acesso)')
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
    """Cadastra uma lista por link (GitHub ou provedor) e baixa agora."""
    clean = list_url(url)
    # Prefixo evita colisão com listas enviadas como arquivo.
    name = _link_name(clean)
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        registry = _registry()
        if name in registry or (ROOT / name).exists():
            raise ValueError('Arquivo já cadastrado; atualize-o ou escolha outra fonte')
        with _open_list(clean) as response:
            if response.status != 200:
                raise ValueError(f'O servidor da lista respondeu HTTP {response.status}')
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
        raise ValueError('Somente listas cadastradas por link podem ser atualizadas')
    clean = list_url(registry[name])
    if not LOCK.acquire(blocking=False):
        raise ValueError('Outra atualização de playlist está em andamento')
    try:
        with _open_list(clean) as response:
            amount = _write_stream(ROOT / name, response)
        return {'filename': name, 'bytes': amount}
    finally:
        LOCK.release()


def _not_indexing():
    # renomear/apagar no meio da indexação faria ela procurar um arquivo que não existe mais
    if JOB.get('state') == 'running':
        raise ValueError('Aguarde a indexação terminar para renomear ou excluir listas')


def remove(name):
    """Apaga uma lista (arquivo e link cadastrado). O catálogo é reindexado em seguida."""
    name = filename(name)
    _not_indexing()
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
    """Renomeia a lista e/ou troca o link (baixando de novo). Reindexa em seguida."""
    name = filename(name)
    _not_indexing()
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
        raise ValueError('Só listas importadas por link têm link para editar')
    # o painel mostra o link sem a senha: se voltar igual, o link não mudou
    if url not in (None, '') and url.strip() == masked(registry.get(name, '')):
        url = None
    clean = list_url(url) if url not in (None, '') else None
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
            with _open_list(clean) as response:
                if response.status != 200:
                    raise ValueError(f'O servidor da lista respondeu HTTP {response.status}')
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
        {'name': p.name, 'size': p.stat().st_size, 'github': p.name in _registry(), 'url': masked(_registry().get(p.name, '')),
         'provider': p.name in _registry() and urlsplit(_registry()[p.name]).hostname != 'raw.githubusercontent.com'}
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
