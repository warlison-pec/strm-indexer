"""TVmaze public series search; one request gives poster, overview, and air year.
TVmaze content CC BY-SA: show provenance/attribution in the catalog.
"""
import html
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = 'https://api.tvmaze.com/'

def configured():
    return True

def search(title, kind='episode'):
    if kind != 'episode' or not isinstance(title, str) or not 2 <= len(title.strip()) <= 150:
        return {'items': []}
    url = BASE + 'search/shows?' + urlencode({'q': title.strip()})
    with urlopen(Request(url, headers={'Accept': 'application/json',
                                     'User-Agent': 'STRMIndexer/0.8.3 (TVmaze metadata)'}), timeout=12) as r:
        raw = r.read(700001)
        if len(raw) > 700000:
            raise ValueError('Resposta TVmaze muito grande')
    results = json.loads(raw)
    items = []
    for result in results[:12]:
        show = result.get('show') or {}
        name = show.get('name') or ''
        date = show.get('premiered') or ''
        year = int(date[:4]) if re.fullmatch(r'\d{4}', date[:4]) else None
        image = show.get('image') or {}
        poster = image.get('medium') or image.get('original')
        if not isinstance(poster, str) or not poster.startswith('https://static.tvmaze.com/'):
            poster = None
        overview = re.sub(r'<[^>]*>', ' ', str(show.get('summary') or ''))
        overview = ' '.join(html.unescape(overview).split())[:2000]
        if show.get('id') and name:
            items.append({'id': show['id'], 'title': name, 'original_title': '',
                          'year': year, 'overview': overview, 'poster': poster,
                          'rating': (show.get('rating') or {}).get('average'), 'votes': None})
    return {'items': items}
