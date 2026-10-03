# webhook fixture

A tiny webhook delivery service: an `http.server` endpoint that accepts `POST /webhook`, stores each delivery in memory, and returns 400 on a malformed body.

- `python3 -m unittest` runs the two unit tests.
- `python3 -m webhook` starts the server on `127.0.0.1:8765`.

`setup.sh` copies this directory to a temp directory and turns it into a fresh git repository - `git init`, a `.gitignore` holding `.dev/`, the first commit on `main`, and a bare repository beside the copy set as `origin` - so a factory run has an offline, disposable target to build against.
