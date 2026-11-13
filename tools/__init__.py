# Regular-package anchor for ``tools``.
#
# Hermes' container image ships its own regular ``tools`` package in
# ``site-packages``. Without this file the release's ``tools`` would be a
# PEP 420 namespace package, and the regular package wins resolution, so
# ``import tools.session`` fails with ModuleNotFoundError even though the
# release files exist. Keeping an explicit ``__init__.py`` makes the
# release-root package regular, and regular packages earlier on ``sys.path``
# win over later ones.
