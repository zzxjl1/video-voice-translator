"""
Development / simple-deployment entry point: `python start_server.py`.

Binds to localhost by default. nginx (see nginx.conf) is the public face and
proxies /api to 127.0.0.1:9100, so there is no reason for uvicorn itself to listen
on every interface — doing so exposed the unauthenticated API directly, around the
proxy's body limits and rate limiting. Override with `HOST=0.0.0.0` when you really
want that, and `RELOAD=1` for auto-reload while developing (it restarts the process
on any file change, including the data directory, which is not what you want in
production).
"""
import os

import uvicorn

# Ensure we are in the backend directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "9100"))
RELOAD = os.environ.get("RELOAD", "").strip().lower() in ("1", "true", "yes")

if __name__ == "__main__":
    print(f"Starting Video Voice Translator backend on http://{HOST}:{PORT} (reload={RELOAD})")
    uvicorn.run(
        "app.main:app",
        host=HOST,
        port=PORT,
        reload=RELOAD,
        # The data directory lives under backend/: without this, reload mode would
        # restart the server (killing a running pipeline) every time one is written.
        reload_excludes=["data/*", "data/**"] if RELOAD else None,
    )
