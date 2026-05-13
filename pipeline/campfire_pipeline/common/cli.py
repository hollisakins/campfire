"""Shared Click utilities for cfpipe CLIs."""

import click


class VariadicOption(click.Option):
    """Click option that consumes multiple space-separated values after a single flag.

    Usage::

        @click.option('--filters', multiple=True, cls=VariadicOption, ...)

    Lets users write ``--filters f444w f200w f115w`` instead of repeating
    the flag (``--filters f444w --filters f200w --filters f115w``).
    """

    def add_to_parser(self, parser, ctx):
        super().add_to_parser(parser, ctx)
        name = self.opts[-1]
        opt = parser._long_opt.get(name)
        if opt is None:
            return
        original_process = opt.process

        def _eat_remaining(value, state):
            original_process(value, state)
            while state.rargs and not state.rargs[0].startswith('-'):
                original_process(state.rargs.pop(0), state)

        opt.process = _eat_remaining
