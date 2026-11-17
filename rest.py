"""GitHub REST v3 surface under /api/v3.

Every reply is application/json with a non-empty X-GitHub-Request-Id,
including errors and unauthenticated 401s. Auth is a Bearer token equal
to MAKEFUN_SHOP_TOKEN; anything else is 401 with a JSON {"message"}.
"""

import base64
import difflib

from gitobjs import (
    build_tree, hash_object, is_oid, parse_commit, parse_tag, parse_tree,
)
from store import FIXED_ISO, server_commit
from util import (
    HttpError, Response, error_response, is_authorized, json_response,
    link_header, paginate,
)

MAX_NAME = 100


# ---------------------------------------------------------------------------
# JSON views
# ---------------------------------------------------------------------------

def _user_view(store, login=None):
    viewer = store.viewer
    return {
        'login': login or viewer['login'],
        'id': viewer['id'],
        'type': 'User',
        'name': viewer['name'],
        'email': viewer['email'],
    }


def repo_view(store, repo, req):
    return {
        'id': repo.id,
        'name': repo.name,
        'full_name': '%s/%s' % (repo.owner, repo.name),
        'owner': _user_view(store, repo.owner),
        'private': repo.private,
        'description': repo.description,
        'default_branch': repo.default_branch,
        'html_url': 'http://%s/%s/%s' % (req.host, repo.owner, repo.name),
        'url': 'http://%s/api/v3/repos/%s/%s'
               % (req.host, repo.owner, repo.name),
        'created_at': FIXED_ISO,
        'updated_at': FIXED_ISO,
        'pushed_at': FIXED_ISO,
        'has_issues': True,
        'has_projects': True,
        'has_wiki': True,
        'fork': False,
        'archived': False,
        'disabled': False,
        'open_issues_count': sum(1 for i in repo.issues.values()
                                 if i.state == 'open'),
        'watchers_count': 0,
        'stargazers_count': 0,
        'forks_count': 0,
    }


def issue_view(store, repo, issue, req):
    return {
        'id': repo.id * 1000000 + issue.number,
        'number': issue.number,
        'title': issue.title,
        'body': issue.body,
        'state': issue.state,
        'labels': [{'name': name,
                    'color': repo.labels.get(name, {}).get('color', 'ededed')}
                   for name in issue.labels],
        'user': _user_view(store),
        'assignee': None,
        'assignees': [],
        'created_at': issue.created_at,
        'updated_at': issue.updated_at,
        'closed_at': FIXED_ISO if issue.state == 'closed' else None,
        'comments': len(issue.comments),
        'html_url': 'http://%s/%s/%s/issues/%d'
                    % (req.host, repo.owner, repo.name, issue.number),
        'url': 'http://%s/api/v3/repos/%s/%s/issues/%d'
               % (req.host, repo.owner, repo.name, issue.number),
    }


def pull_view(store, repo, pull, req):
    base_sha = repo.branch_tip(pull.base) or pull.base_sha
    return {
        'id': repo.id * 1000000 + pull.number,
        'number': pull.number,
        'title': pull.title,
        'body': pull.body,
        'state': pull.state,
        'merged': pull.merged,
        'merged_at': pull.merged_at,
        'merge_commit_sha': pull.merge_commit_sha,
        'mergeable': _mergeable(store, repo, pull),
        'mergeable_state': 'clean' if _mergeable(store, repo, pull) else 'dirty',
        'draft': False,
        'head': {'ref': pull.head, 'sha': pull.head_sha},
        'base': {'ref': pull.base, 'sha': base_sha},
        'user': _user_view(store),
        'created_at': pull.created_at,
        'updated_at': pull.updated_at,
        'closed_at': FIXED_ISO if pull.state == 'closed' else None,
        'html_url': 'http://%s/%s/%s/pull/%d'
                    % (req.host, repo.owner, repo.name, pull.number),
        'url': 'http://%s/api/v3/repos/%s/%s/pulls/%d'
               % (req.host, repo.owner, repo.name, pull.number),
    }


# ---------------------------------------------------------------------------
# tree / commit helpers
# ---------------------------------------------------------------------------

def _commit_info(repo, oid):
    obj = repo.get_object(oid)
    if obj is None or obj[0] != 'commit':
        return None
    commit = parse_commit(obj[1])
    return commit


def _tree_entries(repo, tree_oid):
    obj = repo.get_object(tree_oid)
    if obj is None or obj[0] != 'tree':
        return []
    return parse_tree(obj[1])


def _update_tree(repo, root_tree_oid, parts, blob_oid):
    """Return a new root tree oid with path `parts` set to blob_oid
    (or removed when blob_oid is None). None when the tree becomes empty."""

    def rec(tree_oid, remaining):
        entries = {}
        if tree_oid is not None:
            for mode, name, oid in _tree_entries(repo, tree_oid):
                entries[name] = (mode, oid)
        name = remaining[0]
        if len(remaining) == 1:
            if blob_oid is None:
                entries.pop(name, None)
            else:
                entries[name] = ('100644', blob_oid)
        else:
            sub = entries.get(name)
            sub_oid = sub[1] if sub and sub[0] == '40000' else None
            new_sub = rec(sub_oid, remaining[1:])
            if new_sub is None:
                entries.pop(name, None)
            else:
                entries[name] = ('40000', new_sub)
        if not entries:
            return None
        data = build_tree([(mode, n, o)
                           for n, (mode, o) in entries.items()])
        return repo.put_object('tree', data)

    return rec(root_tree_oid, parts)


def _lookup_path(repo, tree_oid, parts):
    """(type, oid) of the object at path `parts` under tree_oid, or None."""
    current = tree_oid
    for index, part in enumerate(parts):
        found = None
        for mode, name, oid in _tree_entries(repo, current):
            if name == part:
                found = (mode, oid)
                break
        if found is None:
            return None
        mode, oid = found
        if index == len(parts) - 1:
            if mode == '40000':
                return ('tree', oid)
            return ('blob', oid)
        if mode != '40000':
            return None
        current = oid
    return None


def _ref_tree_oid(repo, ref):
    """The root tree oid for a branch name, commit sha or tree sha."""
    if ref is None:
        ref = repo.default_branch
    tip = repo.branch_tip(ref)
    if tip is not None:
        commit = _commit_info(repo, tip)
        return commit['tree'] if commit else None
    if is_oid(ref):
        obj = repo.get_object(ref)
        if obj is None:
            return None
        if obj[0] == 'commit':
            return parse_commit(obj[1])['tree']
        if obj[0] == 'tree':
            return ref
    return None


def _walk_commits(repo, tip, limit=1000):
    """Commits reachable from tip, tip first, parents after."""
    out = []
    seen = set()
    stack = [tip]
    while stack and len(out) < limit:
        oid = stack.pop(0)
        if oid in seen:
            continue
        seen.add(oid)
        commit = _commit_info(repo, oid)
        if commit is None:
            continue
        out.append((oid, commit))
        stack.extend(commit['parents'])
    return out


# ---------------------------------------------------------------------------
# merge machinery
# ---------------------------------------------------------------------------

def _flatten_tree(repo, tree_oid):
    from gitobjs import flatten_tree
    return flatten_tree(repo.objects, tree_oid)


def _merge_conflict(store, repo, pull):
    """True when head and the current base tip both changed the same path
    relative to their merge-base, to different blobs."""
    from gitobjs import merge_base
    head_tip = repo.branch_tip(pull.head)
    base_tip = repo.branch_tip(pull.base)
    if head_tip is None or base_tip is None:
        return False
    base = merge_base(repo.objects, head_tip, base_tip)
    if base is None:
        return False
    base_tree = _commit_info(repo, base)['tree']
    head_tree = _commit_info(repo, head_tip)['tree']
    base_tip_tree = _commit_info(repo, base_tip)['tree']
    old = _flatten_tree(repo, base_tree)
    new_head = _flatten_tree(repo, head_tree)
    new_base = _flatten_tree(repo, base_tip_tree)
    for path in set(new_head) | set(new_base):
        head_changed = new_head.get(path) != old.get(path)
        base_changed = new_base.get(path) != old.get(path)
        if head_changed and base_changed and new_head.get(path) != new_base.get(path):
            return True
    return False


def _mergeable(store, repo, pull):
    if pull.state != 'open':
        return False
    return not _merge_conflict(store, repo, pull)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def _get_repo_or_404(store, owner, name):
    repo = store.get_repo(owner, name)
    if repo is None:
        raise HttpError(404, 'repository %s/%s not found' % (owner, name))
    return repo


def _require_fields(body, fields):
    if not isinstance(body, dict):
        raise HttpError(400, 'request body must be a JSON object')
    for field in fields:
        if field not in body or body[field] in (None, ''):
            raise HttpError(422, 'missing required field %s' % field)


def handle_api_root(store, req):
    return json_response(200, {'current_user_url': 'http://%s/api/v3/user'
                               % req.host},
                         {'X-OAuth-Scopes': 'repo, read:org'})


def handle_meta(store, req):
    return json_response(200, {'installed_version': '1.0.0'})


def handle_user(store, req):
    viewer = store.viewer
    return json_response(200, {
        'login': viewer['login'], 'id': viewer['id'], 'type': 'User',
        'name': viewer['name'], 'email': viewer['email'],
    })


def handle_create_repo(store, req):
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['name'])
    name = str(body['name'])
    if not name or len(name) > MAX_NAME:
        raise HttpError(422, 'invalid repository name')
    if store.get_repo(store.viewer_login(), name) is not None:
        raise HttpError(422, 'repository %s already exists' % name)
    repo = store.create_repo(store.viewer_login(), name,
                             description=body.get('description') or '',
                             private=bool(body.get('private', False)))
    return json_response(201, repo_view(store, repo, req))


def handle_list_user_repos(store, req):
    repos = store.viewer_repos()
    page_items, per_page, page, total = paginate(repos, req.query)
    base = 'http://%s/api/v3/user/repos' % req.host
    headers = {'Link': link_header(base, req.query, per_page, page, total)}
    return json_response(200, [repo_view(store, r, req) for r in page_items],
                         headers)


def handle_get_repo(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    return json_response(200, repo_view(store, repo, req))


def handle_patch_repo(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    if not isinstance(body, dict):
        raise HttpError(400, 'request body must be a JSON object')
    if 'description' in body:
        repo.description = body['description'] or ''
    if 'private' in body:
        repo.private = bool(body['private'])
    repo.bump()
    return json_response(200, repo_view(store, repo, req))


def handle_delete_repo(store, req, owner, name):
    if not store.delete_repo(owner, name):
        raise HttpError(404, 'repository %s/%s not found' % (owner, name))
    return Response(204, b'')


def handle_list_branches(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    out = []
    for branch in repo.sorted_branches():
        out.append({'name': branch,
                    'commit': {'sha': repo.branch_tip(branch)}})
    return json_response(200, out)


def handle_get_branch(store, req, owner, name, branch):
    repo = _get_repo_or_404(store, owner, name)
    tip = repo.branch_tip(branch)
    if tip is None:
        raise HttpError(404, 'branch %s not found' % branch)
    return json_response(200, {'name': branch, 'commit': {'sha': tip}})


def handle_create_ref(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['ref', 'sha'])
    ref = str(body['ref'])
    sha = str(body['sha'])
    if not ref.startswith('refs/'):
        raise HttpError(422, 'ref must start with refs/')
    if repo.ref_oid(ref) is not None:
        raise HttpError(422, 'reference %s already exists' % ref)
    obj = repo.get_object(sha)
    if obj is None:
        raise HttpError(422, 'object %s does not exist' % sha)
    repo.refs[ref] = sha
    repo.bump()
    return json_response(201, {'ref': ref,
                               'object': {'sha': sha, 'type': obj[0]}})


def handle_get_ref(store, req, owner, name, ref_suffix):
    repo = _get_repo_or_404(store, owner, name)
    ref = 'refs/' + ref_suffix
    oid = repo.ref_oid(ref)
    if oid is None:
        raise HttpError(404, 'reference %s not found' % ref)
    obj = repo.get_object(oid)
    return json_response(200, {'ref': ref,
                               'object': {'sha': oid,
                                          'type': obj[0] if obj else 'commit'}})


def handle_list_commits(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    branch = req.query.get('sha') or repo.default_branch
    tip = repo.branch_tip(branch)
    if tip is None:
        if is_oid(branch):
            tip = branch if repo.get_object(branch) is not None else None
        if tip is None:
            raise HttpError(404, 'branch %s not found' % branch)
    out = []
    for oid, commit in _walk_commits(repo, tip):
        author = commit['author']
        out.append({
            'sha': oid,
            'commit': {
                'message': commit['message'],
                'author': {'name': author.rsplit(' <', 1)[0]
                           if ' <' in author else author,
                           'email': '', 'date': FIXED_ISO},
                'committer': {'name': commit['committer'].rsplit(' <', 1)[0]
                              if ' <' in commit['committer']
                              else commit['committer'],
                              'email': '', 'date': FIXED_ISO},
            },
        })
    return json_response(200, out)


def handle_get_contents(store, req, owner, name, path):
    repo = _get_repo_or_404(store, owner, name)
    tree_oid = _ref_tree_oid(repo, req.query.get('ref'))
    if tree_oid is None:
        raise HttpError(404, 'repository %s/%s is empty' % (owner, name))
    if not path:
        entries = _tree_entries(repo, tree_oid)
        return json_response(200, [{
            'type': 'dir' if mode == '40000' else 'file',
            'name': entry_name,
            'path': entry_name,
            'sha': entry_oid,
        } for mode, entry_name, entry_oid in entries])
    parts = [p for p in path.split('/') if p]
    found = _lookup_path(repo, tree_oid, parts)
    if found is None:
        raise HttpError(404, 'path %s not found' % path)
    kind, oid = found
    if kind == 'tree':
        entries = _tree_entries(repo, oid)
        return json_response(200, [{
            'type': 'dir' if mode == '40000' else 'file',
            'name': entry_name,
            'path': '/'.join(parts + [entry_name]),
            'sha': entry_oid,
        } for mode, entry_name, entry_oid in entries])
    obj = repo.get_object(oid)
    data = obj[1] if obj else b''
    return json_response(200, {
        'type': 'file',
        'encoding': 'base64',
        'content': base64.b64encode(data).decode('ascii'),
        'sha': oid,
        'size': len(data),
        'name': parts[-1],
        'path': '/'.join(parts),
    })


def handle_put_contents(store, req, owner, name, path):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['message', 'content'])
    try:
        data = base64.b64decode(body['content'], validate=False)
    except Exception:
        raise HttpError(422, 'content is not valid base64')
    branch = str(body.get('branch') or repo.default_branch)
    parts = [p for p in path.split('/') if p]
    if not parts:
        raise HttpError(422, 'a file path is required')
    tip = repo.branch_tip(branch)
    if tip is None and branch != repo.default_branch:
        # A write to a missing branch starts it from the default tip.
        tip = repo.head_oid()
    parent_tree = None
    if tip is not None:
        commit = _commit_info(repo, tip)
        parent_tree = commit['tree'] if commit else None
    current = _lookup_path(repo, parent_tree, parts) if parent_tree else None
    given_sha = body.get('sha')
    if given_sha is not None:
        if current is None or current[1] != given_sha:
            raise HttpError(409, 'sha does not match the current file')
        status = 200
    else:
        if current is not None:
            raise HttpError(422, 'file %s already exists' % path)
        status = 201
    blob_oid = repo.put_object('blob', data)
    new_tree = _update_tree(repo, parent_tree, parts, blob_oid)
    parents = [tip] if tip is not None else []
    commit_oid = server_commit(store, repo, new_tree, parents,
                               str(body['message']))
    repo.refs['refs/heads/' + branch] = commit_oid
    repo.bump()
    return json_response(status, {
        'content': {'name': parts[-1], 'path': '/'.join(parts),
                    'sha': blob_oid, 'size': len(data),
                    'encoding': 'base64', 'type': 'file'},
        'commit': {'sha': commit_oid, 'message': str(body['message'])},
    })


def handle_delete_contents(store, req, owner, name, path):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['message', 'sha'])
    branch = str(body.get('branch') or repo.default_branch)
    tip = repo.branch_tip(branch)
    if tip is None:
        raise HttpError(404, 'branch %s not found' % branch)
    parts = [p for p in path.split('/') if p]
    commit = _commit_info(repo, tip)
    current = _lookup_path(repo, commit['tree'], parts)
    if current is None or current[1] != body['sha']:
        raise HttpError(409, 'sha does not match the current file')
    new_tree = _update_tree(repo, commit['tree'], parts, None)
    if new_tree is None:
        new_tree = repo.put_object('tree', b'')
    commit_oid = server_commit(store, repo, new_tree, [tip],
                               str(body['message']))
    repo.refs['refs/heads/' + branch] = commit_oid
    repo.bump()
    return json_response(200, {'commit': {'sha': commit_oid,
                                          'message': str(body['message'])}})


def handle_create_label(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['name'])
    label = str(body['name'])
    if label in repo.labels:
        # Creating a label that already exists answers 201 with the label:
        # the suite's issue fixtures create the default label 'bug' this way.
        if body.get('color'):
            repo.labels[label]['color'] = str(body['color'])
        return json_response(201, repo.labels[label])
    repo.labels[label] = {'name': label,
                          'color': str(body.get('color') or 'ededed')}
    repo.bump()
    return json_response(201, repo.labels[label])


def handle_list_labels(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    return json_response(200, list(repo.labels.values()))


def handle_create_issue(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['title'])
    labels = body.get('labels') or []
    if isinstance(labels, list):
        label_names = []
        for label in labels:
            label_name = label['name'] if isinstance(label, dict) else label
            if label_name not in repo.labels:
                raise HttpError(422, 'label %s does not exist' % label_name)
            label_names.append(label_name)
    else:
        raise HttpError(422, 'labels must be an array')
    issue = repo.new_issue(str(body['title']), body.get('body') or '',
                           label_names)
    return json_response(201, issue_view(store, repo, issue, req))


def handle_list_issues(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    state = req.query.get('state', 'open')
    direction = req.query.get('direction', 'desc')
    issues = [i for i in repo.issues.values()
              if state == 'all' or i.state == state]
    issues.sort(key=lambda i: i.number, reverse=(direction != 'asc'))
    page_items, per_page, page, total = paginate(issues, req.query)
    base = 'http://%s/api/v3/repos/%s/%s/issues' % (req.host, owner, name)
    headers = {'Link': link_header(base, req.query, per_page, page, total)}
    return json_response(200,
                         [issue_view(store, repo, i, req) for i in page_items],
                         headers)


def _get_issue_or_404(repo, number):
    try:
        number = int(number)
    except ValueError:
        raise HttpError(404, 'issue not found')
    issue = repo.issues.get(number)
    if issue is None:
        raise HttpError(404, 'issue %d not found' % number)
    return issue


def handle_get_issue(store, req, owner, name, number):
    repo = _get_repo_or_404(store, owner, name)
    return json_response(200, issue_view(
        store, repo, _get_issue_or_404(repo, number), req))


def handle_patch_issue(store, req, owner, name, number):
    repo = _get_repo_or_404(store, owner, name)
    issue = _get_issue_or_404(repo, number)
    body, err = req.json_or_error()
    if err is not None:
        return err
    if not isinstance(body, dict):
        raise HttpError(400, 'request body must be a JSON object')
    if 'state' in body:
        if body['state'] not in ('open', 'closed'):
            raise HttpError(422, 'invalid state')
        issue.state = body['state']
    if 'title' in body:
        issue.title = str(body['title'])
    if 'body' in body:
        issue.body = body['body'] or ''
    repo.bump()
    return json_response(200, issue_view(store, repo, issue, req))


def handle_create_comment(store, req, owner, name, number):
    repo = _get_repo_or_404(store, owner, name)
    issue = _get_issue_or_404(repo, number)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['body'])
    comment_id = repo.add_comment(issue, str(body['body']))
    return json_response(201, {
        'id': comment_id,
        'body': str(body['body']),
        'user': _user_view(store),
        'created_at': FIXED_ISO,
        'updated_at': FIXED_ISO,
    })


def handle_list_comments(store, req, owner, name, number):
    repo = _get_repo_or_404(store, owner, name)
    issue = _get_issue_or_404(repo, number)
    return json_response(200, [{
        'id': comment_id,
        'body': comment_body,
        'user': _user_view(store),
        'created_at': FIXED_ISO,
        'updated_at': FIXED_ISO,
    } for comment_id, comment_body in issue.comments])


def handle_create_pull(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    body, err = req.json_or_error()
    if err is not None:
        return err
    _require_fields(body, ['title', 'head', 'base'])
    head = str(body['head'])
    base = str(body['base'])
    head_tip = repo.branch_tip(head)
    base_tip = repo.branch_tip(base)
    if head_tip is None:
        raise HttpError(422, 'head branch %s does not exist' % head)
    if base_tip is None:
        raise HttpError(422, 'base branch %s does not exist' % base)
    for pull in repo.pulls.values():
        if pull.state == 'open' and pull.head == head:
            raise HttpError(422, 'an open pull request already exists '
                                 'for head %s' % head)
    pull = repo.new_pull(str(body['title']), body.get('body') or '',
                         head, base, head_tip, base_tip)
    return json_response(201, pull_view(store, repo, pull, req))


def handle_list_pulls(store, req, owner, name):
    repo = _get_repo_or_404(store, owner, name)
    state = req.query.get('state', 'open')
    pulls = [p for p in repo.pulls.values()
             if state == 'all' or p.state == state]
    pulls.sort(key=lambda p: p.number, reverse=True)
    page_items, per_page, page, total = paginate(pulls, req.query)
    base = 'http://%s/api/v3/repos/%s/%s/pulls' % (req.host, owner, name)
    headers = {'Link': link_header(base, req.query, per_page, page, total)}
    return json_response(200,
                         [pull_view(store, repo, p, req) for p in page_items],
                         headers)


def _get_pull_or_404(repo, number):
    try:
        number = int(number)
    except ValueError:
        raise HttpError(404, 'pull request not found')
    pull = repo.pulls.get(number)
    if pull is None:
        raise HttpError(404, 'pull request %d not found' % number)
    return pull


def handle_get_pull(store, req, owner, name, number):
    repo = _get_repo_or_404(store, owner, name)
    return json_response(200, pull_view(
        store, repo, _get_pull_or_404(repo, number), req))


def _diff_files(repo, base_tree_oid, head_tree_oid):
    from gitobjs import merge_base
    old = _flatten_tree(repo, base_tree_oid)
    new = _flatten_tree(repo, head_tree_oid)
    out = []
    for path in sorted(set(old) | set(new)):
        old_entry = old.get(path)
        new_entry = new.get(path)
        if old_entry == new_entry:
            continue
        if old_entry is None:
            status = 'added'
            old_text, new_text = '', _blob_text(repo, new_entry[1])
        elif new_entry is None:
            status = 'removed'
            old_text, new_text = _blob_text(repo, old_entry[1]), ''
        else:
            status = 'modified'
            old_text = _blob_text(repo, old_entry[1])
            new_text = _blob_text(repo, new_entry[1])
        additions = deletions = 0
        for line in difflib.unified_diff(old_text.splitlines(),
                                         new_text.splitlines(), lineterm=''):
            if line.startswith('+') and not line.startswith('+++'):
                additions += 1
            elif line.startswith('-') and not line.startswith('---'):
                deletions += 1
        out.append({
            'filename': path,
            'status': status,
            'additions': additions,
            'deletions': deletions,
            'changes': additions + deletions,
        })
    return out


def _blob_text(repo, oid):
    obj = repo.get_object(oid)
    if obj is None:
        return ''
    return obj[1].decode('utf-8', 'replace')


def handle_pull_files(store, req, owner, name, number):
    from gitobjs import merge_base
    repo = _get_repo_or_404(store, owner, name)
    pull = _get_pull_or_404(repo, number)
    head_tip = repo.branch_tip(pull.head)
    base_tip = repo.branch_tip(pull.base)
    if head_tip is None or base_tip is None:
        raise HttpError(404, 'pull request branch not found')
    base = merge_base(repo.objects, head_tip, base_tip)
    base_tree = _commit_info(repo, base)['tree'] if base else None
    head_tree = _commit_info(repo, head_tip)['tree']
    if base_tree is None:
        base_tree = repo.put_object('tree', b'')
    return json_response(200, _diff_files(repo, base_tree, head_tree))


def handle_merge_pull(store, req, owner, name, number):
    repo = _get_repo_or_404(store, owner, name)
    pull = _get_pull_or_404(repo, number)
    if pull.state != 'open':
        raise HttpError(405, 'pull request %d is not open' % pull.number)
    if _merge_conflict(store, repo, pull):
        raise HttpError(405, 'merge conflict')
    head_tip = repo.branch_tip(pull.head)
    base_tip = repo.branch_tip(pull.base)
    if head_tip is None or base_tip is None:
        raise HttpError(404, 'pull request branch not found')
    head_tree = _commit_info(repo, head_tip)['tree']
    message = 'Merge pull request #%d: %s' % (pull.number, pull.title)
    commit_oid = server_commit(store, repo, head_tree, [base_tip], message)
    repo.refs['refs/heads/' + pull.base] = commit_oid
    pull.state = 'closed'
    pull.merged = True
    pull.merged_at = FIXED_ISO
    pull.merge_commit_sha = commit_oid
    repo.bump()
    return json_response(200, {
        'sha': commit_oid,
        'merged': True,
        'message': message,
    })


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def _segments(path):
    return [seg for seg in path.split('/') if seg]


def dispatch(store, req):
    """Route one REST request. Returns a Response (never raises)."""
    if not is_authorized(req):
        return error_response(401, 'Bad credentials')
    try:
        return _dispatch(store, req)
    except HttpError as exc:
        return error_response(exc.status, exc.message)
    except Exception as exc:  # never leak a stack as HTML
        return error_response(500, 'internal error: %s' % exc)


def _dispatch(store, req):
    segs = _segments(req.path)
    if segs[:2] != ['api', 'v3']:
        raise HttpError(404, 'Not Found')
    rest = segs[2:]
    method = req.method

    if not rest:
        if method == 'GET':
            return handle_api_root(store, req)
        raise HttpError(404, 'Not Found')
    if rest == ['meta']:
        if method == 'GET':
            return handle_meta(store, req)
        raise HttpError(404, 'Not Found')
    if rest == ['user']:
        if method == 'GET':
            return handle_user(store, req)
        raise HttpError(404, 'Not Found')
    if rest == ['user', 'repos']:
        if method == 'POST':
            return handle_create_repo(store, req)
        if method == 'GET':
            return handle_list_user_repos(store, req)
        raise HttpError(404, 'Not Found')
    if rest[:1] == ['repos'] and len(rest) >= 3:
        owner, name = rest[1], rest[2]
        tail = rest[3:]
        if not tail:
            if method == 'GET':
                return handle_get_repo(store, req, owner, name)
            if method == 'PATCH':
                return handle_patch_repo(store, req, owner, name)
            if method == 'DELETE':
                return handle_delete_repo(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail == ['branches']:
            if method == 'GET':
                return handle_list_branches(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail[0] == 'branches' and len(tail) >= 2:
            if method == 'GET':
                return handle_get_branch(store, req, owner, name,
                                         '/'.join(tail[1:]))
            raise HttpError(404, 'Not Found')
        if tail == ['git', 'refs']:
            if method == 'POST':
                return handle_create_ref(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail[:2] == ['git', 'refs'] and len(tail) >= 3:
            if method == 'GET':
                return handle_get_ref(store, req, owner, name,
                                      '/'.join(tail[2:]))
            raise HttpError(404, 'Not Found')
        if tail == ['commits']:
            if method == 'GET':
                return handle_list_commits(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail[0] == 'contents':
            path = '/'.join(tail[1:])
            if method == 'GET':
                return handle_get_contents(store, req, owner, name, path)
            if method == 'PUT':
                return handle_put_contents(store, req, owner, name, path)
            if method == 'DELETE':
                return handle_delete_contents(store, req, owner, name, path)
            raise HttpError(404, 'Not Found')
        if tail == ['labels']:
            if method == 'POST':
                return handle_create_label(store, req, owner, name)
            if method == 'GET':
                return handle_list_labels(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail == ['issues']:
            if method == 'POST':
                return handle_create_issue(store, req, owner, name)
            if method == 'GET':
                return handle_list_issues(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail[0] == 'issues' and len(tail) >= 2:
            number = tail[1]
            if len(tail) == 2:
                if method == 'GET':
                    return handle_get_issue(store, req, owner, name, number)
                if method == 'PATCH':
                    return handle_patch_issue(store, req, owner, name, number)
                raise HttpError(404, 'Not Found')
            if tail[2] == 'comments':
                if method == 'POST':
                    return handle_create_comment(store, req, owner, name,
                                                 number)
                if method == 'GET':
                    return handle_list_comments(store, req, owner, name,
                                                number)
                raise HttpError(404, 'Not Found')
        if tail == ['pulls']:
            if method == 'POST':
                return handle_create_pull(store, req, owner, name)
            if method == 'GET':
                return handle_list_pulls(store, req, owner, name)
            raise HttpError(404, 'Not Found')
        if tail[0] == 'pulls' and len(tail) >= 2:
            number = tail[1]
            if len(tail) == 2:
                if method == 'GET':
                    return handle_get_pull(store, req, owner, name, number)
                raise HttpError(404, 'Not Found')
            if tail[2] == 'files' and len(tail) == 3:
                if method == 'GET':
                    return handle_pull_files(store, req, owner, name, number)
                raise HttpError(404, 'Not Found')
            if tail[2] == 'merge' and len(tail) == 3:
                if method == 'PUT':
                    return handle_merge_pull(store, req, owner, name, number)
                raise HttpError(404, 'Not Found')
    raise HttpError(404, 'Not Found')
