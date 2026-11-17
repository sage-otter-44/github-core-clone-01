"""HTTP transport and routing for the forge.

ThreadingHTTPServer with a minimal request parser (no email module),
one global lock around dispatch (the store is single-threaded by
design; the lock also serialises identical fetch computations so the
fetch cache absorbs concurrent clones), and keep-alive support.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gitproto
import graphql
import mcp
import rest
from gitobjs import PktLineError
from store import Store
from util import (
    Request, Response, error_response, json_response, new_request_id,
    parse_query,
)

REASONS = {
    200: 'OK', 201: 'Created', 202: 'Accepted', 204: 'No Content',
    400: 'Bad Request', 401: 'Unauthorized', 404: 'Not Found',
    405: 'Method Not Allowed', 409: 'Conflict', 422: 'Unprocessable Entity',
    500: 'Internal Server Error',
}

_GIT_PATH = re.compile(r'^/([^/]+)/([^/]+)\.git(/.*)$')


class Forge:
    """The application: one store, one lock, pure request dispatch."""

    def __init__(self):
        self.store = Store()
        self.lock = threading.RLock()

    def dispatch(self, req):
        path = req.path
        method = req.method
        try:
            if path == '/healthz':
                if method != 'GET':
                    return Response(405, b'')
                return json_response(200, {'ok': True})
            if path == '/api/graphql':
                if method != 'POST':
                    return Response(405, b'')
                return graphql.dispatch(self.store, req)
            if path == '/mcp':
                return mcp.dispatch(self.store, req)
            if path.startswith('/api/v3'):
                return rest.dispatch(self.store, req)
            match = _GIT_PATH.match(path)
            if match:
                return self._dispatch_git(req, match)
            return error_response(404, 'Not Found')
        except Exception as exc:
            return error_response(500, 'internal error: %s' % exc)

    def _dispatch_git(self, req, match):
        owner, name, suffix = match.group(1), match.group(2), match.group(3)
        repo = self.store.get_repo(owner, name)
        if repo is None:
            return error_response(404, 'repository %s/%s not found'
                                  % (owner, name))
        if suffix == '/info/refs' and req.method == 'GET':
            service = req.query.get('service')
            if service not in ('git-upload-pack', 'git-receive-pack'):
                return error_response(404, 'unknown git service')
            want_v2 = req.headers.get('git-protocol') == 'version=2'
            body, content_type = gitproto.advertisement(repo, service, want_v2)
            return Response(200, body, content_type)
        if suffix == '/git-upload-pack' and req.method == 'POST':
            return self._dispatch_upload_pack(repo, req)
        if suffix == '/git-receive-pack' and req.method == 'POST':
            try:
                body, content_type = gitproto.receive_pack(repo, req.body)
            except gitproto.GitError as exc:
                return error_response(exc.status, exc.message)
            return Response(200, body, content_type)
        return error_response(404, 'Not Found')

    def _dispatch_upload_pack(self, repo, req):
        want_v2 = req.headers.get('git-protocol') == 'version=2'
        if not want_v2:
            try:
                body = gitproto.fetch_v0(repo, req.body)
            except gitproto.GitError as exc:
                return error_response(exc.status, exc.message)
            return Response(200, body, gitproto.CT_UPLOAD_RESULT)
        # v2: the first data packet names the command.
        from gitobjs import parse_pkt_lines
        try:
            packets, _ = parse_pkt_lines(req.body)
        except PktLineError as exc:
            return error_response(400, str(exc))
        command = None
        for kind, payload in packets:
            if kind == 'data':
                line = payload.decode('utf-8', 'replace').rstrip('\n')
                if line.startswith('command='):
                    command = line[len('command='):]
                break
        try:
            if command == 'ls-refs':
                body = gitproto.ls_refs(repo, req.body)
            elif command == 'fetch':
                body = gitproto.fetch_v2(repo, req.body)
            else:
                return error_response(400, 'unsupported v2 command %r'
                                      % command)
        except gitproto.GitError as exc:
            return error_response(exc.status, exc.message)
        return Response(200, body, gitproto.CT_UPLOAD_RESULT)


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Forge/1.0'

    # -- minimal request parsing (no email module) -------------------------
    def parse_request(self):
        # handle_one_request already read the request line into
        # self.raw_requestline; parse it (do not read from rfile again).
        requestline = self.raw_requestline
        if not requestline:
            self.close_connection = True
            return False
        try:
            line = requestline.decode('iso-8859-1').rstrip('\r\n')
            self.command, self.path, self.request_version = line.split(' ', 2)
        except ValueError:
            self.send_error(400, 'Bad request line')
            return False
        self.requestline = line
        headers = {}
        while True:
            hline = self.rfile.readline(65537)
            if hline in (b'\r\n', b'\n', b''):
                break
            if b':' not in hline:
                continue
            key, value = hline.decode('iso-8859-1').split(':', 1)
            headers[key.strip().lower()] = value.strip()
        self.headers = headers
        conn = headers.get('connection', '').lower()
        if self.request_version == 'HTTP/1.0':
            self.close_connection = conn != 'keep-alive'
        else:
            self.close_connection = conn == 'close'
        if headers.get('expect', '').lower() == '100-continue':
            self.wfile.write(b'HTTP/1.1 100 Continue\r\n\r\n')
        return True

    def log_message(self, _format, *_args):
        pass

    # -- verbs ---------------------------------------------------------------
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    # -- handling ------------------------------------------------------------
    def _read_body(self):
        length = self.headers.get('content-length')
        if length is not None:
            try:
                size = int(length)
            except ValueError:
                return b''
            if size <= 0:
                return b''
            return self.rfile.read(size)
        if self.headers.get('transfer-encoding', '').lower() == 'chunked':
            chunks = []
            while True:
                size_line = self.rfile.readline(65537).strip()
                try:
                    size = int(size_line.split(b';')[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65537)
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)
            return b''.join(chunks)
        return b''

    def _handle(self):
        try:
            body = self._read_body()
        except Exception:
            body = b''
        target = self.path
        if target.startswith('http://') or target.startswith('https://'):
            target = '/' + '/'.join(target.split('/')[3:])
        if '?' in target:
            path, query_string = target.split('?', 1)
        else:
            path, query_string = target, ''
        req = Request(self.command, path, parse_query(query_string),
                      self.headers, body)
        forge = self.server.forge
        with forge.lock:
            response = forge.dispatch(req)
        self._write(response)

    def _write(self, response):
        body = response.body or b''
        reason = REASONS.get(response.status, '')
        head = ['HTTP/1.1 %d %s\r\n' % (response.status, reason)]
        head.append('Content-Type: %s\r\n' % response.content_type)
        head.append('Content-Length: %d\r\n' % len(body))
        head.append('X-GitHub-Request-Id: %s\r\n' % new_request_id())
        for key, value in response.headers.items():
            head.append('%s: %s\r\n' % (key, value))
        head.append('Connection: %s\r\n'
                    % ('close' if self.close_connection else 'keep-alive'))
        head.append('\r\n')
        # One write for the whole response: a second write doubles the
        # syscalls on the small responses the benchmarks hammer.
        try:
            self.wfile.write(''.join(head).encode('iso-8859-1') + body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def get_request(self):
        # Disable Nagle on accepted sockets: without TCP_NODELAY a small
        # request/response pair on a keep-alive connection stalls on the
        # delayed-ACK timer (~40 ms per round trip), which dominates the
        # sequential benchmark workloads.
        import socket
        sock, addr = super().get_request()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock, addr

    def __init__(self, address, forge):
        self.forge = forge
        super().__init__(address, _Handler)


def make_server(port=0):
    forge = Forge()
    # Bind all interfaces (like Node's listen(0)): the env dials the pod
    # address the register request came from, which is not loopback.
    server = ForgeServer(('0.0.0.0', port), forge)
    return server, forge
