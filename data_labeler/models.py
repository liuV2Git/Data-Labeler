"""Data models for the Data Labeler application."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ScanResult:
    """Represents the result of scanning a selected folder.

    Attributes:
        root_path: Root folder selected by the user.
        support_root: Folder supplying shared schema and config files.
        documents: Supported documents discovered recursively.
        schema_files: Schema files discovered in the support folder.
        categories_file: Path to the categories configuration, if present.
        labels_file: Path to the labels output file, if present.
        warnings: Non-fatal scanning warnings collected during discovery.
    """

    root_path: Path
    support_root: Path | None = None
    documents: list[Path] = field(default_factory=list)
    schema_files: list[Path] = field(default_factory=list)
    categories_file: Path | None = None
    labels_file: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def document_count(self) -> int:
        """Returns the number of discovered documents."""

        return len(self.documents)

    @property
    def schema_count(self) -> int:
        """Returns the number of discovered schema files."""

        return len(self.schema_files)
