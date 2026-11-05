#!/usr/bin/env python3
"""Read-only local catalog HTTP API. No media proxy and no STRM writes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import hmac
import auth
import media_control as media
import playlist_admin as playlists
import tmdb_meta as tmdb
import series_bulk as bulk
import catalog_enrich as enrich
import metadata_auto as automatic
import tvdb_meta as tvdb
import jellyfin_bridge as bridge
import content_filter
import channels
from catalog_identity import key_for
import os
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
from indexer import DB, norm

HOST = os.environ.get('INDEXER_LISTEN', '0.0.0.0')
PORT = int(os.environ.get('INDEXER_PORT', '8794'))
# Browser auth is handled by auth.py; INDEXER_TOKEN is no longer used.


def query_catalog(args):
    q = args.get('title', [''])[0].strip()
    if not q or len(q) > 200:
        raise ValueError('title obrigatório (máximo 200 caracteres)')
    kind = args.get('type', [''])[0].strip().lower()
    if kind and kind not in ('movie', 'episode'):
        raise ValueError('type deve ser movie ou episode')
    exact = args.get('exact', ['0'])[0] == '1'
    year = args.get('year', [''])[0]
    season = args.get('season', [''])[0]
    episode = args.get('episode', [''])[0]
    limit = max(1, min(100, int(args.get('limit', ['30'])[0])))
    offset = max(0, min(1000000, int(args.get('offset', ['0'])[0])))
    conditions, params = [], []
    if exact:
        conditions.append('normalized = ?')
        params.append(norm(q))
    else:
        # Escape LIKE metacharacters for literal text searches.
        s = norm(q).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append("normalized LIKE ? ESCAPE '\\'")
        params.append('%' + s + '%')
    for column, val in [('kind', kind), ('year', year), ('season', season), ('episode', episode)]:
        if val:
            if column != 'kind':
                val = int(val)
                if val < 0 or val > 9999:
                    raise ValueError('Parâmetro numérico fora do intervalo')
            conditions.append(column + ' = ?')
            params.append(val)
    block_sql, block_params = content_filter.where(for_panel=True)
    conditions.append(block_sql)
    params.extend(block_params)
    where = ' AND '.join(conditions)
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10) as db:
        db.row_factory = sqlite3.Row
        total = db.execute('SELECT COUNT(*) FROM entries WHERE ' + where, params).fetchone()[0]
        rows = db.execute('''SELECT id, source, original_title, title, category, kind,
                       year, season, episode, host FROM entries WHERE ''' + where +
                       ' ORDER BY source_order, normalized LIMIT ? OFFSET ?', params + [limit, offset]).fetchall()
    return {'total': total, 'limit': limit, 'offset': offset, 'items': [dict(r) for r in rows]}



def browse(args):
    kind = args.get('type', ['movie'])[0]
    if kind not in ('movie', 'episode'):
        raise ValueError('Tipo inválido')
    q = args.get('q', [''])[0].strip()
    cat = args.get('category', [''])[0].strip()
    source = args.get('source', [''])[0].strip()
    if any(len(x) > 200 for x in (q, cat, source)):
        raise ValueError('Filtro muito longo')
    limit = max(1, min(60, int(args.get('limit', ['24'])[0])))
    page = max(1, min(10000, int(args.get('page', ['1'])[0])))
    conditions = ['kind = ?']
    params = [kind]
    if q:
        term = norm(q).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append("normalized LIKE ? ESCAPE '\\'")
        params.append('%' + term + '%')
    if cat:
        conditions.append('category = ?')
        params.append(cat)
    if source:
        conditions.append('source = ?')
        params.append(source)
    block_sql, block_params = content_filter.where(for_panel=True)   # oculta categorias bloqueadas
    conditions.append(block_sql)
    params.extend(block_params)
    where = ' AND '.join(conditions)
    group = 'normalized, COALESCE(year, -1)' if kind == 'movie' else 'normalized'
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15) as db:
        db.row_factory = sqlite3.Row
        total = db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM entries WHERE ' + where + ' GROUP BY ' + group + ')', params).fetchone()[0]
        sql = ('SELECT MIN(id) id, MIN(title) title, MIN(year) year, MIN(category) category, '
               'MIN(source) source, COUNT(*) entries, COUNT(DISTINCT season) seasons, '
               'COUNT(DISTINCT source) source_count FROM entries WHERE ' + where +
               ' GROUP BY ' + group + ' ORDER BY MIN(normalized) LIMIT ? OFFSET ?')
        items = [dict(x) for x in db.execute(sql, params + [limit, (page-1)*limit])]
    kind_key = 'movie' if kind == 'movie' else 'episode'
    meta = enrich.for_cards(kind, [x['title'] for x in items])
    automatic.enqueue(kind, items)
    if kind == 'movie':
        states = media.content_states([key_for('movie',x['title'],x['year']) for x in items])
    else:
        states = {}
        with media.connect() as c:
            managed = c.execute("SELECT title, COUNT(*) FROM managed WHERE kind='episode' GROUP BY title").fetchall()
        from indexer import norm as normalize
        counts={normalize(t):n for t,n in managed}
    for it in items:
        it['metadata'] = meta.get(it['title'])
        if kind == 'movie': it['managed'] = states.get(key_for('movie',it['title'],it['year']))
        else: it['managed_count'] = counts.get(norm(it['title']),0)
    return {'total': total, 'limit': limit, 'page': page, 'items': items}


def filters():
    blocked = content_filter.blocked_set()
    hidden = set() if content_filter.load()['show_in_panel'] else blocked
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15) as db:
        categories = db.execute('SELECT category, COUNT(*) FROM entries GROUP BY category ORDER BY COUNT(*) DESC')
        cats = [{'name': name or 'Sem categoria', 'count': count, 'blocked': (name or '') in blocked}
                for name, count in categories if (name or '') not in hidden]
        sources = db.execute('SELECT source, COUNT(*) FROM entries GROUP BY source ORDER BY source')
        srcs = [{'name': name, 'count': count} for name, count in sources]
    return {'categories': cats, 'sources': srcs}


def episodes(args):
    title = args.get('title', [''])[0].strip()
    if not title or len(title) > 200:
        raise ValueError('Título obrigatório')
    source = args.get('source', [''])[0].strip()
    conditions = ["kind='episode'", 'normalized=?']
    params = [norm(title)]
    if source:
        conditions.append('source=?')
        params.append(source)
    with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15) as db:
        db.row_factory = sqlite3.Row
        sql = ('SELECT id,source,original_title,title,category,year,season,episode,host '
               'FROM entries WHERE ' + ' AND '.join(conditions) +
               ' ORDER BY season,episode,source LIMIT 2000')
        rows = [dict(x) for x in db.execute(sql, params)]
    keys=[key_for('episode',r['title'],season=r['season'],episode=r['episode']) for r in rows]
    states=media.content_states(keys)
    for r,k in zip(rows,keys): r['managed']=states.get(k)
    return {'items': rows, 'truncated': len(rows) == 2000}

class Handler(BaseHTTPRequestHandler):
    def _cookie_token(self):
        from http.cookies import SimpleCookie
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            return cookie['strm_session'].value if 'strm_session' in cookie else ''
        except Exception:
            return ''

    def session(self):
        return auth.decode_session(self._cookie_token())

    def authorized(self):
        return bool(self.session())

    def guard(self, mutable=False):
        session = self.session()
        if not session:
            self.send_json(401, {'error': 'Entre com usuário e senha'})
            return False
        if mutable:
            token = self._cookie_token()
            csrf = self.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(csrf, auth.csrf_for(token)):
                self.send_json(403, {'error': 'Sessão inválida: atualize a página'})
                return False
            origin = self.headers.get('Origin', '')
            if origin:
                from urllib.parse import urlsplit as split
                try:
                    remote = split(origin)
                    if remote.netloc != self.headers.get('Host', '') or remote.scheme not in ('http', 'https'):
                        self.send_json(403, {'error': 'Origem não permitida'})
                        return False
                except ValueError:
                    self.send_json(403, {'error': 'Origem inválida'})
                    return False
        return True

    def _cookie_header(self, value, max_age):
        secure = '; Secure' if self.headers.get('X-Forwarded-Proto', '').lower() == 'https' else ''
        # Do not trust arbitrary proxies for public deployments; use HTTPS termination on trusted network.
        return f'strm_session={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}'

    def _json_body(self, maximum=4096):
        size = int(self.headers.get('Content-Length', '0'))
        if size < 2 or size > maximum: raise ValueError('Tamanho de requisição inválido')
        obj = json.loads(self.rfile.read(size))
        if not isinstance(obj, dict): raise ValueError('Objeto JSON obrigatório')
        return obj

    def auth_post(self, path):
        if path == '/auth/login':
            ip = self.client_address[0]
            if not auth.has_admin():
                return self.send_json(503, {'error': 'Configure o usuário administrador no servidor'})
            if not auth.allow_login(ip):
                return self.send_json(429, {'error': 'Muitas tentativas. Tente novamente em cinco minutos'})
            try:
                data = self._json_body(1024)
                username = data.get('username', '')
                password = data.get('password', '')
                if not isinstance(username, str) or not isinstance(password, str) or not auth.verify(username, password):
                    auth.note_login_failure(ip)
                    return self.send_json(401, {'error': 'Usuário ou senha inválidos'})
            except (ValueError, TypeError, json.JSONDecodeError):
                auth.note_login_failure(ip)
                return self.send_json(400, {'error': 'Dados de acesso inválidos'})
            auth.clear_login_failures(ip)
            token = auth.create_session(username)
            return self.send_json(200, {'ok': True, 'user': username, 'csrf': auth.csrf_for(token)},
                                  cookies=[self._cookie_header(token, auth.TTL)])
        if path == '/auth/logout':
            if not self.guard(mutable=True): return
            return self.send_json(200, {'ok': True}, cookies=[self._cookie_header('', 0)])

    def send_login_redirect(self):
        self.send_response(303)
        self.send_header('Location', '/login')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def send_redirect(self, url):
        self.send_response(302)
        self.send_header('Location', url)
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def do_OPTIONS(self):
        if urlsplit(self.path).path.startswith(('/jf/', '/strm-api/')):
            return bridge.options(self)
        self.send_response(405)
        self.end_headers()

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path.startswith('/play/'):
            url = media.get_play_url(path[len('/play/'):])
            return self.send_redirect(url) if url else self.send_json(404, {'error': 'Fonte não disponível'})
        self.send_response(405)
        self.end_headers()

    def do_POST(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith(('/jf/', '/strm-api/')):
            return bridge.handle(self, 'POST', parsed)
        if parsed.path.startswith('/channels/'):
            if not self.guard(mutable=True): return
            try:
                d = self._json_body(1048576)
                if parsed.path == '/channels/add':
                    r = channels.add(d.get('ids'), d.get('q', ''), d.get('group', ''), d.get('source', ''))
                elif parsed.path == '/channels/remove':
                    r = channels.remove(d.get('ids'), bool(d.get('failed')))
                elif parsed.path == '/channels/update':
                    r = channels.update(d.get('id'), d.get('name'), d.get('group'), d.get('number'))
                elif parsed.path == '/channels/reorder':
                    r = channels.reorder(d.get('ids'))
                elif parsed.path == '/channels/renumber':
                    r = channels.renumber(d.get('start', 1))
                elif parsed.path == '/channels/settings':
                    r = channels.save_settings(d.get('epg_url'), d.get('only_ok'), bool(d.get('new_token')))
                elif parsed.path == '/channels/test':
                    r = channels.start_test(d.get('ids'), bool(d.get('only_untested')))
                else:
                    return self.send_json(404, {'error': 'Rota não encontrada'})
                return self.send_json(200, r)
            except (ValueError, TypeError, json.JSONDecodeError, OSError, sqlite3.Error) as exc:
                return self.send_json(400, {'error': str(exc)})
        if parsed.path == '/content-filter':
            if not self.guard(mutable=True): return
            try:
                return self.send_json(200, content_filter.save(self._json_body(262144)))
            except (ValueError, TypeError, json.JSONDecodeError, OSError) as exc:
                return self.send_json(400, {'error': str(exc)})
        if parsed.path == '/requests/retry':
            if not self.guard(mutable=True): return
            try:
                return self.send_json(200, bridge.admin_retry(self._json_body(512).get('id')))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self.send_json(400, {'error': str(exc)})
        if parsed.path == '/playlists/upload':
            if not self.guard(mutable=True): return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                name = self.headers.get('X-Playlist-Filename', '')
                result = playlists.upload(name, self.rfile, length)
                result['import'] = playlists.start_import()
                return self.send_json(200, result)
            except (ValueError, OSError) as exc:
                return self.send_json(400, {'error': str(exc)})
        if parsed.path in ('/playlists/github', '/playlists/refresh', '/playlists/reindex', '/tmdb/apply', '/tmdb/apply-series', '/series/batch'):
            if not self.guard(mutable=True): return
            try:
                data = self._json_body(4096)
                if parsed.path == '/playlists/github':
                    result = playlists.add_github(data.get('url', ''))
                    result['import'] = playlists.start_import()
                elif parsed.path == '/playlists/refresh':
                    result = playlists.refresh(data.get('filename', ''))
                    result['import'] = playlists.start_import()
                elif parsed.path == '/playlists/reindex':
                    result = playlists.start_import()
                elif parsed.path == '/tmdb/apply':
                    result = tmdb.apply(data.get('key'), data.get('tmdb_id'))
                elif parsed.path == '/tmdb/apply-series':
                    result = tmdb.apply_series(data.get('title'), data.get('tmdb_id'))
                else:
                    result = bulk.start(data.get('title'), data.get('source', ''), data.get('season'))
                return self.send_json(200, result)
            except (ValueError, OSError, TypeError) as exc:
                return self.send_json(400, {'error': str(exc)})
            except Exception as exc:
                print('ADMIN:', parsed.path, type(exc).__name__, flush=True)
                return self.send_json(502, {'error': 'Serviço externo indisponível ou dados inválidos'})
        if parsed.path in ('/delete', '/metadata/confirm', '/tvdb/apply'):
            if not self.guard(mutable=True): return
            try:
                data=self._json_body(4096)
                if parsed.path=='/delete':
                    # Synchronize with the batch queue, preventing a queued episode
                    # from being immediately re-created while deletion is in progress.
                    with bulk.PROCESS_LOCK:
                        result=media.delete_managed(key=data.get('key'),series_title=data.get('series_title'))
                        if data.get('series_title'):
                            with bulk.db_connect() as c:
                                c.execute("DELETE FROM series_jobs WHERE normalized=? AND state='queued'",(norm(data['series_title']),))
                elif parsed.path=='/tvdb/apply':
                    result=tvdb.apply(data.get('kind'),data.get('title'),data.get('tvdb_id'),data.get('key'))
                else:
                    provider=data.get('provider')
                    kind=data.get('kind')
                    title=data.get('title')
                    mid=int(data.get('id'))
                    if provider=='tmdb':
                        details=tmdb._fetch(('movie/' if kind=='movie' else 'tv/')+str(mid),{'language':'pt-BR'})
                        if int(details.get('id',0))!=mid: raise ValueError('ID TMDB divergente')
                        name=details.get('title' if kind=='movie' else 'name')
                        date=details.get('release_date' if kind=='movie' else 'first_air_date') or ''
                        year=int(date[:4]) if len(date)>=4 and date[:4].isdigit() else None
                        poster=('https://image.tmdb.org/t/p/w500'+details['poster_path']) if details.get('poster_path') else None
                        result=enrich.save(kind,title,'tmdb',mid,name,year,details.get('overview'),poster)
                    elif provider=='tvdb':
                        resource='movies' if kind=='movie' else 'series'
                        details=tvdb._fetch(resource+'/'+str(mid))
                        if int(details.get('id',0))!=mid:raise ValueError('ID TheTVDB divergente')
                        result=enrich.save(kind,title,'tvdb',mid,details.get('name'),details.get('year') or None,details.get('overview'),details.get('image'))
                    else:raise ValueError('Provedor inválido')
                if parsed.path=='/metadata/confirm':
                    result['jellyfin_posters_created']=enrich.publish_poster(kind,title)
                return self.send_json(200,result)
            except (ValueError,TypeError,KeyError) as e:
                return self.send_json(400,{'error':str(e)})
            except Exception as e:
                print('MANAGED ACTION:',type(e).__name__,flush=True)
                return self.send_json(502,{'error':'Não foi possível concluir a operação'})
        if parsed.path in ('/auth/login', '/auth/logout'):
            return self.auth_post(parsed.path)
        if parsed.path not in ('/validate', '/add', '/recheck'):
            return self.send_json(404, {'error': 'Rota não encontrada'})
        if not self.guard(mutable=True):
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 2 or size > 4096:
                raise ValueError('Tamanho de requisição inválido')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Objeto JSON obrigatório')
            if parsed.path == '/recheck':
                key = str(data.get('key', ''))
                if len(key) > 100:
                    raise ValueError('Chave inválida')
                result = media.recheck(key)
            else:
                entry_id = int(data['id'])
                if parsed.path == '/validate':
                    result = media.make_preview(entry_id)
                else:
                    result = media.add_entry(entry_id)
            self.send_json(200, result)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as ex:
            self.send_json(400, {'error': str(ex)})
        except sqlite3.Error:
            self.send_json(503, {'error': 'Banco de dados indisponível'})
        except OSError:
            self.send_json(503, {'error': 'Falha ao gravar arquivo gerenciado'})

    def send_static(self, filename, content_type):
        path = Path(__file__).resolve().parent / 'web' / filename
        try:
            data = path.read_bytes()
        except OSError:
            return self.send_json(404, {'error': 'Arquivo não encontrado'})
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self' https: http:; img-src 'self' data: https://image.tmdb.org https://artworks.thetvdb.com https://static.tvmaze.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, obj, cookies=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for cookie in cookies or []: self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(data)

    def send_m3u(self, text):
        data = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'audio/x-mpegurl; charset=utf-8')
        self.send_header('Content-Disposition', 'inline; filename="canais.m3u"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        # Lista de canais gerada: pública, protegida pela chave na URL (?t=...)
        if parsed.path in ('/channels/list.m3u', '/strm-api/channels.m3u', '/jf/channels.m3u'):
            text = channels.playlist(parse_qs(parsed.query).get('t', [''])[0])
            return self.send_m3u(text) if text is not None else self.send_json(403, {'error': 'Chave inválida'})
        if parsed.path.startswith(('/jf/', '/strm-api/')):
            return bridge.handle(self, 'GET', parsed)
        if parsed.path.startswith('/channels/'):
            if not self.guard(): return
            args = parse_qs(parsed.query)
            arg = lambda k, d='': args.get(k, [d])[0]
            try:
                if parsed.path == '/channels/all':
                    return self.send_json(200, channels.browse(arg('q').strip()[:200], arg('group')[:200], arg('source')[:200],
                                                              max(1, min(10000, int(arg('page', '1')))), max(1, min(200, int(arg('limit', '60'))))))
                if parsed.path == '/channels/facets':
                    return self.send_json(200, channels.facets(arg('source')[:200]))
                if parsed.path == '/channels/selected':
                    return self.send_json(200, channels.selected())
                if parsed.path == '/channels/test':
                    return self.send_json(200, channels.test_status())
                if parsed.path.startswith('/channels/logo/'):
                    found = channels.logo(parsed.path.rsplit('/', 1)[-1])
                    if not found:
                        return self.send_json(404, {'error': 'Sem logo'})
                    data, typ = found
                    self.send_response(200)
                    self.send_header('Content-Type', typ)
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'private, max-age=604800')
                    if typ == 'image/svg+xml':
                        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'")
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    return self.wfile.write(data)
            except (ValueError, OSError, sqlite3.Error) as exc:
                return self.send_json(400, {'error': str(exc)})
            return self.send_json(404, {'error': 'Rota não encontrada'})
        if parsed.path == '/requests':
            if not self.guard(): return
            return self.send_json(200, bridge.admin_list())
        if parsed.path == '/content-filter':
            if not self.guard(): return
            return self.send_json(200, content_filter.status())
        static = {'/': ('index.html','text/html; charset=utf-8'),
                  '/index.html': ('index.html','text/html; charset=utf-8'),
                  '/app.css': ('app.css','text/css; charset=utf-8'),
                  '/app.js': ('app.js','text/javascript; charset=utf-8')}
        if parsed.path == '/auth/me':
            session = self.session()
            if not session:
                return self.send_json(401, {'error': 'Sessão não iniciada', 'configured': auth.has_admin()})
            return self.send_json(200, {'ok': True, 'user': session['u'], 'csrf': auth.csrf_for(self._cookie_token())})
        if parsed.path == '/login':
            return self.send_static('login.html', 'text/html; charset=utf-8')
        if parsed.path == '/login.js':
            return self.send_static('login.js', 'text/javascript; charset=utf-8')
        if parsed.path in static:
            if parsed.path in ('/', '/index.html') and not self.authorized():
                return self.send_login_redirect()
            return self.send_static(*static[parsed.path])
        if parsed.path.startswith('/play/'):
            url = media.get_play_url(parsed.path[len('/play/'):])
            return self.send_redirect(url) if url else self.send_json(404, {'error': 'Fonte não disponível'})
        if parsed.path.startswith('/preview/'):
            url = media.preview_url(parsed.path[len('/preview/'):])
            return self.send_redirect(url) if url else self.send_json(410, {'error': 'Prévia expirada'})
        if parsed.path in ('/series/job','/series/pending'):
            if not self.guard(): return
            try:
                args=parse_qs(parsed.query)
                result=bulk.get_job(args.get('id',[''])[0]) if parsed.path=='/series/job' else bulk.summary(args.get('title',[''])[0], args.get('source',[''])[0])
                return self.send_json(200,result)
            except (ValueError,TypeError) as exc:
                return self.send_json(400,{'error':str(exc)})
        if parsed.path == '/playlists/status':
            if not self.guard(): return
            return self.send_json(200, playlists.status())
        if parsed.path.startswith('/artwork/'):
            if not self.guard(): return
            image=enrich.artwork(parsed.path[len('/artwork/'):])
            if not image: return self.send_json(404,{'error':'Imagem indisponível'})
            data=image.read_bytes()
            suffix=image.suffix
            self.send_response(200)
            self.send_header('Content-Type',{'.jpg':'image/jpeg','.png':'image/png','.webp':'image/webp'}[suffix])
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','private, max-age=3600')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers();self.wfile.write(data)
            return
        if parsed.path == '/tvdb/search':
            if not self.guard(): return
            try:
                a=parse_qs(parsed.query)
                return self.send_json(200,tvdb.search(a.get('title',[''])[0],a.get('type',['movie'])[0]))
            except ValueError as e: return self.send_json(400,{'error':str(e)})
            except Exception as e:
                print('TVDB_SEARCH:',type(e).__name__,flush=True)
                return self.send_json(502,{'error':'Falha ao consultar TheTVDB; confira o acesso de API'})
        if parsed.path == '/metadata/status':
            if not self.guard():return
            return self.send_json(200, automatic.stats())
        if parsed.path == '/tmdb/status':
            if not self.guard(): return
            return self.send_json(200, {'configured': tmdb.configured()})
        if parsed.path == '/tmdb/search':
            if not self.guard(): return
            try:
                args = parse_qs(parsed.query)
                result = tmdb.search(args.get('title', [''])[0], args.get('type', ['movie'])[0], args.get('year', [None])[0])
                return self.send_json(200, result)
            except (ValueError, TypeError) as exc:
                return self.send_json(400, {'error': str(exc)})
            except Exception as exc:
                print('TMDB_SEARCH:', type(exc).__name__, flush=True)
                return self.send_json(502, {'error': 'Não foi possível consultar o TMDB'})
        if parsed.path in ('/managed', '/alerts'):
            if not self.guard():
                return
            info = media.watched()
            return self.send_json(200, info if parsed.path == '/managed' else {'alerts': info['alerts']})
        if parsed.path == '/health':
            self.send_json(200, {'ok': True, 'index_exists': DB.is_file()})
            return
        if not self.guard():
            return
        if not DB.is_file():
            self.send_json(503, {'error': 'Importe ao menos uma lista primeiro'})
            return
        try:
            if parsed.path == '/stats':
                with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as db:
                    meta = db.execute("SELECT value FROM info WHERE key='metadata'").fetchone()
                self.send_json(200, json.loads(meta[0]))
            elif parsed.path == '/filters':
                self.send_json(200, filters())
            elif parsed.path == '/browse':
                self.send_json(200, browse(parse_qs(parsed.query)))
            elif parsed.path == '/episodes':
                self.send_json(200, episodes(parse_qs(parsed.query)))
            elif parsed.path == '/search':
                self.send_json(200, query_catalog(parse_qs(parsed.query)))
            elif parsed.path == '/source':
                # Only logged-in administrators can inspect URLs containing credentials.
                raw_id = parse_qs(parsed.query).get('id', [''])[0]
                source_id = int(raw_id)
                with sqlite3.connect(f'file:{DB}?mode=ro', uri=True) as db:
                    result = db.execute('SELECT media_url FROM entries WHERE id=?', (source_id,)).fetchone()
                if not result:
                    self.send_json(404, {'error': 'Fonte não encontrada'})
                else:
                    self.send_json(200, {'url': result[0], 'validated': False})
            else:
                self.send_json(404, {'error': 'Rota não encontrada'})
        except (ValueError, OverflowError) as exc:
            self.send_json(400, {'error': str(exc)})
        except sqlite3.Error:
            self.send_json(503, {'error': 'Catálogo indisponível'})


if __name__ == '__main__':
    auth.setup()
    media.initialize()
    enrich.setup()
    automatic.setup()
    threading.Thread(target=automatic.loop,args=(threading.Event(),),daemon=True).start()
    bulk.setup()
    threading.Thread(target=bulk.loop,args=(threading.Event(),),daemon=True).start()
    threading.Thread(target=media.run_monitor, args=(threading.Event(),), daemon=True).start()
    bridge.setup()
    threading.Thread(target=bridge.loop, args=(threading.Event(),), daemon=True).start()
    print(f'STRM Indexer API ouvindo em {HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
