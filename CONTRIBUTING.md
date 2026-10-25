# Contributing

Pyrtan grows mostly by trees, and a tree is a text file. If a tree you want
does not exist, write it; if one gives a wrong answer, the trace shows where.

## Adding or fixing a tree

1. `pyrtan new my-tree --dir examples/my-tree` and edit the file. Keep the
   `hosts` list to exactly the hosts it needs.
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

## Changing the runtime

The runtime is small on purpose, and every node kind is paid for by every
tree author who has to learn it. A new kind needs a case that `observe`,
`decide`, `set`, `each` and `delegate` cannot express together, written down
in the pull request. The same goes for a new filter or condition.

Nothing in `src/pyrtan` may call a model, execute code from a tree, or reach
a host outside a tree's `hosts`. Those are the three rules the design rests
on; see `_meta/design.md`.

## License of contributions

Pyrtan is AGPL-3.0-or-later. By opening a pull request you agree that your
contribution is licensed under the same terms.

## Style

Python 3.12+, standard library only at run time. Tests are pytest. A
docstring at the top of each module says what it is for and what it refuses.
Commit messages say what changed and why, in the imperative.
