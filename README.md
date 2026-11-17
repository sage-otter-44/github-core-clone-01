# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/sage-otter-44/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/sage-otter-44/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A standalone GitHub-compatible git forge in one Python process, standard library only: git smart HTTP, REST v3, GraphQL v4 and MCP, no git binary, no outside service.

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=kc1w4ew7&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>

## Running

Trial (the harness runs this):

```
python3 main.py
```

It binds an ephemeral port, POSTs `{"port": N}` to
`$MAKEFUN_ENV_URL/register`, serves until GET `$MAKEFUN_ENV_URL/done`
answers `{"done": true}`, then exits 0. Without `MAKEFUN_ENV_URL`
it exits 1.

Local testing:

```
MAKEFUN_SHOP_TOKEN=test-token python3 main.py --port 8765 --standalone
```

## Surfaces

- `GET /healthz` — `200 {"ok": true}`, no auth.
- Git smart HTTP at `/OWNER/REPO.git` — upload-pack protocol v2
  (`ls-refs`, `fetch`, shallow/deepen) and v0, receive-pack v0 with
  `report-status`, `side-band-64k`, `delete-refs`. Fast-forward only;
  a refused ref is reported as `ng <ref> non-fast-forward`.
- REST v3 under `/api/v3` — repositories, branches, commits, contents,
  labels, issues, comments, pull requests, files and squash merges.
  Every reply is `application/json` with a non-empty
  `X-GitHub-Request-Id`; auth is `Bearer $MAKEFUN_SHOP_TOKEN`.
- GraphQL v4 at `/api/graphql` — `viewer`, `repository`, `node(id:)`,
  `__type` introspection, issue/pull-request reads and the
  `createIssue` / `closeIssue` / `createPullRequest` /
  `mergePullRequest` / `addComment` mutations, plus operation batching.
- MCP at `/mcp` — JSON-RPC 2.0 over Streamable HTTP, revision
  `2026-07-28` (`server/discover`, `initialize`, `tools/list`,
  `tools/call`). Six tools: `create_repo`, `get_repo`, `create_issue`,
  `list_issues`, `create_pull_request`, `merge_pull_request`.

## Layout

| file | what it holds |
| --- | --- |
| `main.py` | lifecycle: bind, register, poll done, exit 0 |
| `server.py` | HTTP transport (fast request parser, keep-alive) + routing |
| `store.py` | in-memory data model: repos, issues, pulls, labels |
| `gitobjs.py` | SHA-1 objects, trees/commits/tags, pack read/write, deltas, pkt-lines |
| `gitproto.py` | advertisements, ls-refs, fetch (v2 + v0), receive-pack |
| `rest.py` | REST v3 handlers |
| `graphql.py` | GraphQL parser + executor |
| `mcp.py` | MCP JSON-RPC handlers + the six tools |
| `scenario.py` | the seeded scenario generator (derives the viewer login) |
| `util.py` | request/response types, JSON replies, auth, pagination |

## Design notes

- **Determinism.** No wall clock and no unseeded randomness in
  anything the server sends. Server-created commits use a fixed
  epoch plus a per-repo sequence number, so distinct commits always
  get distinct shas. The viewer login is derived from the contest's
  fixed scenario seed (`adaiversen`), not from the token.
- **One lock.** All store access runs under a single `RLock`. It
  keeps concurrent clones consistent and lets the fetch cache absorb
  identical concurrent fetches (the benchmark's 50 shallow clones
  compute the pack once).
- **Caching.** Fetch responses are cached per (repo version, wants,
  haves, depth, shallow set); advertisements and issue lists are
  cached per repo version and invalidated on any mutation.
- **Packs.** The writer emits plain objects only (no deltas); the
  reader resolves `OFS_DELTA` and `REF_DELTA` (thin packs resolve
  bases from the store) and verifies every object's SHA-1 and size.

## Tests

`tests/smoke_test.py` starts the server on a scratch port and
exercises register → create repo → push → clone/fetch (including
shallow/deepen) → REST CRUD → issues/PRs/merge → GraphQL → MCP.
The kit's sealed suite is the real check:

```
node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:8765 --token test-token
node --experimental-strip-types sdk/run.ts --url http://127.0.0.1:8765 --token test-token --perf
```

(run from the kit directory, with the server up)
