"""Pyrtan: a core set of expandable services that act on all forms of I/O through inspectable
decision trees.

The public surface is small on purpose:

- :func:`pyrtan.load_tree` reads one tree file.
- :class:`pyrtan.Registry` holds the trees a service knows about.
- :class:`pyrtan.Runner` executes a tree against a transport and returns an
  :class:`pyrtan.Outcome` with a full :class:`pyrtan.Trace`.
- :func:`pyrtan.grow.propose` turns a sentence nobody answers into the tree
  that would.
"""

from .tree import Tree, load_tree, load_tree_text, TreeError
from .registry import Registry
from .runtime import Runner, Outcome
from .trace import Trace, Step
from .transport import Transport, UrllibTransport, RecordedTransport, Response

__all__ = [
    "Tree",
    "TreeError",
    "load_tree",
    "load_tree_text",
    "Registry",
    "Runner",
    "Outcome",
    "Trace",
    "Step",
    "Transport",
    "UrllibTransport",
    "RecordedTransport",
    "Response",
]

__version__ = "0.35.0"
