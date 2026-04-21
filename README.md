# Data Labeler

Python local web app for the D2R Data Labeler internal workflow.

## Current scope

This repository now implements Story 1:

* choose a local folder
* recursively discover supported documents
* detect schema files, `categories.json`, and `labels.json`
* show a scrollable document list
* handle empty or mixed folders without crashing

## Run

```bash
cd "/Users/liuyang/Data Labeler"
python3 app.py
```

If your default `python3` does not include Tk support, the launcher will
automatically re-run itself with `/usr/bin/python3` on macOS.

## Notes

* The app uses the Python standard library only.
* The UI runs in the browser and is served by a tiny built-in Python HTTP server.
* On macOS, folder selection uses `osascript` to open the native folder picker.
* Docstrings follow Google style.
