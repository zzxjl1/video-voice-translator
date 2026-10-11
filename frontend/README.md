# Video Voice Translator — frontend

React + Vite single-page app. It talks to the FastAPI backend under `/api`.

## Run

```
npm install
npm run dev        # http://localhost:3100, proxies /api to http://localhost:9100
npm run build      # production build into dist/
npm run preview    # serve the build on :3300 with the same /api proxy
```

Set `VITE_BACKEND_ORIGIN` to point the dev/preview proxy at a different backend.
No API keys are needed here: all provider calls are made by the backend.

See `../backend/nginx.conf` for the production layout (static `dist/` + `/api` proxy).
