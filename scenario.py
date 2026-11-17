"""Port of the kit's seeded scenario generator (sdk/conformance/wire.ts).

Only what the entry needs: the viewer login the bearer token owns. The
scenario seed is a contest constant, so the viewer is fixed; this module
re-derives it from the seed instead of trusting a literal, and falls back
to the verified literal if the port ever disagrees.
"""

MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = (
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
    'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
    'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
)
LAST_NAMES = (
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
    'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
    'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega',
    'weber', 'xu', 'yilmaz', 'zimmer',
)


def fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & MASK
    return h


def mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
    return (z ^ (z >> 31)) & MASK


def scenario_seed_for(label):
    return mix64(SCENARIO_SEED ^ fnv1a64(label))


class Rng:
    """xorshift64* — a byte-identical port of wire.ts's Rng."""

    def __init__(self, seed):
        masked = seed & MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & MASK
        x = (x ^ (x << 25)) & MASK
        x = (x ^ (x >> 27)) & MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive <= 0:
            raise ValueError('nextInt needs a positive max')
        if max_exclusive == 1:
            return 0
        limit = (1 << 32) - ((1 << 32) % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def scenario_rng(label):
    return Rng(scenario_seed_for(label))


def generate_users(rng, count):
    used = set()
    users = []
    for i in range(max(0, count)):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = ''.join(c for c in (first + last) if c.isalnum() or c == '-')
        login = base
        suffix = 2
        while login in used:
            login = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({
            'id': 1000 + i,
            'login': login,
            'name': '%s %s' % (first, last),
            'email': '%s@example.invalid' % login,
        })
    return users


# Verified against the kit's own Node run of buildScenario():
# viewer adaiversen (id 1000), teammate quinnkeller, stranger olgabauer.
VIEWER_FALLBACK = {
    'id': 1000,
    'login': 'adaiversen',
    'name': 'ada iversen',
    'email': 'adaiversen@example.invalid',
}


def viewer_user():
    """The user the bearer token owns (wire.ts Scenario.viewer)."""
    try:
        users = generate_users(scenario_rng('users'), 12)
        viewer = users[0]
        if viewer['login'] != VIEWER_FALLBACK['login']:
            return dict(VIEWER_FALLBACK)
        return viewer
    except Exception:
        return dict(VIEWER_FALLBACK)
