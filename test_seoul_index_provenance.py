"""seoul_index_provenance.PROVENANCE stays whole: one entry for every card
category the bot can emit, every field filled, and a 'build' check claimed
only where the harvester that emits the category really calls one.
Run by path or by discovery."""
import ast
import inspect
import re
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import seoul_index_post as S
from seoul_index_provenance import PROVENANCE

HERE = Path(__file__).parent
FIELDS = ('source', 'counts', 'checked_against', 'complete_fetch', 'labels', 'verified')


def emitted_categories():
    """Every category a fact() call in the bot names: the second argument,
    when it is a string literal. Read from the syntax tree, so a category in
    a comment or a docstring does not count."""
    tree = ast.parse((HERE / 'seoul_index_post.py').read_text())
    cats = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'fact'
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                cats.setdefault(node.args[1].value, set()).add(fn.name)
    return cats


def calls_a_check(fn_name, seen=None, depth=0):
    """True if fn_name, or a module function it calls by name (to depth 4),
    calls require(), reconcile(), check_failed() or a @source_check helper.
    Proves a check sits on the path, not that it is the one the entry names."""
    seen = seen if seen is not None else set()
    if fn_name in seen or depth > 4 or not hasattr(S, fn_name):
        return False
    seen.add(fn_name)
    try:
        tree = ast.parse(inspect.getsource(getattr(S, fn_name)).lstrip())
    except (TypeError, OSError, SyntaxError):
        return False
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    if names & {'require', 'reconcile', 'check_failed'}:
        return True
    return any(calls_a_check(n, seen, depth + 1) for n in names if n != fn_name)


def callees(fn_name):
    """Module functions fn_name calls by name."""
    if not hasattr(S, fn_name):
        return set()
    try:
        tree = ast.parse(inspect.getsource(getattr(S, fn_name)).lstrip())
    except (TypeError, OSError, SyntaxError):
        return set()
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and hasattr(S, n.func.id)}


def reachable(fn_name, depth=5):
    out, frontier = {fn_name}, {fn_name}
    for _ in range(depth):
        frontier = {c for f in frontier for c in callees(f)} - out
        out |= frontier
    return out


def harvesters():
    """The harvesters the bot runs inside guarded(), by its first argument:
    build_pool()'s, and main()'s spotlight. A harvester called any other way
    could raise SourceCheckFailed straight through the run."""
    tree = ast.parse(inspect.getsource(S.build_pool) + '\n' + inspect.getsource(S.main))
    return {n.args[0].id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'guarded'
            and n.args and isinstance(n.args[0], ast.Name)}


# Categories whose 'build' checks run in a separate monthly harvest script,
# not in the bot: the test reads that script for a refusal instead.
HARVEST_SCRIPTS = {'spending': 'seoul_index_sales.py', 'avgbill': 'seoul_index_sales.py',
                   'books': 'seoul_index_books_harvest.py'}


class Provenance(unittest.TestCase):
    def test_one_entry_for_every_category_and_no_more(self):
        self.assertEqual(set(PROVENANCE), set(emitted_categories()))

    def test_every_field_is_filled(self):
        for cat, e in PROVENANCE.items():
            for f in FIELDS:
                self.assertTrue(str(e.get(f, '')).strip(), f'{cat}: {f} empty')
            date.fromisoformat(e['verified'])

    def test_every_entry_says_how_it_is_checked(self):
        for cat, e in PROVENANCE.items():
            self.assertTrue(e['checks'], f'{cat}: no checks listed')
            for ch in e['checks']:
                self.assertIn(ch['kind'], ('RECONCILE', 'SHAPE'), cat)
                self.assertIn(ch['when'], ('build', 'audit', 'none'), cat)
                self.assertTrue(ch['what'].strip(), cat)

    def test_a_build_check_is_really_on_the_harvesters_path(self):
        cats = emitted_categories()
        for cat, e in PROVENANCE.items():
            if not any(ch['when'] == 'build' for ch in e['checks']):
                continue
            if cat in HARVEST_SCRIPTS:
                src = (HERE / HARVEST_SCRIPTS[cat]).read_text()
                self.assertRegex(src, r"sys\.exit\(f?'Refusing to write", cat)
                if cat == 'books':
                    continue
            # The harvesters that reach the functions emitting this category.
            hs = {h for h in harvesters() if reachable(h) & cats[cat]}
            self.assertTrue(hs, f'{cat}: no guarded() harvester reaches it')
            self.assertTrue(any(calls_a_check(h) for h in hs),
                            f'{cat}: a build check is claimed but none of '
                            f'{sorted(hs)} calls one')

    def test_held_categories_say_so(self):
        for cat in S.HELD_CATS:
            self.assertIn('HELD', PROVENANCE[cat]['labels'], cat)


    def test_no_check_raising_harvester_runs_unguarded(self):
        """Every direct call of a *_facts harvester in main() goes through
        guarded(), so a failed check withholds a card and never stops a run."""
        tree = ast.parse(inspect.getsource(S.main))
        bare = [n.func.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id.endswith('_facts')]
        self.assertEqual(bare, [], f'unguarded harvester calls in main(): {bare}')


if __name__ == '__main__':
    unittest.main()
