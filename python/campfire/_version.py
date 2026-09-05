"""Single source of the client release version.

Import-free on purpose: ``campfire/__init__.py`` imports the API modules before
it would otherwise define ``__version__``, so anything below the package root
(the HTTP session's User-Agent, the CLI's ``--version``) reads it from here.
Bump together with ``python/pyproject.toml`` and the ``/api/v1/version`` floor.
"""

__version__ = "0.5.0"
