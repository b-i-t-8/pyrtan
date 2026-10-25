# Pyrtan trees

The catalog behind `pyrtan search` and `pyrtan pull`: trees people wrote
for [Pyrtan](https://github.com/b-i-t-8/pyrtan), checked by CI and
published as static files at <https://pyrtan.com/catalog/>.

A tree is one TOML file that says which sentences it answers to, which
hosts it may touch, what it observes, and exactly how it decides. No model
is involved; see the Pyrtan README for the format.

## Using a tree

```
pyrtan search weather
pyrtan pull weather --dir trees
pyrtan serve trees
```

A pull verifies the file against this catalog's index before writing it.

## Adding a tree

1. Fork, then `pyrtan new my-tree --dir trees/my-tree` and write it. Keep
   `hosts` to exactly what it needs, and set `license` (an SPDX
   identifier; `MIT`, `Apache-2.0` or `CC0-1.0`). CI refuses a tree
   without one.
2. `pyrtan check trees/my-tree` until it is clean.
3. Record its responses for the tests:
   `pyrtan run "the sentence" --trees trees/my-tree --record trees/my-tree/fixture.json`.
   Trim the recording to what the test needs; never commit a credential.
4. Add a test under `tests/` that replays the sentence against the fixture
   and asserts the `say`, the `output` and the branches taken.
5. Open a pull request. CI runs the checker, the index build and the
   tests; a maintainer reads the tree.

To change a tree others may have pulled, bump its `version`.

## Mirroring

The catalog is `index.json` and the files it names. Copy the published
site anywhere and point `pyrtan --catalog` (or `$PYRTAN_CATALOG`) at it.

## Licences

Each tree carries its own licence in the file. The repository's own files
(this README, the workflow, the tests) are under the licence in `LICENSE`.
