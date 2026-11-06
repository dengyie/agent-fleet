"""Neutral code shared by the hub (``hub``) and node-side tools (``tools``).

Dependency direction: ``hub -> fleet_core <- tools``.  Modules here must not
import ``hub``, ``tools`` or ``connectors`` (root schema modules are allowed);
``tests/test_boundaries.py`` enforces this.  Kept a regular package (explicit
``__init__``) so a same-named namespace package elsewhere on ``sys.path`` can
never shadow it, mirroring ``tools/__init__.py``.
"""
