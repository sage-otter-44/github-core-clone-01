"""Git smart HTTP: advertisements, ls-refs, fetch (v2 + v0) and receive-pack.

Implements the wire contract the kit's git client speaks (see
sdk/conformance/git-client.ts): v2 upload-pack with ls-refs/fetch, v0
upload-pack fallback, v0 receive-pack with report-status. Fast-forward
only; a refused ref is reported as `ng <ref> non-fast-forward`.
"""

from gitobjs import (
    ZERO_OID, hash_object, is_oid, parse_commit,
    parse_tag, parse_tree, pkt_delim, pkt_flush, pkt_line, pkt_lines,
    read_pack, write_pack, PktLineError,
)

UPLOAD_V2_CAPS = [
    'ls-refs=unborn',
    'fetch=shallow wait-for-done filter',
    'object-format=sha1',
    'agent=forge/1.0',
]
UPLOAD_V0_CAPS = ('symref=HEAD:refs/heads/main agent=forge/1.0 '
                  'side-band-64k thin-pack ofs-delta shallow multi_ack')
RECEIVE_V0_CAPS = ('report-status side-band-64k delete-refs ofs-delta '
                   'agent=forge/1.0')
SIDEBAND_CHUNK = 32768
CT_UPLOAD_ADV = 'application/x-git-upload-pack-advertisement'
CT_RECEIVE_ADV = 'application/x-git-receive-pack-advertisement'
CT_UPLOAD_RESULT = 'application/x-git-upload-pack-result'
CT_RECEIVE_RESULT = 'application/x-git-receive-pack-result'


class GitError(Exception):
    """A request-level git protocol error: (http_status, message)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# advertisements
# ---------------------------------------------------------------------------

def _v0_ref_lines(repo, caps):
    lines = []
    head = repo.head_oid()
    if head is None:
        lines.append('%s capabilities^{}\x00%s\n' % (ZERO_OID, caps))
        return lines
    lines.append('%s HEAD\x00%s\n' % (head, caps))
    for name in repo.sorted_branches():
        lines.append('%s refs/heads/%s\n'
                     % (repo.refs['refs/heads/' + name], name))
    for name in repo.sorted_tags():
        refname = 'refs/tags/' + name
        oid = repo.refs[refname]
        lines.append('%s %s\n' % (oid, refname))
        obj = repo.get_object(oid)
        if obj is not None and obj[0] == 'tag':
            lines.append('%s %s^{}\n' % (parse_tag(obj[1])['object'], refname))
    return lines


def advertisement(repo, service, want_v2):
    """(body, content_type) for GET info/refs?service=<service>."""
    key = (service, want_v2)
    cached = repo.adv_cache.get(key)
    if cached is not None:
        return cached
    if service == 'git-receive-pack':
        body = pkt_lines(['# service=git-receive-pack\n', 'flush']
                         + _v0_ref_lines(repo, RECEIVE_V0_CAPS)
                         + ['flush'])
        result = (body, CT_RECEIVE_ADV)
    elif want_v2:
        body = pkt_lines(['# service=git-upload-pack\n', 'flush',
                          'version 2\n'] + [cap + '\n' for cap in UPLOAD_V2_CAPS]
                         + ['flush'])
        result = (body, CT_UPLOAD_ADV)
    else:
        body = pkt_lines(['# service=git-upload-pack\n', 'flush']
                         + _v0_ref_lines(repo, UPLOAD_V0_CAPS)
                         + ['flush'])
        result = (body, CT_UPLOAD_ADV)
    repo.adv_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# ls-refs (v2)
# ---------------------------------------------------------------------------

def ls_refs(repo, body):
    from gitobjs import parse_pkt_lines
    try:
        packets, _ = parse_pkt_lines(body)
    except PktLineError as exc:
        raise GitError(400, str(exc))
    prefixes = []
    in_args = False
    for kind, payload in packets:
        if kind == 'delim':
            in_args = True
            continue
        if kind == 'flush':
            break
        if kind != 'data' or not in_args:
            continue
        line = payload.decode('utf-8', 'replace').rstrip('\n')
        if line.startswith('ref-prefix '):
            prefixes.append(line[len('ref-prefix '):])
    cache_key = ('ls-refs', tuple(prefixes))
    cached = repo.adv_cache.get(cache_key)
    if cached is not None:
        return cached
    lines = []
    head = repo.head_oid()

    def wanted(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    if head is not None and not prefixes:
        lines.append('%s HEAD symref-target:refs/heads/main\n' % head)
    elif head is None and not prefixes:
        lines.append('unborn HEAD symref-target:refs/heads/main\n')
    if head is not None:
        for name in repo.sorted_branches():
            refname = 'refs/heads/' + name
            if wanted(refname):
                lines.append('%s %s\n' % (repo.refs[refname], refname))
        for name in repo.sorted_tags():
            refname = 'refs/tags/' + name
            if not wanted(refname):
                continue
            oid = repo.refs[refname]
            obj = repo.get_object(oid)
            if obj is not None and obj[0] == 'tag':
                lines.append('%s %s peeled:%s\n'
                             % (oid, refname, parse_tag(obj[1])['object']))
            else:
                lines.append('%s %s\n' % (oid, refname))
    result = pkt_lines(lines + ['flush'])
    repo.adv_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# fetch computation
# ---------------------------------------------------------------------------

def _add_tree_objects(repo, tree_oid, objects):
    stack = [tree_oid]
    while stack:
        oid = stack.pop()
        if oid in objects:
            continue
        obj = repo.get_object(oid)
        if obj is None:
            continue
        objects.add(oid)
        if obj[0] == 'tree':
            for mode, _name, entry_oid in parse_tree(obj[1]):
                if mode == '40000':
                    stack.append(entry_oid)
                elif mode != '160000':
                    if repo.get_object(entry_oid) is not None:
                        objects.add(entry_oid)


def _reachable_with_depth(repo, roots, depth):
    """Objects reachable from roots, following commit parents only while the
    depth limit permits. Returns (objects, boundaries): boundaries are the
    commits whose parents were cut by the depth limit."""
    objects = set()
    commit_depth = {}
    stack = [(oid, 0) for oid in roots if is_oid(oid)]
    while stack:
        oid, d = stack.pop()
        if oid in commit_depth and commit_depth[oid] <= d:
            continue
        obj = repo.get_object(oid)
        if obj is None:
            continue
        if obj[0] == 'commit':
            commit_depth[oid] = d
            objects.add(oid)
            commit = parse_commit(obj[1])
            _add_tree_objects(repo, commit['tree'], objects)
            if depth is None or d + 1 < depth:
                for parent in commit['parents']:
                    stack.append((parent, d + 1))
        elif obj[0] == 'tag':
            objects.add(oid)
            stack.append((parse_tag(obj[1])['object'], d))
        elif obj[0] == 'tree':
            _add_tree_objects(repo, oid, objects)
        else:
            objects.add(oid)
    boundaries = set()
    for oid in commit_depth:
        obj = repo.get_object(oid)
        if obj is None or obj[0] != 'commit':
            continue
        parents = parse_commit(obj[1])['parents']
        if parents and not all(p in commit_depth for p in parents):
            boundaries.add(oid)
    return objects, boundaries


def _is_ancestor_of_any(repo, ancestor, wants):
    if ancestor in wants:
        return True
    for want in wants:
        obj = repo.get_object(want)
        if obj is None or obj[0] != 'commit':
            continue
        seen = set()
        stack = [want]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if oid == ancestor:
                return True
            child = repo.get_object(oid)
            if child is not None and child[0] == 'commit':
                stack.extend(parse_commit(child[1])['parents'])
    return False


def _reachable_with_cutoffs(repo, roots, cutoffs):
    """Objects reachable from roots, except that a cutoff commit's parents
    are never followed: a client's `shallow` lines say it holds the
    boundary commit but none of the history below it."""
    seen = set()
    stack = [oid for oid in roots if is_oid(oid)]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = repo.get_object(oid)
        if obj is None:
            continue
        seen.add(oid)
        if obj[0] == 'commit':
            commit = parse_commit(obj[1])
            stack.append(commit['tree'])
            if oid not in cutoffs:
                stack.extend(commit['parents'])
        elif obj[0] == 'tag':
            stack.append(parse_tag(obj[1])['object'])
        elif obj[0] == 'tree':
            for mode, _name, entry_oid in parse_tree(obj[1]):
                if mode != '160000':
                    stack.append(entry_oid)
    return seen


def compute_fetch(repo, wants, haves, depth, shallow_req):
    """The fetch plan: (send_oids, shallow, unshallow, acks, nak)."""
    want_objects, boundaries = _reachable_with_depth(repo, wants, depth)
    have_objects = _reachable_with_cutoffs(repo, haves, set(shallow_req))
    send = want_objects - have_objects
    shallow = []
    for commit_oid in sorted(boundaries):
        obj = repo.get_object(commit_oid)
        if obj is None or obj[0] != 'commit':
            continue
        parents = parse_commit(obj[1])['parents']
        if parents and not all(p in send or p in have_objects for p in parents):
            shallow.append(commit_oid)
    unshallow = []
    for boundary in shallow_req:
        obj = repo.get_object(boundary)
        if obj is None or obj[0] != 'commit':
            continue
        parents = parse_commit(obj[1])['parents']
        if parents and all(p in send for p in parents):
            unshallow.append(boundary)
    acks = []
    for have in haves:
        if _is_ancestor_of_any(repo, have, wants):
            acks.append(have)
    nak = bool(haves) and not acks
    return send, shallow, unshallow, acks, nak


def _sideband_pack(pack):
    out = bytearray()
    for i in range(0, len(pack), SIDEBAND_CHUNK):
        out += pkt_line(b'\x01' + pack[i:i + SIDEBAND_CHUNK])
    return bytes(out)


def _build_pack(repo, send):
    items = []
    for oid in sorted(send):
        obj = repo.get_object(oid)
        if obj is not None:
            items.append(obj)
    return write_pack(items)


def fetch_response(repo, wants, haves, depth, shallow_req, version2):
    """The full fetch response body, cached per (repo version, request)."""
    key = (repo.version, tuple(wants), tuple(haves), depth,
           tuple(shallow_req), version2)
    cached = repo.fetch_cache.get(key)
    if cached is not None:
        return cached
    send, shallow, unshallow, acks, nak = compute_fetch(
        repo, wants, haves, depth, shallow_req)
    pack = _build_pack(repo, send)
    out = bytearray()
    if version2:
        if haves:
            out += pkt_line('acknowledgments\n')
            for ack in acks:
                out += pkt_line('ACK %s\n' % ack)
            if nak:
                out += pkt_line('NAK\n')
            out += pkt_line('ready\n')
            out += pkt_delim()
        if shallow or unshallow:
            out += pkt_line('shallow-info\n')
            for oid in shallow:
                out += pkt_line('shallow %s\n' % oid)
            for oid in unshallow:
                out += pkt_line('unshallow %s\n' % oid)
            out += pkt_delim()
        out += pkt_line('packfile\n')
        out += _sideband_pack(pack)
        out += pkt_delim()
        out += pkt_flush()
    else:
        for ack in acks:
            out += pkt_line('ACK %s\n' % ack)
        if nak:
            out += pkt_line('NAK\n')
        for oid in shallow:
            out += pkt_line('shallow %s\n' % oid)
        for oid in unshallow:
            out += pkt_line('unshallow %s\n' % oid)
        out += _sideband_pack(pack)
        out += pkt_flush()
    body = bytes(out)
    repo.fetch_cache[key] = body
    return body


# ---------------------------------------------------------------------------
# fetch request parsing
# ---------------------------------------------------------------------------

def _parse_packets(body):
    from gitobjs import parse_pkt_lines
    try:
        packets, _ = parse_pkt_lines(body)
    except PktLineError as exc:
        raise GitError(400, str(exc))
    return packets


def fetch_v2(repo, body):
    packets = _parse_packets(body)
    command = None
    wants, haves, shallow = [], [], []
    depth = None
    in_args = False
    for kind, payload in packets:
        if kind == 'delim':
            in_args = True
            continue
        if kind == 'flush':
            break
        if kind != 'data':
            continue
        line = payload.decode('utf-8', 'replace').rstrip('\n')
        if not in_args:
            if line.startswith('command='):
                command = line[len('command='):]
            continue
        if line.startswith('want '):
            wants.append(line[5:].strip())
        elif line.startswith('have '):
            haves.append(line[5:].strip())
        elif line.startswith('shallow '):
            shallow.append(line[8:].strip())
        elif line.startswith('deepen '):
            try:
                depth = int(line[7:].strip())
            except ValueError:
                raise GitError(400, 'bad deepen value')
        elif line.startswith('filter '):
            pass  # filters are accepted and ignored; nothing is filtered out
        elif line in ('thin-pack', 'no-progress', 'ofs-delta', 'done',
                      'wait-for-done'):
            pass
        elif line.startswith('deepen-since ') or line.startswith('deepen-not '):
            pass
    if command != 'fetch':
        raise GitError(400, 'unsupported v2 command %r' % command)
    for want in wants:
        if not is_oid(want) or repo.get_object(want) is None:
            # Protocol-correct answer: a packfile section carrying a band-3
            # error, so the client fails cleanly instead of hanging.
            out = bytearray()
            if haves:
                out += pkt_line('acknowledgments\n')
                out += pkt_line('NAK\n')
                out += pkt_delim()
            out += pkt_line('packfile\n')
            out += pkt_line(b'\x03object not found: %s\n' % want)
            out += pkt_delim()
            out += pkt_flush()
            return bytes(out)
    return fetch_response(repo, wants, haves, depth, shallow, True)


def fetch_v0(repo, body):
    packets = _parse_packets(body)
    wants, haves, shallow = [], [], []
    depth = None
    section = 0
    for kind, payload in packets:
        if kind == 'flush':
            section += 1
            continue
        if kind != 'data':
            continue
        line = payload.decode('utf-8', 'replace').rstrip('\n')
        if section == 0:
            if line.startswith('want '):
                parts = line.split(' ')
                if len(parts) >= 2 and is_oid(parts[1]):
                    wants.append(parts[1])
            elif line.startswith('shallow '):
                shallow.append(line[8:].strip())
            elif line.startswith('deepen '):
                try:
                    depth = int(line[7:].strip())
                except ValueError:
                    raise GitError(400, 'bad deepen value')
        else:
            if line.startswith('have '):
                haves.append(line[5:].strip())
    return fetch_response(repo, wants, haves, depth, shallow, False)


# ---------------------------------------------------------------------------
# receive-pack (push)
# ---------------------------------------------------------------------------

def _parse_push_body(body):
    """Split a receive-pack body into (commands, pack_bytes)."""
    from gitobjs import parse_pkt_lines
    try:
        packets, consumed = parse_pkt_lines(body, allow_trailing=True)
    except PktLineError as exc:
        raise GitError(400, str(exc))
    commands = []
    pack_start = len(body)
    for kind, payload in packets:
        if kind == 'flush':
            pack_start = consumed
            break
        if kind != 'data':
            raise GitError(400, 'unexpected packet in a push')
        line = payload.decode('utf-8', 'replace').rstrip('\n')
        if '\x00' in line:
            line = line.split('\x00', 1)[0]
        parts = line.split(' ')
        if len(parts) != 3 or not is_oid(parts[0]) or not is_oid(parts[1]) \
                or not parts[2].startswith('refs/'):
            raise GitError(400, 'malformed push command %r' % line[:60])
        commands.append((parts[0], parts[1], parts[2]))
    if not commands:
        raise GitError(400, 'a push needs at least one command')
    return commands, body[pack_start:]


def _apply_ref_update(repo, old, new, ref):
    current = repo.refs.get(ref)
    if new == ZERO_OID:  # delete
        if current is None:
            return False, 'non-fast-forward'
        if old != ZERO_OID and old != current:
            return False, 'non-fast-forward'
        del repo.refs[ref]
        return True, ''
    if old == ZERO_OID:  # create
        if current is not None:
            return False, 'non-fast-forward'
        repo.refs[ref] = new
        return True, ''
    if current is None or old != current:
        return False, 'non-fast-forward'
    if not _is_ancestor_of_any(repo, old, [new]):
        return False, 'non-fast-forward'
    repo.refs[ref] = new
    return True, ''


def receive_pack(repo, body):
    commands, pack_bytes = _parse_push_body(body)
    unpack_detail = 'ok'
    new_objects = {}
    updates_ref = any(new != ZERO_OID for _old, new, _ref in commands)
    if updates_ref:
        if not pack_bytes:
            unpack_detail = 'missing packfile'
        else:
            try:
                new_objects, _delta_count = read_pack(
                    pack_bytes, resolve_base=repo.get_object)
            except ValueError as exc:
                unpack_detail = str(exc)
    if unpack_detail == 'ok':
        for _old, new, _ref in commands:
            if new != ZERO_OID and new not in repo.objects \
                    and new not in new_objects:
                unpack_detail = 'object %s is not available' % new
                break
    results = []
    if unpack_detail == 'ok':
        for oid, obj in new_objects.items():
            if oid not in repo.objects:
                repo.objects[oid] = obj
        changed = False
        for old, new, ref in commands:
            ok, reason = _apply_ref_update(repo, old, new, ref)
            results.append((ref, ok, reason))
            if ok:
                changed = True
        if changed:
            repo.bump()
    inner = bytearray()
    inner += pkt_line('unpack %s\n' % unpack_detail)
    for ref, ok, reason in results:
        if ok:
            inner += pkt_line('ok %s\n' % ref)
        else:
            inner += pkt_line('ng %s %s\n' % (ref, reason))
    inner += pkt_flush()
    out = bytearray()
    out += pkt_line(b'\x01' + bytes(inner))
    out += pkt_flush()
    return bytes(out), CT_RECEIVE_RESULT
