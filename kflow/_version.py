"""Single source of truth for the package version.

This tiny module exists so that every place that needs the version
(``kflow.core``, ``kflow.cli``'s ``--version`` flag, ``kflow.__init__``) imports
the *same* literal. ``bump_version.py`` rewrites the string below; importing it
everywhere keeps ``kflow --version`` from drifting behind a hardcoded copy.
"""

__version__ = "v1.2.1"
