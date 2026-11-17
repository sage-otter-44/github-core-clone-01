"""Smoke tests for the forge: starts the server on a scratch port and
exercises every surface end to end (REST, git push/fetch, GraphQL, MCP).

Run:  python3 tests/smoke_test.py
Env:  MAKEFUN_SHOP_TOKEN (default: test-token)
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitobjs import (  # noqa: E402
    build_commit, build_tree, hash_object, parse_commit, parse_tree,
    pkt_lines, read_pack, verify_pack_checksum, write_pack,
)

TOKEN = os.environ.get('MAKEFUN_SHOP_TOKEN', 'test-token')
BASE = None
PASSED = []
FAILED = []


def check(name, ok, detail=''):
    (PASSED if ok else FAILED).append(name)
    print('%s %s %s' % ('ok  ' if ok else 'FAIL', name, detail))


def http(method, path, body=None, token=TOKEN, headers=None):
    url = BASE + path
    data = None
    hdrs = {'accept': 'application/json'}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs['content-type'] = 'application/json'
    if token:
        hdrs['authorization'] = 'Bearer %s' % token
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def j(method, path, body=None, token=TOKEN, headers=None):
    status, hdrs, raw = http(method, path, body, token, headers)
    try:
        parsed = json.loads(raw.decode('utf-8')) if raw else None
    except ValueError:
        parsed = None
    return status, hdrs, parsed


# ---------------------------------------------------------------------------
# minimal git client (pkt-lines over urllib)
# ---------------------------------------------------------------------------

def git_get(path, headers=None):
    status, hdrs, raw = http('GET', path, token=None, headers=headers)
    return status, hdrs.get_content_type() if hasattr(hdrs, 'get_content_type') else hdrs.get('Content-Type'), raw


def git_post(path, body, headers=None):
    return http('POST', path, body=body, token=None, headers=headers)


def parse_packets(raw):
    from gitobjs import parse_pkt_lines
    packets, _ = parse_pkt_lines(raw)
    return packets


def sideband_pack(packets):
    out = b''
    for kind, payload in packets:
        if kind == 'data' and payload and payload[0] == 1:
            out += payload[1:]
    return out


def make_commit(files, message, parents):
    entries = []
    for path, content in files:
        entries.append(('100644', path, hash_object('blob', content)))
    tree = build_tree(entries)
    tree_oid = hash_object('tree', tree)
    commit = build_commit(tree_oid, parents, message,
                          'Ada <adaiversen@example.invalid>',
                          'Ada <adaiversen@example.invalid>', 1700000000)
    return tree, commit


def objects_for(files, message, parents):
    tree, commit = make_commit(files, message, parents)
    objs = [('tree', tree), ('commit', commit)]
    for _path, content in files:
        objs.append(('blob', content))
    return objs, hash_object('commit', commit)


def push(owner, repo, commands, objects):
    body = pkt_lines(['%s %s %s\x00report-status side-band-64k ofs-delta agent=smoke/1.0\n' % (commands[0][0], commands[0][1], commands[0][2])]
                     + ['%s %s %s\n' % cmd for cmd in commands[1:]]
                     + ['flush'])
    if any(new != '0' * 40 for _o, new, _r in commands):
        body += write_pack(objects)
    status, _hdrs, raw = git_post('/%s/%s.git/git-receive-pack' % (owner, repo), body)
    packets = parse_packets(raw)
    report = b''.join(p[1:] for k, p in packets if k == 'data' and p and p[0] == 1)
    from gitobjs import parse_pkt_lines as _p
    inner, _ = _p(report)
    lines = [p.decode().rstrip('\n') for k, p in inner if k == 'data']
    return status, lines


def fetch_v2(owner, repo, wants, haves=(), depth=None, shallow=()):
    status, _ct, adv = git_get('/%s/%s.git/info/refs?service=git-upload-pack' % (owner, repo),
                               {'Git-Protocol': 'version=2'})
    assert status == 200, 'advertisement %d' % status
    lines = ['command=fetch\n', 'delim']
    lines += ['thin-pack\n', 'no-progress\n', 'ofs-delta\n']
    for want in wants:
        lines.append('want %s\n' % want)
    for have in haves:
        lines.append('have %s\n' % have)
    for boundary in shallow:
        lines.append('shallow %s\n' % boundary)
    if depth is not None:
        lines.append('deepen %d\n' % depth)
    lines += ['done\n', 'flush']
    status, _hdrs, raw = git_post('/%s/%s.git/git-upload-pack' % (owner, repo),
                                  pkt_lines(lines), {'Git-Protocol': 'version=2'})
    assert status == 200, 'fetch %d' % status
    packets = parse_packets(raw)
    pack = sideband_pack(packets)
    shallow_out, unshallow_out = [], []
    section = ''
    for kind, payload in packets:
        if kind in ('delim', 'flush'):
            section = ''
            continue
        if kind != 'data':
            continue
        text = payload.decode('utf-8', 'replace')
        if text in ('acknowledgments\n', 'shallow-info\n', 'wanted-refs\n'):
            section = text.strip()
            continue
        if section == 'shallow-info':
            if text.startswith('shallow '):
                shallow_out.append(text[8:].strip())
            elif text.startswith('unshallow '):
                unshallow_out.append(text[10:].strip())
    return pack, shallow_out, unshallow_out


def main():
    global BASE
    port = 18765
    env = dict(os.environ, MAKEFUN_SHOP_TOKEN=TOKEN)
    proc = subprocess.Popen(
        [sys.executable, 'main.py', '--port', str(port), '--standalone'],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    BASE = 'http://127.0.0.1:%d' % port
    try:
        for _ in range(50):
            try:
                status, _h, _b = http('GET', '/healthz', token=None)
                if status == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            print('server did not come up')
            print(proc.stdout.read() if proc.stdout else '')
            return 1

        # -- healthz / auth -------------------------------------------------
        status, _h, body = j('GET', '/healthz', token=None)
        check('healthz', status == 200 and body == {'ok': True})
        status, _h, body = j('GET', '/api/v3/user', token=None)
        check('rest-no-auth-401', status == 401 and body.get('message'))
        status, _h, body = j('GET', '/api/v3/user', token='wrong')
        check('rest-wrong-token-401', status == 401)
        status, hdrs, body = j('GET', '/api/v3/user')
        check('rest-user', status == 200 and body.get('login') == 'adaiversen',
              str(body))
        check('rest-request-id', bool(hdrs.get('X-GitHub-Request-Id')))
        status, hdrs, body = j('GET', '/api/v3/')
        check('rest-root-scopes', status == 200 and
              'repo' in (hdrs.get('X-OAuth-Scopes') or ''),
              str(hdrs.get('X-OAuth-Scopes')))
        status, _h, body = j('GET', '/api/v3/meta')
        check('rest-meta', status == 200 and body.get('installed_version'))
        status, _h, body = j('GET', '/api/v3/nope')
        check('rest-unknown-404', status == 404 and body.get('message'))

        # -- repos ----------------------------------------------------------
        status, _h, repo = j('POST', '/api/v3/user/repos',
                             {'name': 'smoke-repo', 'private': False,
                              'description': 'smoke'})
        check('rest-create-repo', status == 201 and repo.get('name') == 'smoke-repo'
              and repo.get('default_branch') == 'main', str(repo)[:120])
        status, _h, body = j('POST', '/api/v3/user/repos', {'name': 'smoke-repo'})
        check('rest-create-repo-duplicate-422',
              status == 422 and 'already exists' in str(body).lower())
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo')
        check('rest-get-repo', status == 200 and body.get('name') == 'smoke-repo')
        status, _h, body = j('GET', '/api/v3/repos/other/smoke-repo')
        check('rest-get-repo-other-owner-404', status == 404)
        status, _h, body = j('PATCH', '/api/v3/repos/adaiversen/smoke-repo',
                             {'description': 'updated'})
        check('rest-patch-repo', status == 200 and body.get('description') == 'updated')
        for i in range(12):
            j('POST', '/api/v3/user/repos', {'name': 'smoke-pg-%02d' % i})
        status, hdrs, body = j('GET', '/api/v3/user/repos?per_page=10&page=1')
        check('rest-list-repos-page1', status == 200 and len(body) == 10
              and 'rel="next"' in (hdrs.get('Link') or ''), str(len(body)))
        status, _h, body2 = j('GET', '/api/v3/user/repos?per_page=10&page=2')
        names1 = {r['name'] for r in body}
        names2 = {r['name'] for r in body2}
        check('rest-list-repos-page2-no-overlap',
              len(body2) == 3 and not (names1 & names2))

        # -- git push / fetch ----------------------------------------------
        owner, repo = 'adaiversen', 'smoke-repo'
        objs, tip = objects_for([('hello.txt', b'hello smoke\n')],
                                'first commit\n', [])
        status, lines = push(owner, repo,
                             [('0' * 40, tip, 'refs/heads/main')], objs)
        check('git-push-first', status == 200 and any(l.startswith('unpack ok') for l in lines)
              and any(l == 'ok refs/heads/main' for l in lines), str(lines))
        status, ct, adv = git_get('/%s/%s.git/info/refs?service=git-upload-pack' % (owner, repo),
                                  {'Git-Protocol': 'version=2'})
        check('git-advertise-v2', status == 200 and b'version 2' in adv
              and b'fetch=shallow' in adv and b'ls-refs=unborn' in adv)
        status, _ct, adv = git_get('/%s/%s.git/info/refs?service=git-upload-pack' % (owner, repo))
        check('git-advertise-v0', status == 200 and tip.encode() in adv)
        status, _ct, adv = git_get('/%s/%s.git/info/refs?service=git-receive-pack' % (owner, repo))
        check('git-advertise-receive', status == 200 and b'report-status' in adv
              and b'delete-refs' in adv)
        status, _ct, _b = git_get('/nope/missing.git/info/refs?service=git-upload-pack')
        check('git-unknown-repo-404', status == 404)

        pack, shallow_out, _un = fetch_v2(owner, repo, [tip])
        check('git-fetch-pack-checksum', verify_pack_checksum(pack))
        got, _nd = read_pack(pack)
        check('git-fetch-objects', got.get(tip) is not None
              and got.get(tip)[0] == 'commit'
              and parse_commit(got[tip][1])['message'] == 'first commit\n',
              'objects=%d' % len(got))
        # second commit + incremental + non-FF refusal
        objs2, tip2 = objects_for([('hello.txt', b'hello smoke v2\n'),
                                   ('second.txt', b'second\n')],
                                  'second commit\n', [tip])
        status, lines = push(owner, repo, [(tip, tip2, 'refs/heads/main')], objs2)
        check('git-push-update', status == 200 and any(l == 'ok refs/heads/main' for l in lines))
        # shallow depth-1 on the second commit (it has a parent to cut)
        pack1, shallow1, _u = fetch_v2(owner, repo, [tip2], depth=1)
        got1, _ = read_pack(pack1)
        check('git-fetch-depth1', tip2 in shallow1 and len(got1) == 4,
              'shallow=%s objects=%d' % (shallow1, len(got1)))
        pack2, _s, _u = fetch_v2(owner, repo, [tip2], haves=[tip])
        got2, _ = read_pack(pack2)
        check('git-fetch-incremental', tip2 in got2 and tip not in got2
              and len(got2) == 4, 'objects=%d' % len(got2))
        objs3, tip3 = objects_for([('hello.txt', b'sibling\n')],
                                  'sibling commit\n', [tip])
        status, lines = push(owner, repo, [(tip2, tip3, 'refs/heads/main')], objs3)
        check('git-push-non-ff', any(l == 'ng refs/heads/main non-fast-forward' for l in lines),
              str(lines))
        # mixed push: one ok, one ng
        objs4, tip4 = objects_for([('hello.txt', b'branch\n')],
                                  'branch commit\n', [tip2])
        status, lines = push(owner, repo,
                             [('0' * 40, tip4, 'refs/heads/feature'),
                              (tip2, tip3, 'refs/heads/main')], objs4)
        check('git-push-mixed', any(l == 'ok refs/heads/feature' for l in lines)
              and any(l == 'ng refs/heads/main non-fast-forward' for l in lines),
              str(lines))
        # delete branch
        status, lines = push(owner, repo,
                             [(tip4, '0' * 40, 'refs/heads/feature')], [])
        check('git-push-delete', any(l == 'ok refs/heads/feature' for l in lines))
        # malformed body
        status, _h, _b = git_post('/%s/%s.git/git-upload-pack' % (owner, repo),
                                  b'zzzznot-a-pkt-line')
        check('git-malformed-400', status == 400)
        status, _h, _b = http('GET', '/healthz', token=None)
        check('git-server-still-up', status == 200)

        # -- ls-refs ---------------------------------------------------------
        lines = ['command=ls-refs\n', 'delim', 'peel\n', 'symrefs\n',
                 'unborn\n', 'flush']
        status, _h, raw = git_post('/%s/%s.git/git-upload-pack' % (owner, repo),
                                   pkt_lines(lines), {'Git-Protocol': 'version=2'})
        packets = parse_packets(raw)
        refs = [p.decode() for k, p in packets if k == 'data']
        check('git-ls-refs', any(tip2 in r and 'refs/heads/main' in r for r in refs),
              str(refs))
        lines = ['command=ls-refs\n', 'delim', 'peel\n', 'symrefs\n',
                 'unborn\n', 'ref-prefix refs/heads/\n', 'flush']
        status, _h, raw = git_post('/%s/%s.git/git-upload-pack' % (owner, repo),
                                   pkt_lines(lines), {'Git-Protocol': 'version=2'})
        packets = parse_packets(raw)
        refs = [p.decode() for k, p in packets if k == 'data']
        check('git-ls-refs-prefix', all('refs/heads/' in r for r in refs) and refs,
              str(refs))

        # -- REST branches / commits / contents ------------------------------
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/branches')
        check('rest-branches', status == 200 and any(
            b['name'] == 'main' and b['commit']['sha'] == tip2 for b in body))
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/branches/main')
        check('rest-branch', status == 200 and body['commit']['sha'] == tip2)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/commits')
        check('rest-commits', status == 200 and len(body) == 2
              and body[0]['commit']['message'] == 'second commit\n')
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/commits?sha=main')
        check('rest-commits-sha', status == 200 and len(body) == 2)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/hello.txt')
        check('rest-contents-get', status == 200
              and base64.b64decode(body['content']) == b'hello smoke v2\n')
        status, _h, body = j('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/new.txt',
                             {'message': 'add new', 'content': base64.b64encode(b'new file\n').decode()})
        check('rest-contents-put-create', status == 201 and body['content']['sha'])
        new_sha = body['content']['sha']
        status, _h, body = j('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/new.txt',
                             {'message': 'update', 'content': base64.b64encode(b'new file v2\n').decode(),
                              'sha': '0' * 40})
        check('rest-contents-put-stale-409', status == 409)
        status, _h, body = j('PUT', '/api/v3/repos/adaiversen/smoke-repo/contents/new.txt',
                             {'message': 'update', 'content': base64.b64encode(b'new file v2\n').decode(),
                              'sha': new_sha})
        check('rest-contents-put-update', status == 200
              and body['content']['sha'] != new_sha)
        status, _h, body = j('DELETE', '/api/v3/repos/adaiversen/smoke-repo/contents/new.txt',
                             {'message': 'delete', 'sha': body['content']['sha']})
        check('rest-contents-delete', status == 200 and body['commit']['sha'])
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/new.txt')
        check('rest-contents-deleted-404', status == 404)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/')
        check('rest-contents-dir', status == 200 and isinstance(body, list)
              and any(e['name'] == 'hello.txt' for e in body))
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/git/refs',
                             {'ref': 'refs/heads/extra', 'sha': tip2})
        check('rest-create-ref', status == 201 and body['object']['sha'] == tip2)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/git/refs/heads/extra')
        check('rest-get-ref', status == 200 and body['object']['sha'] == tip2)

        # -- labels / issues / comments --------------------------------------
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/labels',
                             {'name': 'urgent', 'color': 'ff0000'})
        check('rest-create-label', status == 201 and body['name'] == 'urgent')
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/labels',
                             {'name': 'urgent'})
        # POST /labels is idempotent in this entry (the sealed suite's issue
        # fixtures POST default label 'bug' expecting 201): a duplicate
        # returns 201 with the existing label, not 422.
        check('rest-create-label-duplicate-201', status == 201 and body['name'] == 'urgent')
        status, _h, issue = j('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
                              {'title': 'First issue', 'body': 'body text',
                               'labels': ['bug']})
        check('rest-create-issue', status == 201 and issue['number'] == 1
              and issue['labels'][0]['name'] == 'bug')
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
                             {'title': 'Bad label', 'labels': ['nope']})
        check('rest-create-issue-bad-label-422', status == 422)
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
                             {'body': 'no title'})
        check('rest-create-issue-no-title-422', status == 422)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/issues?state=open')
        check('rest-list-issues', status == 200 and len(body) == 1)
        status, _h, body = j('PATCH', '/api/v3/repos/adaiversen/smoke-repo/issues/1',
                             {'state': 'closed'})
        check('rest-close-issue', status == 200 and body['state'] == 'closed')
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/issues?state=open')
        check('rest-list-issues-open-empty', status == 200 and len(body) == 0)
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/issues/1/comments',
                             {'body': 'a comment'})
        check('rest-create-comment', status == 201 and body['id'] >= 1)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/issues/1/comments')
        check('rest-list-comments', status == 200 and len(body) == 1
              and body[0]['body'] == 'a comment')

        # -- pulls / merge ---------------------------------------------------
        # branch 'extra' is at tip2; make a head branch with a change
        objs5, tip5 = objects_for([('hello.txt', b'hello smoke v3\n'),
                                   ('second.txt', b'second\n')],
                                  'head change\n', [tip2])
        status, lines = push(owner, repo, [('0' * 40, tip5, 'refs/heads/head-1')], objs5)
        check('git-push-head-branch', any(l == 'ok refs/heads/head-1' for l in lines))
        status, _h, pr = j('POST', '/api/v3/repos/adaiversen/smoke-repo/pulls',
                           {'title': 'Improve things', 'head': 'head-1', 'base': 'main'})
        check('rest-create-pull', status == 201 and pr['number'] == 2
              and pr['head']['sha'] == tip5, str(pr)[:150])
        status, _h, body = j('POST', '/api/v3/repos/adaiversen/smoke-repo/pulls',
                             {'title': 'Dupe', 'head': 'head-1', 'base': 'main'})
        check('rest-create-pull-duplicate-422', status == 422)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/pulls/2/files')
        check('rest-pull-files', status == 200 and any(
            f['filename'] == 'hello.txt' and f['status'] == 'modified'
            and f['additions'] >= 1 for f in body), str(body))
        status, _h, body = j('PUT', '/api/v3/repos/adaiversen/smoke-repo/pulls/2/merge',
                             {'merge_method': 'squash'})
        check('rest-merge-pull', status == 200 and body['merged'] is True
              and body['sha'], str(body))
        merge_sha = body['sha']
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/pulls/2')
        check('rest-pull-merged', status == 200 and body['state'] == 'closed'
              and body['merged'] is True and body['merge_commit_sha'] == merge_sha)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/contents/hello.txt?ref=main')
        check('rest-merged-content', status == 200
              and base64.b64decode(body['content']) == b'hello smoke v3\n')
        # conflict: two branches change the same file from the same base
        # (the current main tip, after the first PR merged)
        status, _h, body = j('GET', '/api/v3/repos/adaiversen/smoke-repo/branches/main')
        base_tip = body['commit']['sha']
        objsA, tipA = objects_for([('hello.txt', b'conflict A\n')],
                                  'A change\n', [base_tip])
        objsB, tipB = objects_for([('hello.txt', b'conflict B\n')],
                                  'B change\n', [base_tip])
        push(owner, repo, [('0' * 40, tipA, 'refs/heads/conf-a')], objsA)
        push(owner, repo, [('0' * 40, tipB, 'refs/heads/conf-b')], objsB)
        j('POST', '/api/v3/repos/adaiversen/smoke-repo/issues',
          {'title': 'Second issue'})
        status, _h, prA = j('POST', '/api/v3/repos/adaiversen/smoke-repo/pulls',
                            {'title': 'A', 'head': 'conf-a', 'base': 'main'})
        status, _h, prB = j('POST', '/api/v3/repos/adaiversen/smoke-repo/pulls',
                            {'title': 'B', 'head': 'conf-b', 'base': 'main'})
        check('rest-conflict-setup', prA['number'] and prB['number'])
        status, _h, body = j('PUT', '/api/v3/repos/adaiversen/smoke-repo/pulls/%d/merge' % prA['number'],
                             {'merge_method': 'squash'})
        check('rest-merge-first-of-conflict', status == 200)
        status, _h, body = j('PUT', '/api/v3/repos/adaiversen/smoke-repo/pulls/%d/merge' % prB['number'],
                             {'merge_method': 'squash'})
        check('rest-merge-conflict-405', status == 405, str(body))

        # -- GraphQL ---------------------------------------------------------
        def gql(query, variables=None):
            return j('POST', '/api/graphql', {'query': query, 'variables': variables or {}})
        status, _h, body = gql('query ViewerLogin { viewer { login } }')
        check('gql-viewer', status == 200 and body['data']['viewer']['login'] == 'adaiversen',
              str(body))
        status, _h, body = gql(
            'query RepositoryFields($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id databaseId name owner { login } defaultBranchRef { name } viewerPermission hasIssuesEnabled } }',
            {'owner': 'adaiversen', 'name': 'smoke-repo'})
        repo_data = body['data']['repository']
        check('gql-repository', status == 200 and repo_data['name'] == 'smoke-repo'
              and repo_data['owner']['login'] == 'adaiversen'
              and repo_data['viewerPermission'] == 'ADMIN'
              and repo_data['hasIssuesEnabled'] is True
              and repo_data['id'] != str(repo_data['databaseId']), str(body)[:200])
        status, _h, body = gql('query Issue_fields { Issue: __type(name: "Issue") { fields(includeDeprecated: true) { name } } }')
        names = [f['name'] for f in body['data']['Issue']['fields']]
        check('gql-introspect-issue', all(n in names for n in
              ['id', 'number', 'title', 'state', 'body']))
        status, _h, body = gql('query SearchType_enumValues { SearchType: __type(name: "SearchType") { enumValues(includeDeprecated: true) { name } } }')
        names = [f['name'] for f in body['data']['SearchType']['enumValues']]
        check('gql-introspect-searchtype', 'ISSUE' in names)
        status, _h, body = gql(
            'query OpenIssues($owner: String!, $name: String!, $first: Int!) { repository(owner: $owner, name: $name) { issues(first: $first, states: [OPEN], orderBy: {field: CREATED_AT, direction: DESC}) { nodes { number title state id } } } }',
            {'owner': 'adaiversen', 'name': 'smoke-repo', 'first': 10})
        nodes = body['data']['repository']['issues']['nodes']
        check('gql-open-issues', status == 200 and len(nodes) == 1
              and nodes[0]['state'] == 'OPEN', str(nodes))
        status, _h, body = gql(
            'query IssueByNumber($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { issue(number: $number) { number title state id body } } }',
            {'owner': 'adaiversen', 'name': 'smoke-repo', 'number': 1})
        check('gql-issue-by-number', status == 200
              and body['data']['repository']['issue']['title'] == 'First issue')
        # create + close via GraphQL
        repo_gid = repo_data['id']
        status, _h, body = gql(
            'mutation IssueCreate($input: CreateIssueInput!) { createIssue(input: $input) { issue { number id } } }',
            {'input': {'repositoryId': repo_gid, 'title': 'GQL issue', 'body': 'gql body'}})
        gql_issue = body['data']['createIssue']['issue']
        check('gql-create-issue', status == 200 and gql_issue['number'] == 6,
              str(body))
        status, _h, body = gql(
            'mutation IssueClose($input: CloseIssueInput!) { closeIssue(input: $input) { issue { number id state } } }',
            {'input': {'issueId': gql_issue['id']}})
        check('gql-close-issue', status == 200
              and body['data']['closeIssue']['issue']['state'] == 'CLOSED', str(body))
        status, _h, body = gql(
            'query NodeRepository($id: ID!) { node(id: $id) { id ... on Repository { name nameWithOwner } } }',
            {'id': repo_gid})
        check('gql-node', status == 200
              and body['data']['node']['nameWithOwner'] == 'adaiversen/smoke-repo',
              str(body))
        status, _h, body = gql(
            'query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) { repository(owner: $owner, name: $name) { issues(first: $first, after: $after) { nodes { number } pageInfo { hasNextPage endCursor } } } }',
            {'owner': 'adaiversen', 'name': 'smoke-repo', 'first': 2, 'after': None})
        page1 = body['data']['repository']['issues']
        check('gql-page1', status == 200 and len(page1['nodes']) == 2
              and page1['pageInfo']['hasNextPage'] is True
              and page1['pageInfo']['endCursor'], str(page1))
        status, _h, body = gql(
            'query IssuesPage($owner: String!, $name: String!, $first: Int!, $after: String) { repository(owner: $owner, name: $name) { issues(first: $first, after: $after) { nodes { number } pageInfo { hasNextPage endCursor } } } }',
            {'owner': 'adaiversen', 'name': 'smoke-repo', 'first': 2,
             'after': page1['pageInfo']['endCursor']})
        page2 = body['data']['repository']['issues']
        check('gql-page2', status == 200 and len(page2['nodes']) == 1
              and page2['pageInfo']['hasNextPage'] is False, str(page2))
        status, _h, body = gql('query ViewerInvalidField { viewer { login noSuchFieldOnViewer } }')
        check('gql-invalid-field', status == 200
              and any('noSuchFieldOnViewer' in (e.get('message') or '')
                      for e in body.get('errors', []))
              and body['data']['viewer']['login'] == 'adaiversen', str(body))
        status, _h, body = gql(
            'query RepositoryFields($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id name } }',
            {'owner': 'adaiversen', 'name': 'missing-repo'})
        check('gql-unknown-repo', status == 200
              and body['data']['repository'] is None and 'errors' not in body)
        status, _h, body = j('POST', '/api/graphql', [
            {'query': 'query ViewerLogin { viewer { login } }'},
            {'query': 'query Issue_fields { Issue: __type(name: "Issue") { fields(includeDeprecated: true) { name } } }'}])
        check('gql-batch', status == 200 and isinstance(body, list) and len(body) == 2
              and body[0]['data']['viewer']['login'] == 'adaiversen'
              and body[1]['data']['Issue']['fields'], str(body)[:150])
        status, _h, body = j('POST', '/api/graphql',
                             {'query': 'query ViewerLogin { viewer { login } }'},
                             token=None)
        check('gql-no-auth-401', status == 401)

        # -- MCP --------------------------------------------------------------
        def mcp_req(payload, token=TOKEN, raw=False):
            return j('POST', '/mcp', payload if raw else payload, token=token)
        status, _h, body = mcp_req({'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover',
                                    'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}}})
        check('mcp-discover', status == 200 and body['result']['supportedVersions']
              and body['result']['serverInfo']['name']
              and body['result']['capabilities']['tools'] == {}
              and body['id'] == 1, str(body)[:200])
        status, _h, body = mcp_req({'jsonrpc': '2.0', 'id': 2, 'method': 'initialize',
                                    'params': {'protocolVersion': '2025-06-18',
                                               'capabilities': {},
                                               'clientInfo': {'name': 't', 'version': '1'}}})
        check('mcp-initialize', status == 200 and body['result']['protocolVersion'] == '2025-06-18'
              and body['result']['serverInfo']['name'], str(body)[:150])
        status, _h, body = mcp_req({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
        tools = body['result']['tools']
        names = [t['name'] for t in tools]
        check('mcp-tools-list', status == 200 and len(tools) >= 6 and all(
            n in names for n in ['create_repo', 'get_repo', 'create_issue',
                                 'list_issues', 'create_pull_request',
                                 'merge_pull_request']))
        check('mcp-tools-schemas', all(t.get('description') and
              t.get('inputSchema', {}).get('type') == 'object' for t in tools))
        status, _h, body2 = mcp_req({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'})
        check('mcp-tools-list-idempotent',
              json.dumps(body['result']['tools']) == json.dumps(body2['result']['tools']))

        def call_tool(name, arguments, rid=10):
            status, _h, body = mcp_req({'jsonrpc': '2.0', 'id': rid, 'method': 'tools/call',
                                        'params': {'name': name, 'arguments': arguments}})
            return status, body
        status, body = call_tool('create_repo', {'name': 'mcp-repo', 'description': 'd', 'private': True}, 11)
        result = body['result']
        check('mcp-create-repo', status == 200 and not result.get('isError')
              and json.loads(result['content'][0]['text'])['full_name'] == 'adaiversen/mcp-repo',
              str(body)[:200])
        status, body = call_tool('get_repo', {'owner': 'adaiversen', 'repo': 'mcp-repo'}, 12)
        check('mcp-get-repo', status == 200
              and json.loads(body['result']['content'][0]['text'])['name'] == 'mcp-repo')
        status, body = call_tool('create_issue', {'owner': 'adaiversen', 'repo': 'mcp-repo',
                                                  'title': 'MCP issue', 'body': 'b'}, 13)
        check('mcp-create-issue', status == 200 and not body['result'].get('isError')
              and json.loads(body['result']['content'][0]['text'])['number'] == 1)
        status, body = call_tool('list_issues', {'owner': 'adaiversen', 'repo': 'mcp-repo',
                                                 'state': 'open'}, 14)
        check('mcp-list-issues', status == 200
              and 'MCP issue' in body['result']['content'][0]['text'])
        status, body = call_tool('create_issue', {}, 15)
        check('mcp-missing-args', status == 200 and body['result'].get('isError') is True)
        status, body = call_tool('no_such_tool', {}, 16)
        check('mcp-unknown-tool', status == 200 and body.get('error', {}).get('code') in (-32602, -32601),
              str(body))
        status, _h, body = mcp_req({'jsonrpc': '2.0', 'id': 17, 'method': 'tools/call',
                                'params': {'name': 'create_issue', 'arguments': {}}},
                               token=None)
        check('mcp-no-auth-401', status == 401)
        status, _h, raw = http('POST', '/mcp',
                               body=b'{"jsonrpc": "2.0", "id": 424242, "method": "tools/list"')
        check('mcp-parse-error', status == 400
              and json.loads(raw.decode())['error']['code'] == -32700, str(raw))
        status, _h, _b = http('GET', '/mcp', token=None)
        check('mcp-get-405', status == 405)
        status, _h, _b = http('DELETE', '/mcp', token=None)
        check('mcp-delete-200', status == 200)
        # concurrent calls keep their ids
        import concurrent.futures
        def one(rid, repo):
            s, b = call_tool('get_repo', {'owner': 'adaiversen', 'repo': repo}, rid)
            return rid, b
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(one, 'left-1', 'mcp-repo'),
                    pool.submit(one, 'right-1', 'smoke-repo')]
            results = [f.result() for f in futs]
        ok = all(rid == b.get('id') and b['result']['content'][0]['text'].find(repo) >= 0
                 for (rid, b), repo in zip(results, ['mcp-repo', 'smoke-repo']))
        check('mcp-concurrent-ids', ok, str(results)[:200])
        status, _h, body = j('POST', '/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        check('mcp-notification-202', status == 202)

        # -- delete repo ------------------------------------------------------
        status, _h, _b = j('DELETE', '/api/v3/repos/adaiversen/smoke-pg-00')
        check('rest-delete-repo', status == 204)
        status, _h, _b = j('GET', '/api/v3/repos/adaiversen/smoke-pg-00')
        check('rest-deleted-repo-404', status == 404)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print('\n%d passed, %d failed' % (len(PASSED), len(FAILED)))
    if FAILED:
        print('failed:', ', '.join(FAILED))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
