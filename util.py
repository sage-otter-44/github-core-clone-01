"""Shared HTTP plumbing: request/response types, JSON replies, auth."""

import itertools
import json
import os

_REQUEST_ID = itertools.count(1)


def new_request_id():
    return 'req-%d' % next(_REQUEST_ID)


class Request:
    """One HTTP request, transport-independent."""

    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query          # dict[str, str] (first value wins)
        self.headers = headers      # dict[str, str], keys lower-cased
        self.body = body            # bytes
        self._json = ...
        self._json_parsed = False

    @property
    def host(self):
        return self.headers.get('host', '127.0.0.1')

    def json(self):
        """The parsed JSON body, or None; raises ValueError on bad JSON."""
        if not self._json_parsed:
            self._json_parsed = True
            if not self.body:
                self._json = None
            else:
                self._json = json.loads(self.body.decode('utf-8'))
        return self._json

    def json_or_error(self):
        """(value, error_response_or_None). Malformed JSON -> 400."""
        try:
            return self.json(), None
        except (ValueError, UnicodeDecodeError):
            return None, error_response(400, 'malformed JSON body')


class Response:
    def __init__(self, status, body=b'', content_type='application/json',
                 headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = dict(headers or {})


def expected_token():
    return os.environ.get('MAKEFUN_SHOP_TOKEN') or 'test-token'


def bearer_token(req):
    auth = req.headers.get('authorization', '')
    if auth.startswith('Bearer '):
        return auth[len('Bearer '):]
    return None


def is_authorized(req):
    token = bearer_token(req)
    return token is not None and token == expected_token()


def json_response(status, obj, headers=None):
    body = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return Response(status, body, 'application/json', headers)


def error_response(status, message):
    return json_response(status, {'message': message or 'error'})


class HttpError(Exception):
    """Raise inside a handler to return an error response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_query(query_string):
    from urllib.parse import parse_qsl
    out = {}
    for key, value in parse_qsl(query_string, keep_blank_values=True):
        if key not in out:
            out[key] = value
    return out


def page_params(query, default_per_page=30):
    try:
        per_page = int(query.get('per_page', default_per_page))
    except ValueError:
        per_page = default_per_page
    try:
        page = int(query.get('page', 1))
    except ValueError:
        page = 1
    per_page = max(1, min(per_page, 100))
    page = max(1, page)
    return per_page, page


def paginate(items, query):
    """(page_items, link_header_or_None, total)."""
    per_page, page = page_params(query)
    total = len(items)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    return page_items, per_page, page, total


def link_header(base_url, query, per_page, page, total):
    """A Link header with rel=next/prev/first/last (absolute, same origin)."""
    from urllib.parse import urlencode
    links = []

    def url(p):
        params = dict(query)
        params['per_page'] = str(per_page)
        params['page'] = str(p)
        return '<%s?%s>' % (base_url, urlencode(params))

    last_page = max(1, (total + per_page - 1) // per_page)
    if page < last_page:
        links.append('%s; rel="next"' % url(page + 1))
    if page > 1:
        links.append('%s; rel="prev"' % url(page - 1))
    links.append('%s; rel="first"' % url(1))
    links.append('%s; rel="last"' % url(last_page))
    return ', '.join(links)
