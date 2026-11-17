"""GitHub GraphQL v4 at /api/graphql.

A small recursive-descent parser for the documents gh and
github-mcp-server actually send (operations, variables, aliases,
arguments, inline fragments, __type introspection, batching), plus an
executor over the same store the REST and git surfaces share.
"""

import base64
import json

from util import HttpError, error_response, is_authorized, json_response

# ---------------------------------------------------------------------------
# schema table (only what introspection and field validation need)
# ---------------------------------------------------------------------------

SCHEMA = {
    'Query': {
        'kind': 'OBJECT',
        'fields': ['viewer', 'repository', 'node', '__type', '__schema'],
    },
    'Mutation': {
        'kind': 'OBJECT',
        'fields': ['createIssue', 'closeIssue', 'createPullRequest',
                   'mergePullRequest', 'addComment'],
    },
    'User': {
        'kind': 'OBJECT',
        'fields': ['id', 'login', 'name', 'email'],
    },
    'Repository': {
        'kind': 'OBJECT',
        'fields': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner',
                   'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled',
                   'issue', 'issues', 'pullRequests', 'pullRequest',
                   'description', 'private', 'defaultBranch', 'createdAt',
                   'updatedAt'],
    },
    'Issue': {
        'kind': 'OBJECT',
        'fields': ['id', 'number', 'title', 'state', 'body', 'url',
                   'createdAt', 'updatedAt', 'author', 'labels', 'comments'],
    },
    'IssueConnection': {
        'kind': 'OBJECT',
        'fields': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    },
    'IssueEdge': {
        'kind': 'OBJECT',
        'fields': ['node', 'cursor'],
    },
    'PageInfo': {
        'kind': 'OBJECT',
        'fields': ['hasNextPage', 'hasPreviousPage', 'endCursor',
                   'startCursor'],
    },
    'PullRequest': {
        'kind': 'OBJECT',
        'fields': ['id', 'number', 'title', 'state', 'merged', 'headRefName',
                   'baseRefName', 'mergeable', 'files', 'body', 'url',
                   'createdAt', 'updatedAt', 'headRef', 'baseRef',
                   'mergeCommit'],
    },
    'PullRequestConnection': {
        'kind': 'OBJECT',
        'fields': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    },
    'PullRequestChangedFileConnection': {
        'kind': 'OBJECT',
        'fields': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    },
    'PullRequestChangedFile': {
        'kind': 'OBJECT',
        'fields': ['path', 'additions', 'deletions', 'changeType'],
    },
    'IssueComment': {
        'kind': 'OBJECT',
        'fields': ['id', 'body', 'author', 'createdAt', 'updatedAt'],
    },
    'IssueCommentEdge': {
        'kind': 'OBJECT',
        'fields': ['node', 'cursor'],
    },
    'Ref': {
        'kind': 'OBJECT',
        'fields': ['name', 'target'],
    },
    'SearchType': {
        'kind': 'ENUM',
        'enumValues': ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION'],
    },
    'IssueState': {
        'kind': 'ENUM',
        'enumValues': ['OPEN', 'CLOSED'],
    },
    'PullRequestState': {
        'kind': 'ENUM',
        'enumValues': ['OPEN', 'CLOSED', 'MERGED'],
    },
    'MergeableState': {
        'kind': 'ENUM',
        'enumValues': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'],
    },
    'RepositoryPermission': {
        'kind': 'ENUM',
        'enumValues': ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE', 'READ'],
    },
}

# field -> (object type it returns, for fragment matching / validation)
FIELD_TYPE = {
    ('Query', 'viewer'): 'User',
    ('Query', 'repository'): 'Repository',
    ('Query', 'node'): 'Node',
    ('Query', '__type'): '__Type',
    ('Repository', 'owner'): 'User',
    ('Repository', 'defaultBranchRef'): 'Ref',
    ('Repository', 'issue'): 'Issue',
    ('Repository', 'issues'): 'IssueConnection',
    ('Repository', 'pullRequests'): 'PullRequestConnection',
    ('Repository', 'pullRequest'): 'PullRequest',
    ('IssueConnection', 'nodes'): 'Issue',
    ('IssueConnection', 'pageInfo'): 'PageInfo',
    ('PullRequestConnection', 'nodes'): 'PullRequest',
    ('PullRequest', 'files'): 'PullRequestChangedFileConnection',
    ('PullRequestChangedFileConnection', 'nodes'): 'PullRequestChangedFile',
}


# ---------------------------------------------------------------------------
# lexer
# ---------------------------------------------------------------------------

class _Token:
    __slots__ = ('kind', 'value')

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return 'Token(%s, %r)' % (self.kind, self.value)


def _lex(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ' \t\r\n,':
            i += 1
            continue
        if ch == '#':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if ch in '{}()[]:!=$@':
            tokens.append(_Token(ch, ch))
            i += 1
            continue
        if ch == '.' and text[i:i + 3] == '...':
            tokens.append(_Token('...', '...'))
            i += 3
            continue
        if ch == '"':
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == '\\' and i + 1 < n:
                    nxt = text[i + 1]
                    mapping = {'n': '\n', 't': '\t', 'r': '\r', '"': '"',
                               '\\': '\\', '/': '/'}
                    out.append(mapping.get(nxt, nxt))
                    i += 2
                else:
                    out.append(text[i])
                    i += 1
            if i >= n:
                raise ValueError('unterminated string in GraphQL document')
            i += 1
            tokens.append(_Token('string', ''.join(out)))
            continue
        if ch.isdigit() or (ch == '-' and i + 1 < n and text[i + 1].isdigit()):
            j = i
            if text[j] == '-':
                j += 1
            is_float = False
            while j < n and (text[j].isdigit() or text[j] in '.eE+-'):
                if text[j] in '.eE':
                    is_float = True
                j += 1
            raw = text[i:j]
            tokens.append(_Token('float' if is_float else 'int',
                                 float(raw) if is_float else int(raw)))
            i = j
            continue
        if ch.isalpha() or ch == '_':
            j = i
            while j < n and (text[j].isalnum() or text[j] == '_'):
                j += 1
            tokens.append(_Token('name', text[i:j]))
            i = j
            continue
        raise ValueError('unexpected character %r in GraphQL document' % ch)
    tokens.append(_Token('eof', None))
    return tokens


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, text):
        self.tokens = _lex(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def take(self, kind=None):
        token = self.tokens[self.pos]
        if kind is not None and token.kind != kind:
            raise ValueError('expected %s, got %s' % (kind, token.kind))
        self.pos += 1
        return token

    def at(self, kind):
        return self.tokens[self.pos].kind == kind

    def parse_document(self):
        operations = []
        fragments = {}
        while not self.at('eof'):
            if self.at('{'):
                operations.append(self.parse_operation(None))
            elif self.peek().kind == 'name' and self.peek().value in (
                    'query', 'mutation', 'subscription'):
                operations.append(self.parse_operation(None))
            elif self.peek().kind == 'name' and self.peek().value == 'fragment':
                name, type_cond, selections = self.parse_fragment()
                fragments[name] = (type_cond, selections)
            else:
                raise ValueError('unexpected token %r in GraphQL document'
                                 % (self.peek().value,))
        return operations, fragments

    def parse_operation(self, _unused):
        op_type = 'query'
        name = None
        if self.peek().kind == 'name' and self.peek().value in (
                'query', 'mutation', 'subscription'):
            op_type = self.take().value
            if self.at('name'):
                name = self.take().value
        variables = {}
        defaults = {}
        if self.at('('):
            self.take('(')
            while not self.at(')'):
                self.take('$')
                var_name = self.take('name').value
                self.take(':')
                self.parse_type()
                if self.at('='):
                    self.take('=')
                    defaults[var_name] = self.parse_value()
                variables[var_name] = True
            self.take(')')
        selections = self.parse_selection_set()
        return {'type': op_type, 'name': name, 'variables': variables,
                'defaults': defaults, 'selections': selections}

    def parse_type(self):
        if self.at('['):
            self.take('[')
            self.parse_type()
            self.take(']')
        else:
            self.take('name')
        if self.at('!'):
            self.take('!')

    def parse_fragment(self):
        self.take('name')  # 'fragment'
        name = self.take('name').value
        self.take('name')  # 'on'
        type_cond = self.take('name').value
        selections = self.parse_selection_set()
        return name, type_cond, selections

    def parse_selection_set(self):
        self.take('{')
        selections = []
        while not self.at('}'):
            selections.append(self.parse_selection())
        self.take('}')
        return selections

    def parse_selection(self):
        if self.at('...'):
            self.take('...')
            if self.at('name') and self.peek().value == 'on':
                self.take('name')
                type_cond = self.take('name').value
                selections = self.parse_selection_set()
                return {'kind': 'inline', 'type': type_cond,
                        'selections': selections}
            name = self.take('name').value
            return {'kind': 'spread', 'name': name}
        name = self.take('name').value
        alias = None
        if self.at(':'):
            self.take(':')
            alias = name
            name = self.take('name').value
        arguments = {}
        if self.at('('):
            self.take('(')
            while not self.at(')'):
                arg_name = self.take('name').value
                self.take(':')
                arguments[arg_name] = self.parse_value()
            self.take(')')
        # directives: parsed and ignored (the suite sends none)
        while self.at('@'):
            self.take('@')
            self.take('name')
            if self.at('('):
                self.take('(')
                depth = 1
                while depth:
                    token = self.take()
                    if token.kind == '(':
                        depth += 1
                    elif token.kind == ')':
                        depth -= 1
        selections = None
        if self.at('{'):
            selections = self.parse_selection_set()
        return {'kind': 'field', 'name': name, 'alias': alias or name,
                'arguments': arguments, 'selections': selections}

    def parse_value(self):
        token = self.peek()
        if token.kind == '$':
            self.take('$')
            return ('var', self.take('name').value)
        if token.kind in ('int', 'float', 'string'):
            self.take()
            return token.value
        if token.kind == 'name':
            self.take()
            if token.value == 'true':
                return True
            if token.value == 'false':
                return False
            if token.value == 'null':
                return None
            return ('enum', token.value)
        if token.kind == '[':
            self.take('[')
            items = []
            while not self.at(']'):
                items.append(self.parse_value())
            self.take(']')
            return items
        if token.kind == '{':
            self.take('{')
            obj = {}
            while not self.at('}'):
                key = self.take('name').value
                self.take(':')
                obj[key] = self.parse_value()
            self.take('}')
            return obj
        raise ValueError('unexpected value token %r' % (token.value,))


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------

def _resolve_value(value, variables, defaults):
    if isinstance(value, tuple) and value and value[0] == 'var':
        name = value[1]
        if name in variables:
            return variables[name]
        return defaults.get(name)
    if isinstance(value, tuple) and value and value[0] == 'enum':
        return value[1]
    if isinstance(value, list):
        return [_resolve_value(item, variables, defaults) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_value(item, variables, defaults)
                for key, item in value.items()}
    return value


def _cursor_for(number):
    return base64.urlsafe_b64encode(
        ('issue:%d' % number).encode('ascii')).decode('ascii')


def _cursor_number(cursor):
    if not isinstance(cursor, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode('ascii')).decode('ascii')
    except Exception:
        return None
    if not raw.startswith('issue:'):
        return None
    try:
        return int(raw[len('issue:'):])
    except ValueError:
        return None


class _Executor:
    def __init__(self, store, variables, defaults):
        self.store = store
        self.variables = variables
        self.defaults = defaults
        self.errors = []

    # -- selection ---------------------------------------------------------
    def execute(self, operation, fragments):
        root_type = 'Query' if operation['type'] == 'query' else 'Mutation'
        root = {'__typename': root_type}
        data = self.resolve_selections(root_type, root,
                                       operation['selections'], fragments)
        return data

    def resolve_selections(self, obj_type, obj, selections, fragments):
        out = {}
        for selection in selections:
            if selection['kind'] == 'inline':
                if selection['type'] in (obj_type, 'Node') or \
                        obj.get('__typename') == selection['type']:
                    sub = self.resolve_selections(obj_type, obj,
                                                  selection['selections'],
                                                  fragments)
                    out.update(sub)
                continue
            if selection['kind'] == 'spread':
                fragment = fragments.get(selection['name'])
                if fragment is None:
                    self.errors.append({'message': 'unknown fragment %s'
                                        % selection['name']})
                    continue
                type_cond, frag_selections = fragment
                if type_cond in (obj_type, 'Node') or \
                        obj.get('__typename') == type_cond:
                    sub = self.resolve_selections(obj_type, obj,
                                                  frag_selections, fragments)
                    out.update(sub)
                continue
            field = selection['name']
            alias = selection['alias']
            args = {name: _resolve_value(value, self.variables, self.defaults)
                    for name, value in selection['arguments'].items()}
            value = self.resolve_field(obj_type, obj, field, args,
                                       selection['selections'], fragments)
            out[alias] = value
        return out

    def resolve_field(self, obj_type, obj, field, args, selections, fragments):
        schema = SCHEMA.get(obj_type)
        if schema is not None and field not in schema.get('fields', []):
            self.errors.append({
                'message': "Cannot query field '%s' on type '%s'."
                           % (field, obj_type)})
            return None
        method = getattr(self, 'field_%s_%s' % (obj_type, field), None)
        if method is None:
            method = getattr(self, 'field_%s' % field, None)
        if method is not None:
            result = method(obj, args)
        else:
            result = obj.get(field)
        if selections is not None and result is not None:
            child_type = FIELD_TYPE.get((obj_type, field))
            if isinstance(result, list):
                item_type = child_type or obj_type
                return [self.resolve_selections(item_type, item, selections,
                                                fragments)
                        if isinstance(item, dict) else item
                        for item in result]
            if isinstance(result, dict):
                if child_type is None:
                    child_type = result.get('__typename', obj_type)
                return self.resolve_selections(child_type, result, selections,
                                              fragments)
        return result

    # -- Query ---------------------------------------------------------------
    def field_Query_viewer(self, _obj, _args):
        viewer = self.store.viewer
        return {'__typename': 'User', 'id': viewer['id'],
                'login': viewer['login'], 'name': viewer['name'],
                'email': viewer['email']}

    def field_Query_repository(self, _obj, args):
        repo = self.store.get_repo(args.get('owner'), args.get('name'))
        if repo is None:
            return None
        return self._repository_obj(repo)

    def field_Query_node(self, _obj, args):
        decoded = self.store.decode_global_id(args.get('id') or '')
        if decoded is None:
            return None
        kind, owner, name, _number, _comment = decoded
        if kind != 'Repository':
            return None
        repo = self.store.get_repo(owner, name)
        if repo is None:
            return None
        return self._repository_obj(repo)

    def field_Query___type(self, _obj, args):
        name = args.get('name')
        entry = SCHEMA.get(name)
        if entry is None:
            return None
        return {'__typename': '__Type', 'name': name, 'kind': entry['kind'],
                '_fields': entry.get('fields', []),
                '_enumValues': entry.get('enumValues', [])}

    # -- __Type --------------------------------------------------------------
    def field___Type_fields(self, obj, _args):
        return [{'__typename': '__Field', 'name': name, 'description': None}
                for name in obj.get('_fields', [])]

    def field___Type_enumValues(self, obj, _args):
        return [{'__typename': '__EnumValue', 'name': name,
                 'description': None}
                for name in obj.get('_enumValues', [])]

    def field___Type_name(self, obj, _args):
        return obj.get('name')

    def field___Type_kind(self, obj, _args):
        return obj.get('kind')

    # -- Repository ----------------------------------------------------------
    def _repository_obj(self, repo):
        return {'__typename': 'Repository', '_repo': repo,
                'id': self.store.global_id('Repository', repo),
                'databaseId': repo.id, 'name': repo.name,
                'nameWithOwner': '%s/%s' % (repo.owner, repo.name),
                'description': repo.description, 'private': repo.private,
                'defaultBranch': repo.default_branch}

    def field_Repository_owner(self, obj, _args):
        repo = obj['_repo']
        return {'__typename': 'User', 'login': repo.owner}

    def field_Repository_defaultBranchRef(self, obj, _args):
        repo = obj['_repo']
        if repo.head_oid() is None:
            return None
        return {'__typename': 'Ref', 'name': repo.default_branch}

    def field_Repository_viewerPermission(self, _obj, _args):
        return 'ADMIN'

    def field_Repository_hasIssuesEnabled(self, _obj, _args):
        return True

    def field_Repository_issue(self, obj, args):
        repo = obj['_repo']
        issue = repo.issues.get(args.get('number'))
        if issue is None:
            return None
        return self._issue_obj(repo, issue)

    def field_Repository_issues(self, obj, args):
        repo = obj['_repo']
        states = args.get('states')
        order_by = args.get('orderBy') or {}
        direction = (order_by.get('direction') or 'ASC').upper()
        issues = list(repo.issues.values())
        if states:
            wanted = set(s.upper() for s in states)
            issues = [i for i in issues if i.state.upper() in wanted]
        issues.sort(key=lambda i: i.number, reverse=(direction == 'DESC'))
        after = _cursor_number(args.get('after'))
        if after is not None:
            if direction == 'DESC':
                issues = [i for i in issues if i.number < after]
            else:
                issues = [i for i in issues if i.number > after]
        first = args.get('first')
        total = len(issues)
        if first is not None:
            page = issues[:first]
        else:
            page = issues
        return {'__typename': 'IssueConnection',
                '_nodes': [self._issue_obj(repo, i) for i in page],
                '_pageInfo': {
                    'hasNextPage': len(page) < total,
                    'endCursor': _cursor_for(page[-1].number) if page else None,
                }}

    def field_Repository_pullRequests(self, obj, args):
        repo = obj['_repo']
        pulls = [p for p in repo.pulls.values() if p.state == 'open']
        pulls.sort(key=lambda p: p.number, reverse=True)
        first = args.get('first')
        if first is not None:
            pulls = pulls[:first]
        return {'__typename': 'PullRequestConnection',
                '_nodes': [self._pull_obj(repo, p) for p in pulls]}

    def field_Repository_pullRequest(self, obj, args):
        repo = obj['_repo']
        pull = repo.pulls.get(args.get('number'))
        if pull is None:
            return None
        return self._pull_obj(repo, pull)

    # -- connections ---------------------------------------------------------
    def field_IssueConnection_nodes(self, obj, _args):
        return obj.get('_nodes', [])

    def field_IssueConnection_pageInfo(self, obj, _args):
        info = obj.get('_pageInfo', {})
        return {'__typename': 'PageInfo',
                'hasNextPage': info.get('hasNextPage', False),
                'hasPreviousPage': False,
                'endCursor': info.get('endCursor'),
                'startCursor': info.get('endCursor')}

    def field_PullRequestConnection_nodes(self, obj, _args):
        return obj.get('_nodes', [])

    def field_PullRequestChangedFileConnection_nodes(self, obj, _args):
        return obj.get('_nodes', [])

    # -- Issue ---------------------------------------------------------------
    def _issue_obj(self, repo, issue):
        return {'__typename': 'Issue', '_repo': repo, '_issue': issue,
                'id': self.store.global_id('Issue', repo, issue.number),
                'number': issue.number, 'title': issue.title,
                'state': issue.state.upper(), 'body': issue.body}

    # -- PullRequest ---------------------------------------------------------
    def _pull_obj(self, repo, pull):
        from rest import _mergeable
        state = 'MERGED' if pull.merged else pull.state.upper()
        return {'__typename': 'PullRequest', '_repo': repo, '_pull': pull,
                'id': self.store.global_id('PullRequest', repo, pull.number),
                'number': pull.number, 'title': pull.title,
                'state': state, 'merged': pull.merged,
                'headRefName': pull.head, 'baseRefName': pull.base,
                'mergeable': ('MERGEABLE' if _mergeable(self.store, repo, pull)
                              else 'CONFLICTING')
                             if pull.state == 'open' else 'UNKNOWN'}

    def field_PullRequest_files(self, obj, args):
        from gitobjs import merge_base
        from rest import _commit_info, _diff_files
        repo = obj['_repo']
        pull = obj['_pull']
        head_tip = repo.branch_tip(pull.head)
        base_tip = repo.branch_tip(pull.base)
        if head_tip is None or base_tip is None:
            return {'__typename': 'PullRequestChangedFileConnection',
                    '_nodes': []}
        base = merge_base(repo.objects, head_tip, base_tip)
        base_tree = _commit_info(repo, base)['tree'] if base else None
        head_tree = _commit_info(repo, head_tip)['tree']
        if base_tree is None:
            base_tree = repo.put_object('tree', b'')
        files = _diff_files(repo, base_tree, head_tree)
        first = args.get('first')
        if first is not None:
            files = files[:first]
        return {'__typename': 'PullRequestChangedFileConnection',
                '_nodes': [{'__typename': 'PullRequestChangedFile',
                            'path': f['filename'],
                            'additions': f['additions'],
                            'deletions': f['deletions']}
                           for f in files]}

    # -- Mutation ------------------------------------------------------------
    def field_Mutation_createIssue(self, _obj, args):
        from rest import _get_repo_or_404
        data = args.get('input') or {}
        decoded = self.store.decode_global_id(data.get('repositoryId') or '')
        if decoded is None or decoded[0] != 'Repository':
            self.errors.append({'message': 'invalid repositoryId'})
            return None
        repo = self.store.get_repo(decoded[1], decoded[2])
        if repo is None:
            self.errors.append({'message': 'repository not found'})
            return None
        title = data.get('title')
        if not title:
            self.errors.append({'message': 'title is required'})
            return None
        issue = repo.new_issue(str(title), data.get('body') or '', [])
        return {'__typename': 'CreateIssuePayload',
                'issue': self._issue_obj(repo, issue)}

    def field_Mutation_closeIssue(self, _obj, args):
        data = args.get('input') or {}
        decoded = self.store.decode_global_id(data.get('issueId') or '')
        if decoded is None or decoded[0] != 'Issue':
            self.errors.append({'message': 'invalid issueId'})
            return None
        repo = self.store.get_repo(decoded[1], decoded[2])
        issue = repo.issues.get(decoded[3]) if repo else None
        if issue is None:
            self.errors.append({'message': 'issue not found'})
            return None
        issue.state = 'closed'
        repo.bump()
        return {'__typename': 'CloseIssuePayload',
                'issue': self._issue_obj(repo, issue)}

    def field_Mutation_createPullRequest(self, _obj, args):
        data = args.get('input') or {}
        decoded = self.store.decode_global_id(data.get('repositoryId') or '')
        if decoded is None or decoded[0] != 'Repository':
            self.errors.append({'message': 'invalid repositoryId'})
            return None
        repo = self.store.get_repo(decoded[1], decoded[2])
        if repo is None:
            self.errors.append({'message': 'repository not found'})
            return None
        head = data.get('headRefName')
        base = data.get('baseRefName')
        head_tip = repo.branch_tip(head)
        base_tip = repo.branch_tip(base)
        if head_tip is None or base_tip is None:
            self.errors.append({'message': 'head or base branch not found'})
            return None
        for pull in repo.pulls.values():
            if pull.state == 'open' and pull.head == head:
                return {'__typename': 'CreatePullRequestPayload',
                        'pullRequest': self._pull_obj(repo, pull)}
        pull = repo.new_pull(str(data.get('title') or ''), '', head, base,
                             head_tip, base_tip)
        return {'__typename': 'CreatePullRequestPayload',
                'pullRequest': self._pull_obj(repo, pull)}

    def field_Mutation_mergePullRequest(self, _obj, args):
        from rest import _merge_conflict
        from store import server_commit
        data = args.get('input') or {}
        decoded = self.store.decode_global_id(data.get('pullRequestId') or '')
        if decoded is None or decoded[0] != 'PullRequest':
            self.errors.append({'message': 'invalid pullRequestId'})
            return None
        repo = self.store.get_repo(decoded[1], decoded[2])
        pull = repo.pulls.get(decoded[3]) if repo else None
        if pull is None:
            self.errors.append({'message': 'pull request not found'})
            return None
        if pull.state != 'open':
            self.errors.append({'message': 'pull request is not open'})
            return None
        if _merge_conflict(self.store, repo, pull):
            self.errors.append({'message': 'merge conflict'})
            return None
        head_tip = repo.branch_tip(pull.head)
        base_tip = repo.branch_tip(pull.base)
        if head_tip is None or base_tip is None:
            self.errors.append({'message': 'pull request branch not found'})
            return None
        from rest import _commit_info
        head_tree = _commit_info(repo, head_tip)['tree']
        message = 'Merge pull request #%d: %s' % (pull.number, pull.title)
        commit_oid = server_commit(self.store, repo, head_tree, [base_tip],
                                   message)
        repo.refs['refs/heads/' + pull.base] = commit_oid
        pull.state = 'closed'
        pull.merged = True
        from store import FIXED_ISO
        pull.merged_at = FIXED_ISO
        pull.merge_commit_sha = commit_oid
        repo.bump()
        return {'__typename': 'MergePullRequestPayload',
                'pullRequest': self._pull_obj(repo, pull)}

    def field_Mutation_addComment(self, _obj, args):
        data = args.get('input') or {}
        decoded = self.store.decode_global_id(data.get('subjectId') or '')
        if decoded is None or decoded[0] not in ('Issue', 'PullRequest'):
            self.errors.append({'message': 'invalid subjectId'})
            return None
        repo = self.store.get_repo(decoded[1], decoded[2])
        issue = repo.issues.get(decoded[3]) if repo else None
        if issue is None:
            self.errors.append({'message': 'issue not found'})
            return None
        body = data.get('body')
        if not body:
            self.errors.append({'message': 'body is required'})
            return None
        comment_id = repo.add_comment(issue, str(body))
        node = {'__typename': 'IssueComment',
                'id': self.store.global_id('IssueComment', repo,
                                           issue.number, comment_id),
                'body': str(body)}
        return {'__typename': 'AddCommentPayload',
                'commentEdge': {'__typename': 'IssueCommentEdge',
                                'node': node}}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def execute_operation(store, query_text, variables):
    """Run one operation document. Returns the response dict."""
    variables = variables or {}
    try:
        operations, fragments = _Parser(query_text).parse_document()
    except ValueError as exc:
        return {'data': None,
                'errors': [{'message': 'GraphQL parse error: %s' % exc}]}
    if not operations:
        return {'data': None,
                'errors': [{'message': 'no operation in document'}]}
    operation = operations[0]
    executor = _Executor(store, variables, operation['defaults'])
    try:
        data = executor.execute(operation, fragments)
    except Exception as exc:
        return {'data': None,
                'errors': [{'message': 'execution error: %s' % exc}]}
    result = {'data': data}
    if executor.errors:
        result['errors'] = executor.errors
    return result


def dispatch(store, req):
    if not is_authorized(req):
        return error_response(401, 'Bad credentials')
    try:
        body = req.json()
    except (ValueError, UnicodeDecodeError):
        return error_response(400, 'malformed JSON body')
    if isinstance(body, list):
        results = []
        for item in body:
            if not isinstance(item, dict) or 'query' not in item:
                results.append({'data': None,
                                'errors': [{'message': 'invalid operation'}]})
                continue
            results.append(execute_operation(store, item.get('query', ''),
                                             item.get('variables') or {}))
        return json_response(200, results)
    if not isinstance(body, dict) or 'query' not in body:
        return error_response(400, 'a GraphQL query is required')
    return json_response(200, execute_operation(
        store, body.get('query', ''), body.get('variables') or {}))
