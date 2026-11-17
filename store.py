"""In-memory data model for the forge.

One Store holds every repository; one global RLock guards all access
(the HTTP transport is multi-threaded). Nothing here touches the network
or the filesystem: persistence is not required by the trial.
"""

import threading

from gitobjs import hash_object, parse_commit
from scenario import viewer_user

FIXED_ISO = '2025-01-01T00:00:00Z'
FIXED_EPOCH = 1700000000
DEFAULT_LABELS = ('bug', 'enhancement', 'documentation', 'question',
                  'good first issue', 'help wanted')
LABEL_COLORS = {
    'bug': 'd73a4a', 'enhancement': 'a2eeef', 'documentation': '0075ca',
    'question': 'd876e3', 'good first issue': '7057ff',
    'help wanted': '008672',
}


class Issue:
    __slots__ = ('number', 'title', 'body', 'state', 'labels', 'comments',
                 'created_seq', 'created_at', 'updated_at')

    def __init__(self, number, title, body, labels, created_seq):
        self.number = number
        self.title = title
        self.body = body or ''
        self.state = 'open'
        self.labels = list(labels)
        self.comments = []  # list of (id, body)
        self.created_seq = created_seq
        self.created_at = FIXED_ISO
        self.updated_at = FIXED_ISO


class Pull:
    __slots__ = ('number', 'title', 'body', 'state', 'head', 'base',
                 'head_sha', 'base_sha', 'merged', 'merged_at',
                 'merge_commit_sha', 'created_seq', 'created_at',
                 'updated_at')

    def __init__(self, number, title, body, head, base, head_sha, base_sha,
                 created_seq):
        self.number = number
        self.title = title
        self.body = body or ''
        self.state = 'open'
        self.head = head
        self.base = base
        self.head_sha = head_sha
        self.base_sha = base_sha
        self.merged = False
        self.merged_at = None
        self.merge_commit_sha = None
        self.created_seq = created_seq
        self.created_at = FIXED_ISO
        self.updated_at = FIXED_ISO


class Repo:
    """One repository: refs, objects, issues, pulls, labels."""

    def __init__(self, repo_id, owner, name, description='', private=False):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.description = description or ''
        self.private = bool(private)
        self.default_branch = 'main'
        self.refs = {}          # 'refs/heads/main' -> oid
        self.objects = {}       # oid -> (type, bytes)
        self.issues = {}        # number -> Issue
        self.pulls = {}         # number -> Pull
        self.next_number = 1    # shared issue/PR counter
        self.next_comment_id = 1
        self.labels = {name: {'name': name, 'color': LABEL_COLORS[name]}
                     for name in DEFAULT_LABELS}
        self.seq = 0            # server-created commit sequence
        self.created_seq = 0    # issue/PR creation order
        self.version = 0        # bumped on any mutation; caches key on it
        self.fetch_cache = {}   # fetch key -> packed bytes
        self.adv_cache = {}     # (service, protocol) -> bytes
        self.issue_list_cache = {}  # (state, sort, direction) -> list bytes

    # -- objects -----------------------------------------------------------
    def put_object(self, obj_type, data):
        oid = hash_object(obj_type, data)
        if oid not in self.objects:
            self.objects[oid] = (obj_type, data)
        return oid

    def get_object(self, oid):
        return self.objects.get(oid)

    def bump(self):
        self.version += 1
        self.fetch_cache.clear()
        self.adv_cache.clear()
        self.issue_list_cache.clear()

    # -- refs --------------------------------------------------------------
    def head_ref(self):
        return 'refs/heads/%s' % self.default_branch

    def head_oid(self):
        return self.refs.get(self.head_ref())

    def branch_tip(self, branch):
        return self.refs.get('refs/heads/%s' % branch)

    def sorted_branches(self):
        return sorted(name[11:] for name in self.refs
                      if name.startswith('refs/heads/'))

    def sorted_tags(self):
        return sorted(name[10:] for name in self.refs
                      if name.startswith('refs/tags/'))

    def ref_oid(self, refname):
        if refname == 'HEAD':
            return self.head_oid()
        return self.refs.get(refname)

    # -- issues / pulls ----------------------------------------------------
    def new_issue(self, title, body, labels):
        self.created_seq += 1
        number = self.next_number
        self.next_number += 1
        issue = Issue(number, title, body, labels, self.created_seq)
        self.issues[number] = issue
        self.bump()
        return issue

    def new_pull(self, title, body, head, base, head_sha, base_sha):
        self.created_seq += 1
        number = self.next_number
        self.next_number += 1
        pull = Pull(number, title, body, head, base, head_sha, base_sha,
                    self.created_seq)
        self.pulls[number] = pull
        self.bump()
        return pull

    def add_comment(self, issue, body):
        comment_id = self.next_comment_id
        self.next_comment_id += 1
        issue.comments.append((comment_id, body))
        self.bump()
        return comment_id


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}         # (owner, name) -> Repo
        self.next_repo_id = 5000
        self.viewer = viewer_user()
        self._global_id_seq = 0

    def viewer_login(self):
        return self.viewer['login']

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def create_repo(self, owner, name, description='', private=False):
        if (owner, name) in self.repos:
            return None
        repo = Repo(self.next_repo_id, owner, name, description, private)
        self.next_repo_id += 1
        self.repos[(owner, name)] = repo
        return repo

    def delete_repo(self, owner, name):
        return self.repos.pop((owner, name), None) is not None

    def viewer_repos(self):
        login = self.viewer_login()
        return [repo for (owner, _name), repo in self.repos.items()
                if owner == login]

    def global_id(self, kind, repo, number=None, comment_id=None):
        """Opaque, stable global ids (GraphQL node ids)."""
        import base64
        if kind == 'Repository':
            raw = 'Repository:%s/%s' % (repo.owner, repo.name)
        elif kind == 'Issue':
            raw = 'Issue:%s/%s:%d' % (repo.owner, repo.name, number)
        elif kind == 'PullRequest':
            raw = 'PullRequest:%s/%s:%d' % (repo.owner, repo.name, number)
        elif kind == 'IssueComment':
            raw = 'IssueComment:%s/%s:%d:%d' % (repo.owner, repo.name, number,
                                                comment_id)
        else:
            raw = '%s:%s/%s' % (kind, repo.owner, repo.name)
        return base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii')

    def decode_global_id(self, gid):
        """Resolve a global id back to (kind, owner, name, number, comment)."""
        import base64
        try:
            raw = base64.urlsafe_b64decode(gid.encode('ascii')).decode('utf-8')
        except Exception:
            return None
        parts = raw.split(':', 1)
        if len(parts) != 2:
            return None
        kind, rest = parts
        if kind == 'Repository':
            if '/' not in rest:
                return None
            owner, name = rest.split('/', 1)
            return (kind, owner, name, None, None)
        if kind in ('Issue', 'PullRequest'):
            if '/' not in rest or ':' not in rest:
                return None
            owner_repo, number = rest.rsplit(':', 1)
            owner, name = owner_repo.split('/', 1)
            try:
                return (kind, owner, name, int(number), None)
            except ValueError:
                return None
        if kind == 'IssueComment':
            pieces = rest.rsplit(':', 2)
            if len(pieces) != 3:
                return None
            owner, name = pieces[0].split('/', 1)
            try:
                return (kind, owner, name, int(pieces[1]), int(pieces[2]))
            except ValueError:
                return None
        return None


def commit_author_line(user):
    return '%s <%s>' % (user['name'], user['email'])


def server_commit(store, repo, tree_oid, parents, message):
    """Create a server-side commit with a fixed timestamp and a sequence
    number folded into the message-free part of the identity: the
    timestamp is fixed (no wall clock), and the sequence guarantees
    distinct commits with identical content get distinct shas."""
    repo.seq += 1
    timestamp = FIXED_EPOCH + repo.seq
    user = store.viewer
    author = '%s <%s>' % (user['name'], user['email'])
    from gitobjs import build_commit
    data = build_commit(tree_oid, parents, message, author, author,
                        timestamp)
    return repo.put_object('commit', data)
