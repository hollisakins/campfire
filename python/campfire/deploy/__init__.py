"""CAMPFIRE deployment — deploys pipeline products to Supabase + Cloudflare R2."""

import click


def require_pipeline(feature):
    """Import guard for code paths that reuse ``campfire_pipeline`` (the
    ``campfire[deploy]`` extra). Deploy machines always carry the pipeline —
    there's no use deploying data you can't reduce — but pip extras aren't
    tracked at runtime, so this turns the raw ImportError into instructions.
    """
    try:
        import campfire_pipeline  # noqa: F401
    except ImportError as e:
        raise click.ClickException(
            f"{feature} reuses the reduction pipeline, which isn't installed "
            "in this environment. Install the deploy profile: from the repo "
            "root, pip install -e ./layout -e ./pipeline -e './python[deploy]' "
            "(or re-run install.py with the Deployment profile selected)."
        ) from e
