"""Model Context Protocol at /mcp: JSON-RPC 2.0 over Streamable HTTP.

Modern era (revision 2026-07-28): server/discover answers identity,
capabilities and supportedVersions in one request; the legacy
initialize handshake is kept as a fallback. Six tools, all backed by
the same store the REST and git surfaces share.
"""

import json

from rest import (
    _get_issue_or_404, _get_pull_or_404, _get_repo_or_404, _merge_conflict,
    issue_view, pull_view, repo_view,
)
from store import FIXED_ISO, server_commit
from util import HttpError, Response, error_response, is_authorized, json_response

SERVER_NAME = 'github-core-clone-forge'
SERVER_VERSION = '1.0.0'
SUPPORTED_VERSIONS = ['2026-07-28', '2025-11-25', '2025-06-18',
                      '2025-03-26', '2024-11-05']

TOOLS = [
    {
        'name': 'create_repo',
        'description': 'Create a repository owned by the authenticated user.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string',
                         'description': 'The repository name.'},
                'description': {'type': 'string',
                                'description': 'A short description.'},
                'private': {'type': 'boolean',
                            'description': 'Whether the repository is private.'},
            },
            'required': ['name'],
        },
    },
    {
        'name': 'get_repo',
        'description': 'Read one repository by owner and name.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string',
                          'description': 'The repository owner login.'},
                'repo': {'type': 'string',
                         'description': 'The repository name.'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_issue',
        'description': 'Create an issue in a repository.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string',
                          'description': 'The repository owner login.'},
                'repo': {'type': 'string',
                         'description': 'The repository name.'},
                'title': {'type': 'string',
                          'description': 'The issue title.'},
                'body': {'type': 'string',
                         'description': 'The issue body.'},
            },
            'required': ['owner', 'repo', 'title'],
        },
    },
    {
        'name': 'list_issues',
        'description': 'List the issues of a repository.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string',
                          'description': 'The repository owner login.'},
                'repo': {'type': 'string',
                         'description': 'The repository name.'},
                'state': {'type': 'string',
                          'description': 'open, closed or all.',
                          'enum': ['open', 'closed', 'all']},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_pull_request',
        'description': 'Open a pull request from a head branch into a base branch.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string',
                          'description': 'The repository owner login.'},
                'repo': {'type': 'string',
                         'description': 'The repository name.'},
                'title': {'type': 'string',
                          'description': 'The pull request title.'},
                'head': {'type': 'string',
                         'description': 'The head branch name.'},
                'base': {'type': 'string',
                         'description': 'The base branch name.'},
                'body': {'type': 'string',
                         'description': 'The pull request body.'},
            },
            'required': ['owner', 'repo', 'title', 'head', 'base'],
        },
    },
    {
        'name': 'merge_pull_request',
        'description': 'Merge a pull request with a squash commit.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string',
                          'description': 'The repository owner login.'},
                'repo': {'type': 'string',
                         'description': 'The repository name.'},
                'pull_number': {'type': 'integer',
                                'description': 'The pull request number.'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
    },
]

_TOOL_BY_NAME = {tool['name']: tool for tool in TOOLS}


def _server_info():
    return {'name': SERVER_NAME, 'version': SERVER_VERSION}


def _tool_result(payload):
    text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return {'content': [{'type': 'text', 'text': text}],
            'structuredContent': payload}


def _tool_error(message):
    return {'content': [{'type': 'text', 'text': str(message)}],
            'isError': True}


def _missing(args, required):
    return [name for name in required
            if name not in args or args[name] in (None, '')]


def _call_tool(store, req, name, arguments):
    """Run one tool. Returns a tool-result dict (isError on failure)."""
    tool = _TOOL_BY_NAME.get(name)
    if tool is None:
        return None  # unknown tool: handled by the caller as -32602
    args = arguments if isinstance(arguments, dict) else {}
    required = tool['inputSchema'].get('required', [])
    missing = _missing(args, required)
    if missing:
        return _tool_error('missing required argument(s): %s'
                           % ', '.join(missing))
    try:
        if name == 'create_repo':
            return _create_repo(store, req, args)
        if name == 'get_repo':
            return _get_repo(store, req, args)
        if name == 'create_issue':
            return _create_issue(store, req, args)
        if name == 'list_issues':
            return _list_issues(store, req, args)
        if name == 'create_pull_request':
            return _create_pull_request(store, req, args)
        if name == 'merge_pull_request':
            return _merge_pull_request(store, req, args)
    except HttpError as exc:
        return _tool_error(exc.message)
    except Exception as exc:
        return _tool_error('internal error: %s' % exc)
    return _tool_error('unknown tool %s' % name)


def _create_repo(store, req, args):
    owner = store.viewer_login()
    name = str(args['name'])
    if store.get_repo(owner, name) is not None:
        return _tool_error('repository %s already exists' % name)
    repo = store.create_repo(owner, name,
                             description=args.get('description') or '',
                             private=bool(args.get('private', False)))
    return _tool_result(repo_view(store, repo, req))


def _get_repo(store, req, args):
    repo = _get_repo_or_404(store, args['owner'], args['repo'])
    return _tool_result(repo_view(store, repo, req))


def _create_issue(store, req, args):
    repo = _get_repo_or_404(store, args['owner'], args['repo'])
    issue = repo.new_issue(str(args['title']), args.get('body') or '', [])
    return _tool_result(issue_view(store, repo, issue, req))


def _list_issues(store, req, args):
    repo = _get_repo_or_404(store, args['owner'], args['repo'])
    state = str(args.get('state') or 'open')
    issues = [i for i in repo.issues.values()
              if state == 'all' or i.state == state]
    issues.sort(key=lambda i: i.number, reverse=True)
    return _tool_result({
        'issues': [issue_view(store, repo, i, req) for i in issues],
    })


def _create_pull_request(store, req, args):
    repo = _get_repo_or_404(store, args['owner'], args['repo'])
    head = str(args['head'])
    base = str(args['base'])
    head_tip = repo.branch_tip(head)
    base_tip = repo.branch_tip(base)
    if head_tip is None:
        return _tool_error('head branch %s does not exist' % head)
    if base_tip is None:
        return _tool_error('base branch %s does not exist' % base)
    for pull in repo.pulls.values():
        if pull.state == 'open' and pull.head == head:
            return _tool_result(pull_view(store, repo, pull, req))
    pull = repo.new_pull(str(args['title']), args.get('body') or '',
                         head, base, head_tip, base_tip)
    return _tool_result(pull_view(store, repo, pull, req))


def _merge_pull_request(store, req, args):
    from rest import _commit_info
    repo = _get_repo_or_404(store, args['owner'], args['repo'])
    pull = _get_pull_or_404(repo, args['pull_number'])
    if pull.state != 'open':
        return _tool_error('pull request %d is not open' % pull.number)
    if _merge_conflict(store, repo, pull):
        return _tool_error('merge conflict')
    head_tip = repo.branch_tip(pull.head)
    base_tip = repo.branch_tip(pull.base)
    if head_tip is None or base_tip is None:
        return _tool_error('pull request branch not found')
    head_tree = _commit_info(repo, head_tip)['tree']
    message = 'Merge pull request #%d: %s' % (pull.number, pull.title)
    commit_oid = server_commit(store, repo, head_tree, [base_tip], message)
    repo.refs['refs/heads/' + pull.base] = commit_oid
    pull.state = 'closed'
    pull.merged = True
    pull.merged_at = FIXED_ISO
    pull.merge_commit_sha = commit_oid
    repo.bump()
    return _tool_result({
        'number': pull.number,
        'merged': True,
        'state': 'closed',
        'sha': commit_oid,
        'message': message,
    })


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _rpc_result(request_id, result):
    return json_response(200, {'jsonrpc': '2.0', 'id': request_id,
                               'result': result})


def _rpc_error(request_id, code, message, status=200):
    return json_response(status, {'jsonrpc': '2.0', 'id': request_id,
                                  'error': {'code': code,
                                            'message': message}})


def dispatch(store, req):
    if req.method == 'GET':
        return Response(405, b'')
    if req.method == 'DELETE':
        return Response(200, b'')
    if req.method != 'POST':
        return Response(405, b'')
    try:
        body = req.json()
    except (ValueError, UnicodeDecodeError):
        return _rpc_error(None, -32700, 'Parse error', status=400)
    request_id = body.get('id') if isinstance(body, dict) else None
    if not is_authorized(req):
        return _rpc_error(request_id, -32001, 'Unauthorized', status=401)
    if not isinstance(body, dict) or 'method' not in body:
        return _rpc_error(request_id, -32600, 'Invalid Request')
    method = body['method']
    params = body.get('params') or {}
    if not isinstance(params, dict):
        params = {}

    # A notification (no id) gets no JSON-RPC body back.
    is_notification = 'id' not in body

    if method == 'notifications/initialized':
        return Response(202, b'')
    if method == 'ping':
        if is_notification:
            return Response(202, b'')
        return _rpc_result(request_id, {})
    if method == 'server/discover':
        return _rpc_result(request_id, {
            'supportedVersions': list(SUPPORTED_VERSIONS),
            'capabilities': {'tools': {}},
            'serverInfo': _server_info(),
            '_meta': {'io.modelcontextprotocol/serverInfo': _server_info()},
        })
    if method == 'initialize':
        offered = params.get('protocolVersion')
        version = offered if offered in SUPPORTED_VERSIONS \
            else SUPPORTED_VERSIONS[0]
        return _rpc_result(request_id, {
            'protocolVersion': version,
            'capabilities': {'tools': {}},
            'serverInfo': _server_info(),
        })
    if method == 'tools/list':
        return _rpc_result(request_id, {'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        result = _call_tool(store, req, name, arguments)
        if result is None:
            return _rpc_error(request_id, -32602,
                              'unknown tool %s' % name)
        return _rpc_result(request_id, result)
    return _rpc_error(request_id, -32601, 'method not found: %s' % method)
