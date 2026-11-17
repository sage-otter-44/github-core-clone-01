"""Git object model, packfiles and pkt-lines — implemented from scratch.

Covers exactly what the suite's git client speaks: SHA-1 object ids,
commit/tree/tag/blob objects, pack v2 read/write (plain objects plus
OFS_DELTA / REF_DELTA resolution on read), delta application, and the
pkt-line framing of smart HTTP.
"""

import hashlib
import struct
import zlib

ZERO_OID = '0' * 40
TYPE_CODE = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}
CODE_TYPE = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
PKT_LINE_MAX = 65520


def hash_object(obj_type, data):
    """git's object id: SHA-1 over '<type> <size>\\0' + bytes."""
    h = hashlib.sha1()
    h.update(('%s %d\x00' % (obj_type, len(data))).encode('ascii'))
    h.update(data)
    return h.hexdigest()


def is_oid(value):
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in '0123456789abcdef' for c in value)
    )


# ---------------------------------------------------------------------------
# trees / commits / tags
# ---------------------------------------------------------------------------

def parse_tree(data):
    """A tree's entries in stored order: (mode, name, oid)."""
    entries = []
    offset = 0
    n = len(data)
    while offset < n:
        space = data.index(b' ', offset)
        nul = data.index(b'\x00', space + 1)
        mode = data[offset:space].decode('ascii')
        name = data[space + 1:nul].decode('utf-8')
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        offset = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _oid = entry
    key = (name + '/') if mode == '40000' else name
    return key.encode('utf-8')


def build_tree(entries):
    """Build a tree object from entries, sorted the way git sorts them."""
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += ('%s %s\x00' % (mode, name)).encode('utf-8')
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data):
    """A commit's headers and message. Returns a dict."""
    text = data.decode('utf-8', 'surrogateescape')
    head, sep, message = text.partition('\n\n')
    tree = ''
    parents = []
    author = ''
    committer = ''
    for line in head.split('\n'):
        if not line:
            continue
        key, _, value = line.partition(' ')
        if key == 'tree':
            tree = value
        elif key == 'parent':
            parents.append(value)
        elif key == 'author':
            author = value
        elif key == 'committer':
            committer = value
    return {
        'tree': tree, 'parents': parents, 'author': author,
        'committer': committer, 'message': message if sep else '',
    }


def build_commit(tree, parents, message, author, committer, timestamp,
                 timezone='+0000'):
    lines = ['tree %s' % tree]
    for parent in parents:
        lines.append('parent %s' % parent)
    lines.append('author %s %d %s' % (author, timestamp, timezone))
    lines.append('committer %s %d %s' % (committer, timestamp, timezone))
    lines.append('')
    return ('\n'.join(lines) + '\n' + message).encode('utf-8')


def parse_tag(data):
    text = data.decode('utf-8', 'surrogateescape')
    head, sep, message = text.partition('\n\n')
    fields = {}
    for line in head.split('\n'):
        if not line:
            continue
        key, _, value = line.partition(' ')
        fields[key] = value
    return {
        'object': fields.get('object', ''),
        'type': fields.get('type', ''),
        'tag': fields.get('tag', ''),
        'tagger': fields.get('tagger', ''),
        'message': message if sep else '',
    }


def build_tag(obj, obj_type, tag, message, tagger, timestamp,
              timezone='+0000'):
    return (
        'object %s\ntype %s\ntag %s\ntagger %s %d %s\n\n%s'
        % (obj, obj_type, tag, tagger, timestamp, timezone, message)
    ).encode('utf-8')


# ---------------------------------------------------------------------------
# reachability
# ---------------------------------------------------------------------------

def collect_reachable(roots, lookup, allow_missing=False):
    """Every object id reachable from roots (commits, trees, tags, blobs)."""
    seen = set()
    stack = [oid for oid in roots if is_oid(oid)]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = lookup(oid)
        if obj is None:
            if allow_missing:
                continue
            raise KeyError('missing object %s' % oid)
        seen.add(oid)
        obj_type, data = obj
        if obj_type == 'commit':
            commit = parse_commit(data)
            stack.append(commit['tree'])
            stack.extend(commit['parents'])
        elif obj_type == 'tag':
            stack.append(parse_tag(data)['object'])
        elif obj_type == 'tree':
            for mode, _name, entry_oid in parse_tree(data):
                if mode != '160000':  # gitlinks name commits the repo does not hold
                    stack.append(entry_oid)
    return seen


def tree_at_commit(store, commit_oid):
    """The root tree oid of a commit, or None."""
    obj = store.get(commit_oid)
    if obj is None or obj[0] != 'commit':
        return None
    return parse_commit(obj[1])['tree']


def flatten_tree(store, tree_oid, prefix=''):
    """A tree flattened to {path: (mode, oid)} for regular entries."""
    out = {}
    obj = store.get(tree_oid)
    if obj is None or obj[0] != 'tree':
        return out
    for mode, name, oid in parse_tree(obj[1]):
        path = prefix + name
        if mode == '40000':
            out.update(flatten_tree(store, oid, path + '/'))
        else:
            out[path] = (mode, oid)
    return out


def is_ancestor(store, ancestor, descendant):
    """True when `ancestor` is reachable from `descendant` (or equal)."""
    if ancestor == descendant:
        return True
    seen = set()
    stack = [descendant]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        obj = store.get(oid)
        if obj is None or obj[0] != 'commit':
            continue
        parents = parse_commit(obj[1])['parents']
        if ancestor in parents:
            return True
        stack.extend(parents)
    return False


def merge_base(store, a, b):
    """A common ancestor of a and b (first found by breadth-first walk)."""
    if a is None or b is None:
        return None
    ancestors_a = set()
    stack = [a]
    while stack:
        oid = stack.pop()
        if oid in ancestors_a:
            continue
        ancestors_a.add(oid)
        obj = store.get(oid)
        if obj is not None and obj[0] == 'commit':
            stack.extend(parse_commit(obj[1])['parents'])
    stack = [b]
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        if oid in ancestors_a:
            return oid
        obj = store.get(oid)
        if obj is not None and obj[0] == 'commit':
            stack.extend(parse_commit(obj[1])['parents'])
    return None


# ---------------------------------------------------------------------------
# deltas
# ---------------------------------------------------------------------------

def _read_varint(data, offset):
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError('delta size varint is truncated')
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    return value, offset


def apply_delta(base, delta):
    """Apply a git delta instruction stream to a base object's bytes."""
    offset = 0
    source_size, offset = _read_varint(delta, offset)
    target_size, offset = _read_varint(delta, offset)
    if source_size != len(base):
        raise ValueError('delta expects a %d-byte base, got %d'
                         % (source_size, len(base)))
    out = bytearray()
    while offset < len(delta):
        op = delta[offset]
        offset += 1
        if op & 0x80:
            copy_offset = 0
            copy_size = 0
            shift = 0
            for bit in range(4):
                if op & (1 << bit):
                    copy_offset += delta[offset] << shift
                    offset += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    copy_size += delta[offset] << shift
                    offset += 1
                shift += 8
            if copy_size == 0:
                copy_size = 0x10000
            if copy_offset + copy_size > len(base):
                raise ValueError('delta copy is out of the base')
            out += base[copy_offset:copy_offset + copy_size]
        elif op:
            size = op & 0x7F
            out += delta[offset:offset + size]
            offset += size
        else:
            raise ValueError('delta opcode 0 is invalid')
    if len(out) != target_size:
        raise ValueError('delta produced %d of %d bytes'
                         % (len(out), target_size))
    return bytes(out)


# ---------------------------------------------------------------------------
# packfiles
# ---------------------------------------------------------------------------

def _read_pack_size(data, offset):
    first = data[offset]
    type_code = (first >> 4) & 0x07
    size = first & 0x0F
    shift = 4
    offset += 1
    byte = first
    while byte & 0x80:
        byte = data[offset]
        offset += 1
        size += (byte & 0x7F) << shift
        shift += 7
    return type_code, size, offset


def _read_ofs_base(data, offset):
    byte = data[offset]
    offset += 1
    value = byte & 0x7F
    while byte & 0x80:
        byte = data[offset]
        offset += 1
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value, offset


def read_pack(pack, resolve_base=None):
    """Read a packfile v2/v3. Returns (objects, delta_count).

    objects: dict oid -> (type, data). Raises ValueError on corruption.
    resolve_base: callable(oid) -> (type, data) for thin-pack REF_DELTA bases.
    """
    if len(pack) < 32 or pack[:4] != b'PACK':
        raise ValueError('pack does not start with PACK')
    version = struct.unpack('>I', pack[4:8])[0]
    if version not in (2, 3):
        raise ValueError('unsupported pack version %d' % version)
    count = struct.unpack('>I', pack[8:12])[0]
    trailer_start = len(pack) - 20
    trailer = pack[trailer_start:].hex()
    computed = hashlib.sha1(pack[:trailer_start]).hexdigest()
    if trailer != computed:
        raise ValueError('pack trailer does not match its bytes')

    raw = []  # (offset, type_code, size, data, base_offset, base_oid)
    by_offset = {}
    offset = 12
    for _ in range(count):
        entry_offset = offset
        type_code, size, offset = _read_pack_size(pack, offset)
        base_offset = None
        base_oid = None
        if type_code == 6:  # OFS_DELTA
            rel, offset = _read_ofs_base(pack, offset)
            base_offset = entry_offset - rel
        elif type_code == 7:  # REF_DELTA
            base_oid = pack[offset:offset + 20].hex()
            offset += 20
        elif type_code not in CODE_TYPE:
            raise ValueError('pack entry has unknown type code %d' % type_code)
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(pack[offset:trailer_start])
        consumed = len(pack[offset:trailer_start]) - len(decompressor.unused_data)
        if len(data) != size:
            raise ValueError('pack entry declares %d bytes but inflated to %d'
                             % (size, len(data)))
        offset += consumed
        entry = (entry_offset, type_code, size, data, base_offset, base_oid)
        raw.append(entry)
        by_offset[entry_offset] = entry
    if offset != trailer_start:
        raise ValueError('pack has stray bytes before its trailer')

    objects = {}
    resolved_by_oid = {}
    resolved_by_offset = {}
    resolving = set()
    delta_count = 0

    def resolve(entry):
        entry_offset, type_code, _size, data, base_offset, base_oid = entry
        if entry_offset in resolved_by_offset:
            return resolved_by_offset[entry_offset]
        if entry_offset in resolving:
            raise ValueError('delta cycle at pack offset %d' % entry_offset)
        resolving.add(entry_offset)
        if type_code == 6:
            base_entry = by_offset.get(base_offset)
            if base_entry is None:
                raise ValueError('ofs-delta base at offset %d is not in the pack'
                                 % base_offset)
            base_type, base_data = resolve(base_entry)[:2]
            obj_type = base_type
            obj_data = apply_delta(base_data, data)
        elif type_code == 7:
            base_obj = resolved_by_oid.get(base_oid)
            if base_obj is None:
                # The base may be another entry in this pack that is not
                # resolved yet (possibly itself a delta); resolve candidates
                # until one produces the base oid.
                for candidate in raw:
                    if candidate[0] in resolved_by_offset:
                        continue
                    resolved_candidate = resolve(candidate)
                    if resolved_candidate[2] == base_oid:
                        base_obj = resolved_candidate[:2]
                        break
            if base_obj is None and resolve_base is not None:
                base_obj = resolve_base(base_oid)
            if base_obj is None:
                raise ValueError('ref-delta base %s is not available' % base_oid)
            base_type, base_data = base_obj
            obj_type = base_type
            obj_data = apply_delta(base_data, data)
        else:
            obj_type = CODE_TYPE[type_code]
            obj_data = data
        oid = hash_object(obj_type, obj_data)
        resolved = (obj_type, obj_data, oid)
        resolved_by_offset[entry_offset] = resolved
        resolved_by_oid[oid] = (obj_type, obj_data)
        resolving.discard(entry_offset)
        return resolved

    for entry in raw:
        if entry[1] in (6, 7):
            delta_count += 1
        obj_type, obj_data, oid = resolve(entry)
        if oid not in objects:
            objects[oid] = (obj_type, obj_data)
    return objects, delta_count


def _pack_object_header(obj_type, size):
    code = TYPE_CODE[obj_type]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects, compress_level=6):
    """Build a packfile v2 from plain objects (no deltas).

    objects: iterable of (type, data). Returns bytes with a valid trailer.
    """
    items = list(objects)
    parts = [b'PACK' + struct.pack('>II', 2, len(items))]
    for obj_type, data in items:
        parts.append(_pack_object_header(obj_type, len(data)))
        parts.append(zlib.compress(data, compress_level))
    body = b''.join(parts)
    return body + hashlib.sha1(body).digest()


def verify_pack_checksum(pack):
    if len(pack) < 20:
        return False
    return hashlib.sha1(pack[:-20]).hexdigest() == pack[-20:].hex()


# ---------------------------------------------------------------------------
# pkt-lines
# ---------------------------------------------------------------------------

def pkt_line(payload):
    """Encode one data packet: 4 hex length bytes + payload."""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    if len(payload) + 4 > PKT_LINE_MAX:
        raise ValueError('pkt-line payload is too long')
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


def pkt_flush():
    return b'0000'


def pkt_delim():
    return b'0001'


def pkt_lines(items):
    """Encode a sequence; 'flush'/'delim' are control packets."""
    out = bytearray()
    for item in items:
        if item == 'flush':
            out += pkt_flush()
        elif item == 'delim':
            out += pkt_delim()
        else:
            out += pkt_line(item)
    return bytes(out)


class PktLineError(ValueError):
    pass


def parse_pkt_lines(data, allow_trailing=False):
    """Decode a whole buffer into packets.

    Returns (packets, consumed): packets is a list of ('data', payload) /
    ('flush', None) / ('delim', None); consumed is the byte offset just past
    the last complete packet. Raises PktLineError on malformed framing.
    """
    packets = []
    offset = 0
    n = len(data)
    while offset < n:
        if allow_trailing and offset + 4 <= n:
            head = data[offset:offset + 4]
            try:
                head.decode('ascii')
            except UnicodeDecodeError:
                break
            if not all(c in b'0123456789abcdefABCDEF' for c in head):
                break
        if offset + 4 > n:
            raise PktLineError('truncated pkt-line header')
        head = data[offset:offset + 4]
        try:
            head_text = head.decode('ascii')
        except UnicodeDecodeError:
            raise PktLineError('invalid pkt-line length header')
        if not all(c in '0123456789abcdefABCDEF' for c in head_text):
            raise PktLineError('invalid pkt-line length %r' % head_text)
        length = int(head_text, 16)
        if length == 0:
            packets.append(('flush', None))
            offset += 4
        elif length == 1:
            packets.append(('delim', None))
            offset += 4
        elif length == 2:
            packets.append(('response-end', None))
            offset += 4
        elif length < 4 or length > PKT_LINE_MAX:
            raise PktLineError('invalid pkt-line length %d' % length)
        else:
            if offset + length > n:
                raise PktLineError('truncated pkt-line')
            packets.append(('data', data[offset + 4:offset + length]))
            offset += length
    return packets, offset
