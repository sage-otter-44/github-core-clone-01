"""Entry point: bind an ephemeral port, register with the env, wait for
done, exit 0. The lifecycle the kit's starter settles (starter/main.ts).

Without MAKEFUN_ENV_URL there is nothing to register with, so the
program exits 1 — unless --port is given, in which case it serves
standalone (local testing only).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from server import make_server

REQUEST_TIMEOUT_S = 30
RETRY_INITIAL_S = 0.25
RETRY_MAX_S = 2.0


def _retry_delay(attempt):
    return min(RETRY_MAX_S, RETRY_INITIAL_S * 2 ** min(attempt, 8))


def _http(method, url, payload=None, timeout=REQUEST_TIMEOUT_S):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['content-type'] = 'application/json'
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def register(env_url, port):
    """POST the bound port until the env accepts it (200 or 409)."""
    for attempt in range(100000):
        try:
            status, _body = _http('POST', env_url + '/register',
                                  {'port': port})
            if status in (200, 409):
                return
            if status in (400, 405):
                raise RuntimeError('register answered HTTP %d' % status)
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(_retry_delay(attempt))
    raise RuntimeError('register never succeeded')


def wait_for_done(env_url):
    """Poll GET /done until it answers {"done": true}."""
    for attempt in range(100000):
        try:
            status, body = _http('GET', env_url + '/done')
            if status == 200:
                parsed = json.loads(body.decode('utf-8'))
                if parsed.get('done') is True:
                    return
        except Exception:
            pass
        time.sleep(_retry_delay(attempt))
    raise RuntimeError('the env never said done')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0,
                        help='standalone port (local testing only)')
    parser.add_argument('--standalone', action='store_true',
                        help='serve forever without an env')
    args = parser.parse_args()

    env_url = (os.environ.get('MAKEFUN_ENV_URL') or '').rstrip('/')
    server, _forge = make_server(args.port)
    port = server.server_address[1]
    sys.stdout.write('[entry] listening on port %d\n' % port)
    sys.stdout.flush()

    if env_url == '':
        if not args.standalone and args.port == 0:
            sys.stderr.write('[entry] MAKEFUN_ENV_URL is not set: '
                             'there is no env to register with\n')
            server.server_close()
            return 1
        sys.stdout.write('[entry] standalone mode on port %d\n' % port)
        sys.stdout.flush()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        register(env_url, port)
        sys.stdout.write('[entry] registered; waiting for the env to say done\n')
        sys.stdout.flush()
        wait_for_done(env_url)
    finally:
        server.shutdown()
        server.server_close()
    sys.stdout.write('[entry] done\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
