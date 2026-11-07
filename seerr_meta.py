"""Read-only Seerr metadata search. No requests, approvals, or library mutations.

The Seerr /api/v1/search response includes TMDB IDs, poster paths, year and overview.
Never expose the Seerr API key to the browser or in logged URLs.
"""
import json
import os
import re
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = os.environ.get('INDEXER_SEERR_URL', '').rstrip('/')
TOKEN = os.environ.get('INDEXER_SEERR_API_KEY', '').strip()


def configured():
    parsed = urlsplit(BASE)
    return bool(TOKEN and parsed.scheme in ('http', 'https') and parsed.hostname
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)


def search(title, kind):
    """Return movie or TV matches in metadata_auto's standard schema."""
    if kind not in ('movie', 'episode') or not isinstance(title, str) or not 2 <= len(title.strip()) <= 150:
        return {'items': []}
    if not configured():
        return {'items': []}
    # The running user's Seerr instance accepts percent-encoded spaces, not '+' for this query.
    query = urlencode({'query': title.strip(), 'page': '1', 'language': 'en'}, quote_via=quote)
    url = BASE + '/api/v1/search?' + query

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = Request(url, headers={
        'X-Api-Key': TOKEN,
        'Accept': 'application/json',
        'User-Agent': 'STRMIndexer/0.8.5',
    })
    with build_opener(NoRedirect).open(request, timeout=15) as response:
        data = response.read(800001)
        if len(data) > 800000:
            raise ValueError('Resposta Seerr muito grande')
    payload = json.loads(data)
    desired = 'movie' if kind == 'movie' else 'tv'
    items = []
    for entry in payload.get('results', [])[:20]:
        if not isinstance(entry, dict) or entry.get('mediaType') != desired:
            continue
        ident = entry.get('id')
        if type(ident) is not int or ident < 1:
            continue
        label = entry.get('title' if desired == 'movie' else 'name') or ''
        alternate = entry.get('originalTitle' if desired == 'movie' else 'originalName') or ''
        if not isinstance(label, str) or not label.strip():
            continue
        date = entry.get('releaseDate' if desired == 'movie' else 'firstAirDate') or ''
        year = int(date[:4]) if isinstance(date, str) and re.fullmatch(r'\d{4}', date[:4]) else None
        poster_path = entry.get('posterPath')
        # Only allow a TMDB relative path; never fetch user supplied external URLs.
        poster = ('https://image.tmdb.org/t/p/w500' + poster_path
                  if isinstance(poster_path, str)
                  and re.fullmatch(r'/[A-Za-z0-9_./-]{1,180}\.(?:jpg|jpeg|png|webp)', poster_path)
                  and '..' not in poster_path else None)
        items.append({'id': ident, 'title': label.strip(),
                      'original_title': alternate if isinstance(alternate, str) else '',
                      'year': year, 'overview': str(entry.get('overview') or '')[:2000],
                      'poster': poster, 'rating': entry.get('voteAverage'), 'votes': entry.get('voteCount')})
    return {'items': items}
