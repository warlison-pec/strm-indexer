"""Read-only Jellyfin metadata lookup; never modifies Jellyfin objects.
Uses server-side header authentication; key never appears in JSON or image URLs.
"""
import json
import os
import re
from urllib.parse import urlencode, urlsplit
from indexer import norm
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = os.environ.get('INDEXER_JELLYFIN_URL', '').rstrip('/')
TOKEN = os.environ.get('INDEXER_JELLYFIN_API_KEY', '').strip()

def configured():
    p = urlsplit(BASE)
    return bool(TOKEN and p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password and not p.query and not p.fragment)

def _get(path, params=None, maximum=2_000_000):
    if not configured():
        raise ValueError('Jellyfin não configurado')
    url = BASE + path + ('?' + urlencode(params) if params else '')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with build_opener(NoRedirect).open(Request(url, headers={'Authorization': f'MediaBrowser Token="{TOKEN}"',
                                       'Accept': 'application/json',
                                       'User-Agent': 'STRMIndexer/0.8.3'}), timeout=12) as resp:
        data = resp.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError('Resposta Jellyfin muito grande')
        return data, resp.headers.get('Content-Type', '').split(';')[0].lower()

def search(title, kind):
    if kind not in ('movie', 'episode') or not isinstance(title, str) or not 2 <= len(title) <= 150:
        return {'items': []}
    # Use at most two local searches. Jellyfin text search can differ on
    # punctuation, but matching remains exact after normalization in the worker.
    requests = [title.strip()]
    # A punctuation-free title is useful for e.g. "007 - ..." vs "007: ...".
    import re as _re
    loose = _re.sub(r'\s+', ' ', _re.sub(r'[^\w\s]', ' ', title, flags=_re.UNICODE)).strip()
    if loose and loose.casefold() != title.strip().casefold():
        requests.append(loose)
    found = {}
    for query in requests[:2]:
        params = {'Recursive': 'true', 'IncludeItemTypes': 'Movie' if kind == 'movie' else 'Series',
                  'SearchTerm': query, 'Fields': 'ProviderIds,Overview,CommunityRating', 'Limit': '30'}
        data, _ = _get('/Items', params)
        for record in json.loads(data).get('Items', []):
            if record.get('Id'):
                found[str(record['Id'])] = record
    result = {'Items': list(found.values())}
    items = []
    for x in result.get('Items', []):
        if x.get('Type') != ('Movie' if kind == 'movie' else 'Series'):
            continue
        item_id = str(x.get('Id') or '')
        if not re.fullmatch(r'[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}', item_id):
            continue
        name = x.get('Name') or ''
        if not name:
            continue
        year = x.get('ProductionYear')
        if not isinstance(year, int) or year < 1900 or year > 2100:
            year = None
        items.append({'id': item_id, 'title': name, 'original_title': x.get('OriginalTitle') or '',
                      'year': year, 'overview': str(x.get('Overview') or '')[:2000],
                      'rating': x.get('CommunityRating'), 'votes': None,
                      'has_image': bool((x.get('ImageTags') or {}).get('Primary'))})
    return {'items': items}

def poster(item_id):
    if not re.fullmatch(r'[a-fA-F0-9]{32}|[a-fA-F0-9-]{36}', str(item_id)):
        return None
    try:
        data, mime = _get('/Items/' + str(item_id) + '/Images/Primary',
                          {'maxWidth': 500, 'quality': 80}, maximum=3*1024*1024)
        if mime not in ('image/jpeg','image/png','image/webp') or not data:
            return None
        return data, mime
    except Exception:
        return None
