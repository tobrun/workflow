"""Launch command for the fixture: python3 -m webhook."""

from __future__ import annotations

from webhook import serve

if __name__ == "__main__":
    server = serve()
    print(f"webhook fixture listening on {server.server_address}")
    server.serve_forever()
