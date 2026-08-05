# Marks ``api`` as a package so endpoint handlers (e.g. ``api.runs_post``)
# can resolve their ``from .._shared import ...`` relative imports when the
# local web server imports them.
