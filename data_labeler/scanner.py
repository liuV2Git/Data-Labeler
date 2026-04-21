"""Filesystem scanning helpers for Story 1 folder intake."""

from __future__ import annotations

import os
from pathlib import Path

from data_labeler.models import ScanResult

SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SCHEMA_FILE_PREFIX = "schema"
CATEGORIES_FILE_NAME = "categories.json"
LABELS_FILE_NAME = "labels.json"


def scan_folder(root_path: Path) -> ScanResult:
    """Scans the selected folder for supported content.

    The scan is intentionally lightweight for Story 1. It recursively discovers
    supported document files, schema files, and shared support files used by the
    later workflow.

    Args:
        root_path: Folder selected by the user.

    Returns:
        A ``ScanResult`` containing discovered files and non-fatal warnings.
    """

    result = ScanResult(root_path=root_path)

    def on_error(error: OSError) -> None:
        """Collects non-fatal filesystem traversal errors."""

        result.warnings.append(str(error))

    for current_root, _, file_names in os.walk(root_path, onerror=on_error):
        current_path = Path(current_root)

        for file_name in sorted(file_names):
            path = current_path / file_name
            normalized_name = file_name.lower()
            suffix = path.suffix.lower()

            if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
                result.documents.append(path)
                continue

            if normalized_name.startswith(SCHEMA_FILE_PREFIX) and suffix == ".json":
                result.schema_files.append(path)
                continue

            if normalized_name == CATEGORIES_FILE_NAME and result.categories_file is None:
                result.categories_file = path
                continue

            if normalized_name == LABELS_FILE_NAME and result.labels_file is None:
                result.labels_file = path

    result.documents.sort()
    result.schema_files.sort()
    return result


def format_relative_paths(paths: list[Path], root_path: Path) -> list[str]:
    """Formats paths for display relative to the selected root.

    Args:
        paths: Absolute paths that should be rendered in the UI.
        root_path: Selected root folder.

    Returns:
        A list of user-friendly relative path strings.
    """

    formatted_paths: list[str] = []
    for path in paths:
        try:
            formatted_paths.append(str(path.relative_to(root_path)))
        except ValueError:
            formatted_paths.append(str(path))
    return formatted_paths
