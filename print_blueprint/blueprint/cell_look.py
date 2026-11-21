"""Read a cell or dashboard look from core, or a browser's resolved semantic tokens.

The host does not pass look tokens to fetch(). Saved placements are resolved through
its page/theme/font stores; client.js supplies live CSS values for unsaved previews.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from urllib.parse import urlsplit


def normalise(values: dict | None) -> dict | None:
    if not isinstance(values, dict):
        return None
    out = {}
    for key in ('bg', 'ink', 'rule', 'accent'):
        value = str(values.get(key) or '')
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            return None
        out[key] = value.lower()
    out['font'] = str(values.get('font') or '')[:200]
    return out


def font_path(family: str) -> str:
    """Only files belonging to a registered font; never accept a path or URL from a client."""
    try:
        from flask import current_app
        registry = current_app.config['PLUGIN_REGISTRY']
        fonts = list(registry.fonts.values())
        for name in family.split(','):
            name = name.strip().strip('\"\'')
            font = next((f for f in fonts if name.lower() in (f.id.lower(), f.name.lower())), None)
            if font is None:
                from app.composer import _cached_font
                font = _cached_font(name)
            if font is None:
                continue
            url = font.files.get('400') or next(iter(font.files.values()), '')
            path = urlsplit(url).path
            if path.startswith('/plugins/'):
                _, _, plugin_id, relative = path.split('/', 3)
                root = Path(registry.get(plugin_id).path).resolve()
                candidate = (root / relative).resolve()
            elif path.startswith('/page-fonts/'):
                from app import font_cache
                _, _, slug, relative = path.split('/', 3)
                root = font_cache.font_dir(Path(current_app.config['DATA_ROOT']), slug).resolve()
                candidate = (root / relative).resolve()
            else:
                continue
            if candidate.is_relative_to(root) and candidate.is_file():
                from PIL import ImageFont
                ImageFont.truetype(str(candidate), 20)
                return str(candidate)
    except (ImportError, RuntimeError, KeyError, AttributeError, OSError, ValueError):
        pass
    return ''


def apply(opts, look: dict | None):
    look = normalise(look)
    if look is None:
        return opts
    return dataclasses.replace(opts, background='blue', palette=tuple(look[k] for k in ('bg', 'ink', 'rule', 'accent')),
                               font_path=font_path(look['font']))


def _saved_cell(ctx: dict, options: dict):
    from flask import current_app, has_request_context, request
    placement = str(ctx.get('placement_id') or '').split('@', 1)[0]
    page_id, _, cell_id = placement.partition('/')
    if has_request_context():
        args = request.view_args or {}
        page_id = page_id or str(args.get('page_id') or '')
        cell_id = cell_id or str(ctx.get('sid') or args.get('cell_id') or '')
    store = current_app.config.get('PAGE_STORE')
    page = store.get(page_id) if store and page_id else None
    if page is None:
        return None, None
    cells = [c for c in page.cells if c.plugin == 'print_blueprint']
    if cell_id:
        cell = next((c for c in cells if c.id == cell_id), None)
    else:
        matching = [c for c in cells if all(c.options.get(k, v) == v for k, v in options.items())]
        cell = matching[0] if len(matching) == 1 else cells[0] if len(cells) == 1 else None
    return page, cell


def saved(ctx: dict, options: dict) -> dict | None:
    """Inherit the dashboard only when Paper requests it; explicit cell looks always win."""
    try:
        from flask import current_app
        from app.state.theme_registry import bundled_theme_colours
        page, cell = _saved_cell(ctx, options)
        if page is None:
            return None
        if cell is None and options.get('background') != 'dashboard':
            return None
        if options.get('background') != 'dashboard' and not any((cell.theme, cell.style, cell.font)):
            return None
        theme_id = (cell.theme if cell else None) or page.theme or 'light'
        tokens = dict(bundled_theme_colours(theme_id))
        family = ''
        for key in ('USER_THEMES_STORE', 'COMMUNITY_THEMES_STORE'):
            store = current_app.config.get(key)
            theme = store.get(theme_id) if store else None
            if theme is not None:
                tokens.update(dataclasses.asdict(theme))
                family = str(tokens.get('font_family') or '')
                if hasattr(theme, 'css_path'):
                    from app.state.theme_registry import _parse_theme_blocks
                    block = _parse_theme_blocks(theme.css_path.read_text()).get(theme_id, {})
                    tokens.update({k.removeprefix('--').replace('-', '_'): v for k, v in block.items()
                                   if re.fullmatch(r'#[0-9a-fA-F]{6}', v)})
                    family = block.get('--font-family', family)
        registry = current_app.config['PLUGIN_REGISTRY']
        style = (cell.style if cell else None) or page.style or 'standard'
        # Core styles expose a font family as CSS; read that same declaration.
        from app import composer
        css = (Path(composer.__file__).resolve().parents[1] / 'static/style/spectra-styles.css').read_text()
        block = re.search(r'\[data-style="' + re.escape(style) + r'"\]\s*\{([^}]+)\}', css)
        face = re.search(r'--font-family:\s*([^;]+);', block[1]) if block else None
        if face:
            family = face[1]
        font_id = (cell.font if cell else None) or page.font
        if font_id:
            from app.composer import _resolve_font
            font = _resolve_font(font_id, registry)
            if font:
                family = font.name
        defaults = bundled_theme_colours('light')
        def token(key):
            return str(tokens.get(key) or defaults[key])
        return normalise(dict(bg=token('surface'), ink=token('text_primary'), rule=token('text_secondary'),
                              accent=token('accent_4'), font=family))
    except (ImportError, RuntimeError, KeyError, AttributeError, OSError, ValueError):
        return None
