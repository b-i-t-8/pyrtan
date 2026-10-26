# Contributing

Pyrtan grows mostly by trees, and a tree is a text file. If a tree you want
does not exist, write it; if one gives a wrong answer, the trace shows where.

## Adding or fixing a tree

1. `pyrtan new --from "the sentence it answers" --dir examples/my-tree`
   (or `pyrtan new my-tree`) and edit the file. If a running swarm has
   already been asked the sentence, `pyrtan unanswered --at URL --scaffold
   DIR` writes the same file. Keep the `hosts` list to exactly the hosts it
   needs.
2. `pyrtan check examples/my-tree` until it is clean. Warnings are worth
   reading; an `info` about a `decide` without `else` is a choice, not a
   fault.
3. Record responses for the tests:
   `pyrtan run "the sentence" --trees examples --record tests/fixtures/my-tree.json`.
   Trim the recording to what the test needs, and never commit anything
   with a credential in it.
4. Add a test in `tests/test_examples.py` that runs the sentence against the
   recording and asserts the `say`, the `output`, and the branches taken.
5. `uv run pytest`.

A tree that reads a page should prefer an API or a feed when the site offers
one, and should decide on the status code before it decides on the body.

A tree meant for the catalog at pyrtan.com carries a `license` line (an
SPDX identifier; `MIT`, `Apache-2.0` or `CC0-1.0` are the usual choices)
and a `namespace` line (who publishes it; the catalog id is
`namespace/name`). The repository's own examples are the `pyrtan`
namespace. Everyone else publishes under `catalog/<namespace>/`:

    pyrtan publish my-tree.toml --into . --owner <your GitHub login>
    git checkout -b catalog/<namespace>-my-tree && git add catalog && git commit && git push -u origin HEAD

then open a pull request. The `catalog` workflow runs the gate
(`site/gate.py`): the tree loads clean, has a licence and a namespace,
sits in its namespace's directory under its own name, no other tree has
its id, only the namespace's `OWNERS` change it, and a changed file bumps
its `version`. A test and a recording are welcome but not required for
a namespace of your own; the gate is the review. A maintainer merges,
and the `pages` workflow publishes it at <https://pyrtan.com/catalog/>
the same day. See [`catalog/README.md`](catalog/README.md) and
[`site/README.md`](site/README.md).

Once it is out there, `pyrtan reports <namespace>/my-tree` shows the gaps
hubs that opted in have met with it: the node, the value no branch
covered, how often. That is the list of branches to write next, and
`pyrtan grow --reports --trees . --apply` drafts each one into the file
by rules (the range of the hole a number fell into, `{ eq = … }`
otherwise, a `fail` node to finish); you say what the value means and
bump the version.

## Changing the runtime

The runtime is small on purpose, and every node kind is paid for by every
tree author who has to learn it. A new kind needs a case that `observe`,
`decide`, `set`, `each` and `delegate` cannot express together, written down
in the pull request. The same goes for a new filter or condition.

Nothing in `src/pyrtan` may call a model, execute code from a tree, or reach
a host outside a tree's `hosts`. Those are the three rules the design rests
on; see `_meta/design.md`. A fourth, from `_meta/platform.md`: nothing in
`src/pyrtan` knows about tenants, plans or billing. The hosted product runs
this code unchanged.

## License of contributions

Pyrtan is AGPL-3.0-or-later. By opening a pull request you agree that your
contribution is licensed under the same terms.

## Style

Python 3.12+, standard library only at run time. Tests are pytest. A
docstring at the top of each module says what it is for and what it refuses.
Commit messages say what changed and why, in the imperative.
