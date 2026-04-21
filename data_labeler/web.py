"""Local web application for Story 2 schema selection and preview."""

from __future__ import annotations

import json
import subprocess
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from data_labeler.models import ScanResult
from data_labeler.scanner import (
    format_relative_paths,
    get_default_active_schema,
    scan_folder,
)

HOST = "127.0.0.1"
PORT = 8765


@dataclass
class AppState:
    """Stores mutable application state for the local web app.

    Attributes:
        selected_folder: Folder currently selected by the user.
        scan_result: Most recent scan result.
        active_schema: Schema currently selected by the user.
        status_message: Short UI status text.
    """

    selected_folder: Path | None = None
    scan_result: ScanResult | None = None
    active_schema: Path | None = None
    status_message: str = (
        "Choose a folder to discover documents and support files."
    )


def _serialize_scan_result(result: ScanResult | None) -> dict[str, Any]:
    """Converts a scan result into JSON-safe browser data.

    Args:
        result: Current scan result or ``None`` before a folder is chosen.

    Returns:
        A dictionary that can be encoded as JSON for the browser UI.
    """

    if result is None:
        return {
            "selectedFolder": None,
            "supportFolder": None,
            "documents": [],
            "schemaFiles": [],
            "categoriesFile": None,
            "labelsFile": None,
            "warnings": [],
            "documentCount": 0,
            "schemaCount": 0,
        }

    support_root = result.support_root or result.root_path
    return {
        "selectedFolder": str(result.root_path),
        "supportFolder": str(support_root),
        "documents": format_relative_paths(result.documents, result.root_path),
        "schemaFiles": format_relative_paths(result.schema_files, support_root),
        "categoriesFile": (
            str(result.categories_file.relative_to(support_root))
            if result.categories_file
            else None
        ),
        "labelsFile": (
            str(result.labels_file.relative_to(support_root))
            if result.labels_file
            else None
        ),
        "warnings": result.warnings,
        "documentCount": result.document_count,
        "schemaCount": result.schema_count,
    }


def _read_schema_preview(schema_path: Path | None) -> dict[str, Any]:
    """Reads the active schema into preview-friendly browser data.

    Args:
        schema_path: Currently active schema path.

    Returns:
        A dictionary with preview text and optional preview error.
    """

    if schema_path is None:
        return {
            "activeSchema": None,
            "activeSchemaPath": None,
            "schemaPreview": "No active schema selected.",
            "schemaPreviewError": None,
        }

    try:
        contents = schema_path.read_text(encoding="utf-8")
        parsed = json.loads(contents)
        preview = json.dumps(parsed, indent=2, ensure_ascii=True)
        return {
            "activeSchema": schema_path.name,
            "activeSchemaPath": str(schema_path),
            "schemaPreview": preview,
            "schemaPreviewError": None,
        }
    except Exception as error:  # noqa: BLE001
        return {
            "activeSchema": schema_path.name,
            "activeSchemaPath": str(schema_path),
            "schemaPreview": "",
            "schemaPreviewError": str(error),
        }


def _serialize_state(state: AppState) -> dict[str, Any]:
    """Converts full application state into JSON-safe browser data.

    Args:
        state: Shared application state.

    Returns:
        Serialized application state for browser rendering.
    """

    return {
        "statusMessage": state.status_message,
        **_serialize_scan_result(state.scan_result),
        **_read_schema_preview(state.active_schema),
    }


def _choose_folder_with_osascript() -> Path | None:
    """Prompts the user to pick a folder using macOS AppleScript.

    Returns:
        The selected folder path, or ``None`` if the picker was canceled.
    """

    command = [
        "osascript",
        "-e",
        'POSIX path of (choose folder with prompt "Choose document root folder")',
    ]

    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if "User canceled" in stderr:
            return None
        raise RuntimeError(stderr or "Folder picker failed.")

    selected = completed.stdout.strip()
    return Path(selected) if selected else None


def _scan_selected_folder(state: AppState, folder: Path) -> None:
    """Scans a selected folder and updates application state.

    Args:
        state: Shared application state.
        folder: Folder that should be scanned.
    """

    state.selected_folder = folder
    state.scan_result = scan_folder(folder)

    available_schemas = set(state.scan_result.schema_files)
    if state.active_schema not in available_schemas:
        state.active_schema = get_default_active_schema(state.scan_result.schema_files)

    document_count = state.scan_result.document_count
    if document_count == 0:
        state.status_message = (
            "Scan complete. No supported documents were found in the selected folder."
        )
    else:
        state.status_message = (
            f"Scan complete. Found {document_count} supported documents."
        )


def _set_active_schema(state: AppState, schema_name: str) -> None:
    """Sets the active schema from the current scan result.

    Args:
        state: Shared application state.
        schema_name: Name of the schema selected in the UI.

    Raises:
        RuntimeError: If no folder has been scanned yet.
        ValueError: If the requested schema name does not exist.
    """

    if state.scan_result is None:
        raise RuntimeError("Choose a folder before selecting a schema.")

    for schema_path in state.scan_result.schema_files:
        if schema_path.name == schema_name:
            state.active_schema = schema_path
            state.status_message = f"Active schema set to {schema_name}."
            return

    raise ValueError(f"Schema not found: {schema_name}")


def _open_in_system_editor(file_path: Path) -> None:
    """Opens a file using the macOS default editor.

    Args:
        file_path: File that should be opened.
    """

    subprocess.run(["open", str(file_path)], check=True)


class DataLabelerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the local Story 2 web app."""

    app_state: AppState

    def do_GET(self) -> None:  # noqa: N802
        """Handles GET requests for the UI and current state."""

        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(_build_html())
            return

        if parsed.path == "/api/state":
            self._send_json(_serialize_state(self.app_state))
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        """Handles POST requests for Story 2 actions."""

        parsed = urlparse(self.path)

        try:
            if parsed.path == "/api/choose-folder":
                folder = _choose_folder_with_osascript()
                if folder is None:
                    self.app_state.status_message = "Folder selection canceled."
                    self._send_json({"ok": True, **_serialize_state(self.app_state)})
                    return

                _scan_selected_folder(self.app_state, folder)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/refresh":
                if self.app_state.selected_folder is None:
                    self.app_state.status_message = (
                        "Choose a folder before refreshing."
                    )
                    self._send_json(
                        {"ok": False, **_serialize_state(self.app_state)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                _scan_selected_folder(self.app_state, self.app_state.selected_folder)
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/set-active-schema":
                body = self._read_json_body()
                _set_active_schema(self.app_state, body.get("schemaName", ""))
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

            if parsed.path == "/api/open-active-schema":
                if self.app_state.active_schema is None:
                    raise RuntimeError("No active schema selected.")

                _open_in_system_editor(self.app_state.active_schema)
                self.app_state.status_message = (
                    f"Opened {self.app_state.active_schema.name} in the system editor."
                )
                self._send_json({"ok": True, **_serialize_state(self.app_state)})
                return

        except Exception as error:  # noqa: BLE001
            self.app_state.status_message = f"Error: {error}"
            self._send_json(
                {"ok": False, **_serialize_state(self.app_state)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        """Suppresses default HTTP request logging noise."""

    def _read_json_body(self) -> dict[str, Any]:
        """Reads a JSON request body.

        Returns:
            Parsed JSON body.
        """

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(raw_body.decode("utf-8"))

    def _send_html(self, content: str) -> None:
        """Writes an HTML response.

        Args:
            content: HTML content to send.
        """

        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        """Writes a JSON response.

        Args:
            payload: Dictionary to encode as JSON.
            status: HTTP status code for the response.
        """

        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _build_html() -> str:
    """Builds the single-page Story 2 user interface.

    Returns:
        The full HTML document used by the local browser UI.
    """

    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>D2R Data Labeler</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #eef3f8;
        --panel: #ffffff;
        --panel-alt: #f7f9fc;
        --border: #dce4ee;
        --ink: #11253e;
        --muted: #6f8097;
        --blue: #5b8def;
        --blue-soft: #eaf1ff;
        --teal: #2f8f83;
        --teal-soft: #e9f7f4;
        --amber: #a56a00;
        --amber-soft: #fff3db;
      }

      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: var(--bg);
        color: var(--ink);
      }
      .app {
        max-width: 1480px;
        margin: 0 auto;
        padding: 24px;
      }
      .header, .panel {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 20px;
      }
      .header {
        padding: 24px;
        margin-bottom: 20px;
      }
      .eyebrow {
        display: inline-block;
        margin: 0 0 12px;
        padding: 6px 12px;
        border-radius: 999px;
        background: var(--blue-soft);
        color: var(--blue);
        font-size: 13px;
        font-weight: 600;
      }
      h1, h2, h3 { margin: 0; }
      .subtitle {
        margin: 10px 0 0;
        color: var(--muted);
        max-width: 920px;
        line-height: 1.5;
      }
      .controls {
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin: 20px 0;
      }
      button {
        border: 1px solid #bcd0ff;
        border-radius: 14px;
        background: var(--blue-soft);
        color: var(--blue);
        padding: 10px 16px;
        font: inherit;
        font-weight: 600;
        cursor: pointer;
      }
      button.secondary {
        border-color: var(--border);
        background: #fff;
        color: var(--ink);
      }
      button:disabled {
        opacity: 0.6;
        cursor: not-allowed;
      }
      .status {
        margin: 0 0 16px;
        padding: 14px 16px;
        border-radius: 14px;
        background: var(--panel-alt);
        border: 1px solid var(--border);
        color: var(--muted);
      }
      .grid {
        display: grid;
        grid-template-columns: 360px 420px 1fr;
        gap: 20px;
      }
      .panel { padding: 20px; }
      .summary-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
      }
      .summary-card {
        background: var(--panel-alt);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 14px;
      }
      .summary-label {
        margin: 0 0 6px;
        font-size: 12px;
        color: var(--muted);
      }
      .summary-value {
        margin: 0;
        font-size: 18px;
        font-weight: 700;
        word-break: break-word;
      }
      .badge {
        display: inline-block;
        margin-bottom: 8px;
        padding: 6px 12px;
        border-radius: 999px;
        font-size: 12px;
      }
      .badge.teal { background: var(--teal-soft); color: var(--teal); }
      .badge.blue { background: var(--blue-soft); color: var(--blue); }
      .badge.amber { background: var(--amber-soft); color: var(--amber); }
      .list-shell {
        border: 1px solid var(--border);
        border-radius: 16px;
        overflow: hidden;
        background: var(--panel-alt);
      }
      .list-header {
        padding: 14px 16px;
        border-bottom: 1px solid var(--border);
        font-weight: 700;
      }
      .list-content {
        max-height: 560px;
        overflow: auto;
        background: #fff;
      }
      .row {
        padding: 12px 16px;
        border-bottom: 1px solid #edf2f7;
        font-size: 14px;
        line-height: 1.4;
        word-break: break-word;
      }
      .row:last-child { border-bottom: 0; }
      .empty { color: var(--muted); }
      .schema-title {
        font-weight: 700;
      }
      .schema-controls {
        display: flex;
        gap: 8px;
        margin-top: 10px;
        align-items: center;
      }
      .preview {
        max-height: 560px;
        overflow: auto;
        margin: 0;
        padding: 16px;
        background: #0f1d31;
        color: #f3f7ff;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13px;
        line-height: 1.5;
        white-space: pre-wrap;
      }
      @media (max-width: 1180px) {
        .grid { grid-template-columns: 1fr; }
        .summary-grid { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <main class="app">
      <section class="header">
        <p class="eyebrow">Story 2</p>
        <h1>D2R Data Labeler</h1>
        <p class="subtitle">
          Choose a local folder, review detected schemas, mark one as active,
          preview it read-only in the app, and open it in your system editor.
        </p>
      </section>

      <p id="status" class="status">Loading application state...</p>

      <div class="controls">
        <button id="choose-folder">Choose Folder</button>
        <button id="refresh-folder" class="secondary" disabled>Refresh Folder</button>
      </div>

      <section class="grid">
        <div class="panel">
          <h2>Folder Summary</h2>
          <p class="subtitle" id="selected-folder">No folder selected.</p>
          <p class="subtitle">
            Shared support files from:
            <strong id="support-folder">No support folder selected.</strong>
          </p>

          <div class="summary-grid" style="margin-top: 20px;">
            <article class="summary-card">
              <span class="badge amber">Documents</span>
              <p class="summary-label">Supported files</p>
              <p id="document-count" class="summary-value">0</p>
            </article>
            <article class="summary-card">
              <span class="badge blue">Schemas</span>
              <p class="summary-label">Detected schema files</p>
              <p id="schema-count" class="summary-value">0</p>
            </article>
            <article class="summary-card">
              <span class="badge teal">Categories</span>
              <p class="summary-label">Shared config</p>
              <p id="categories-file" class="summary-value">Not found</p>
            </article>
            <article class="summary-card">
              <span class="badge blue">Labels</span>
              <p class="summary-label">Existing output</p>
              <p id="labels-file" class="summary-value">Not found</p>
            </article>
          </div>

          <div class="list-shell" style="margin-top: 20px;">
            <div class="list-header">Warnings</div>
            <div id="warning-list" class="list-content"></div>
          </div>
        </div>

        <div class="panel">
          <h2>Schema List</h2>
          <p class="subtitle">
            The active schema controls later extraction behavior. Editing still
            happens outside the app.
          </p>

          <div class="controls">
            <button id="refresh-schemas" class="secondary" disabled>Refresh Schemas</button>
            <button id="open-active-schema" class="secondary" disabled>Open Active Schema</button>
          </div>

          <div class="list-shell">
            <div class="list-header">Available Schemas</div>
            <div id="schema-list" class="list-content"></div>
          </div>
        </div>

        <div class="panel">
          <h2>Active Schema Preview</h2>
          <p class="subtitle">
            Read-only preview of the currently active schema plus the current
            document list for the selected working set.
          </p>

          <div class="list-shell" style="margin-top: 20px;">
            <div class="list-header" id="active-schema-name">No active schema selected</div>
            <pre id="schema-preview" class="preview">No active schema selected.</pre>
          </div>

          <div class="list-shell" style="margin-top: 20px;">
            <div class="list-header">Discovered Documents</div>
            <div id="document-list" class="list-content"></div>
          </div>
        </div>
      </section>
    </main>

    <script>
      const statusEl = document.querySelector("#status");
      const selectedFolderEl = document.querySelector("#selected-folder");
      const supportFolderEl = document.querySelector("#support-folder");
      const documentCountEl = document.querySelector("#document-count");
      const schemaCountEl = document.querySelector("#schema-count");
      const categoriesFileEl = document.querySelector("#categories-file");
      const labelsFileEl = document.querySelector("#labels-file");
      const warningListEl = document.querySelector("#warning-list");
      const schemaListEl = document.querySelector("#schema-list");
      const activeSchemaNameEl = document.querySelector("#active-schema-name");
      const schemaPreviewEl = document.querySelector("#schema-preview");
      const documentListEl = document.querySelector("#document-list");
      const chooseFolderButton = document.querySelector("#choose-folder");
      const refreshFolderButton = document.querySelector("#refresh-folder");
      const refreshSchemasButton = document.querySelector("#refresh-schemas");
      const openActiveSchemaButton = document.querySelector("#open-active-schema");

      async function requestJson(url, options = {}) {
        const response = await fetch(url, options);
        return response.json();
      }

      function renderRows(container, items, emptyText) {
        container.innerHTML = "";
        if (!items.length) {
          const row = document.createElement("div");
          row.className = "row empty";
          row.textContent = emptyText;
          container.appendChild(row);
          return;
        }
        for (const item of items) {
          const row = document.createElement("div");
          row.className = "row";
          row.textContent = item;
          container.appendChild(row);
        }
      }

      function renderSchemaRows(payload) {
        schemaListEl.innerHTML = "";
        if (!payload.schemaFiles.length) {
          const row = document.createElement("div");
          row.className = "row empty";
          row.textContent = "No schema files found";
          schemaListEl.appendChild(row);
          return;
        }

        for (const schemaName of payload.schemaFiles) {
          const row = document.createElement("div");
          row.className = "row";

          const title = document.createElement("div");
          title.className = "schema-title";
          title.textContent = schemaName;
          row.appendChild(title);

          const controls = document.createElement("div");
          controls.className = "schema-controls";

          if (payload.activeSchema === schemaName) {
            const badge = document.createElement("span");
            badge.className = "badge teal";
            badge.textContent = "Active";
            controls.appendChild(badge);
          } else {
            const button = document.createElement("button");
            button.className = "secondary";
            button.textContent = "Set Active";
            button.addEventListener("click", async () => {
              statusEl.textContent = `Setting active schema to ${schemaName}...`;
              const nextPayload = await requestJson("/api/set-active-schema", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ schemaName })
              });
              renderState(nextPayload);
            });
            controls.appendChild(button);
          }

          row.appendChild(controls);
          schemaListEl.appendChild(row);
        }
      }

      function renderState(payload) {
        statusEl.textContent = payload.statusMessage;
        selectedFolderEl.textContent = payload.selectedFolder || "No folder selected.";
        supportFolderEl.textContent = payload.supportFolder || "No support folder selected.";
        documentCountEl.textContent = String(payload.documentCount);
        schemaCountEl.textContent = String(payload.schemaCount);
        categoriesFileEl.textContent = payload.categoriesFile || "Not found";
        labelsFileEl.textContent = payload.labelsFile || "Not found";
        refreshFolderButton.disabled = !payload.selectedFolder;
        refreshSchemasButton.disabled = !payload.selectedFolder;
        openActiveSchemaButton.disabled = !payload.activeSchema;
        activeSchemaNameEl.textContent = payload.activeSchema || "No active schema selected";
        schemaPreviewEl.textContent = payload.schemaPreviewError
          ? `Preview unavailable: ${payload.schemaPreviewError}`
          : payload.schemaPreview;

        renderSchemaRows(payload);
        renderRows(documentListEl, payload.documents, "No supported documents found");
        renderRows(warningListEl, payload.warnings, "No warnings");
      }

      async function loadState() {
        const payload = await requestJson("/api/state");
        renderState(payload);
      }

      chooseFolderButton.addEventListener("click", async () => {
        statusEl.textContent = "Opening folder picker...";
        const payload = await requestJson("/api/choose-folder", { method: "POST" });
        renderState(payload);
      });

      refreshFolderButton.addEventListener("click", async () => {
        statusEl.textContent = "Refreshing folder...";
        const payload = await requestJson("/api/refresh", { method: "POST" });
        renderState(payload);
      });

      refreshSchemasButton.addEventListener("click", async () => {
        statusEl.textContent = "Refreshing schema list...";
        const payload = await requestJson("/api/refresh", { method: "POST" });
        renderState(payload);
      });

      openActiveSchemaButton.addEventListener("click", async () => {
        statusEl.textContent = "Opening active schema...";
        const payload = await requestJson("/api/open-active-schema", { method: "POST" });
        renderState(payload);
      });

      loadState();
    </script>
  </body>
</html>
"""


def launch_app() -> None:
    """Launches the local browser-based application."""

    state = AppState()

    class BoundHandler(DataLabelerHandler):
        """Request handler bound to the shared application state."""

        app_state = state

    server = ThreadingHTTPServer((HOST, PORT), BoundHandler)
    url = f"http://{HOST}:{PORT}"

    print(f"Data Labeler running at {url}")
    print("Press Ctrl+C to stop the server.")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\\nShutting down Data Labeler...")
    finally:
        server.server_close()
