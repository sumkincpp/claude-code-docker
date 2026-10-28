"""Renderers: plugins that turn the selected features into build input. The kernel (ccd.features) knows none of them.

Built-in renderers are listed here; others come from the `ccd.renderers` entry-point group, as backends do.
"""

from importlib import import_module
from importlib.metadata import entry_points

from ccd.features.render.base import RenderOptions, Renderer, RendererError
from ccd.features.render.dialect import INSTALLED_FILE

__all__ = ["DEFAULT_RENDERER", "INSTALLED_FILE", "RenderOptions", "Renderer", "RendererError", "available_renderers", "get_renderer"]

ENTRY_POINT_GROUP = "ccd.renderers"
DEFAULT_RENDERER = "parallel"
BUILTIN_RENDERERS = {
    "parallel": "ccd.features.render.parallel:ParallelRenderer",
    "sequential": "ccd.features.render.sequential:SequentialRenderer",
}


def _builtin(value: str) -> type[Renderer]:
    module, _, attr = value.partition(":")
    return getattr(import_module(module), attr)


def _installed() -> dict:
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def available_renderers() -> dict[str, Renderer]:
    """Every renderer by name. Installed entry points win over built-ins."""
    found = {name: _builtin(value)() for name, value in BUILTIN_RENDERERS.items()}
    found.update({name: ep.load()() for name, ep in _installed().items()})
    return found


def get_renderer(name: str | None = None, options: RenderOptions | None = None) -> Renderer:
    """The renderer called `name` (the default one without a name); only that one is loaded."""
    name = name or DEFAULT_RENDERER
    installed = _installed()
    if name in installed:
        return installed[name].load()(options)
    if name in BUILTIN_RENDERERS:
        return _builtin(BUILTIN_RENDERERS[name])(options)
    raise RendererError(f"Invalid build mode {name!r} (expected one of: {', '.join(sorted({*BUILTIN_RENDERERS, *installed}))})")
