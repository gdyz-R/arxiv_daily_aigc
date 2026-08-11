"""Backward-compatible rendering facade."""

from __future__ import annotations

import logging
import os

try:
    from render import render_json_file
except ImportError:  # pragma: no cover
    from .render import render_json_file


def generate_html_from_json(
    json_file_path: str,
    template_dir: str,
    template_name: str,
    output_dir: str,
):
    del template_dir, template_name
    stem = os.path.splitext(os.path.basename(json_file_path))[0].replace("-", "_")
    output_path = os.path.join(output_dir, f"{stem}.html")
    try:
        return render_json_file(json_file_path, output_path)
    except Exception as exc:
        logging.error("Failed to render %s: %s", json_file_path, exc, exc_info=True)
        return None
