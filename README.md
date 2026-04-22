# Data Labeler

Python local web app for the D2R Data Labeler internal workflow.

## Current scope

This repository now implements Stories 1 through 4, plus a Story 6 extraction spike:

* choose a local folder
* recursively discover supported documents
* detect schema files, `categories.json`, and `labels.json`
* show a scrollable document list and manual review workspace
* choose an active schema and preview it
* save and reload reviews in `labels.json`
* trigger one-document field extraction from the review screen

## Run

```bash
cd /path/to/Data-Labeler
python3 app.py
```

## Extraction setup

The Story 6 spike now prefers Oracle-internal Responses-compatible endpoints:

* local `ocat` endpoint at `http://127.0.0.1:9755/v1/responses` when available
* OCI internal fallback with a bearer token read from a file
* demo stub mode when neither live option is configured

Recommended local setup with OCAT:

```bash
python3 app.py
```

OCI internal fallback setup:

```bash
export OCA_TOKEN_FILE="$HOME/.config/data-labeler/oca-token.txt"
export DATA_LABELER_MODEL="gpt-5.4"
python3 app.py
```

Keep the token file outside the repository and never commit it.

Optional legacy OpenAI setup remains available:

```bash
export OPENAI_API_KEY="your-key-here"
export OPENAI_MODEL="gpt-5-mini"
python3 app.py
```

The app always sends extraction requests from the Python backend, not the browser.

## Notes

* The app uses the Python standard library only.
* The UI runs in the browser and is served by a tiny built-in Python HTTP server.
* On macOS, folder selection uses `osascript` to open the native folder picker.
* Docstrings follow Google style.
