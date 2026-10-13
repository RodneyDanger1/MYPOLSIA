# backend/data — runtime state (never source)

What lives here:

- `deepseek_api_key.json` — the DeepSeek API key saved from the dashboard
  (Settings → AI model (DeepSeek)). Written atomically by
  `app/services/secret_store.py` (temp file + `os.replace`, mode 0600 best-effort).
  **Gitignored on purpose** (see the repo `.gitignore`): committing it would leak
  the key to everyone with repo access. The path is relative to the backend
  process's working directory (`backend/data/…` locally, `/app/data/…` in the
  image) and can be moved with the `DEEPSEEK_KEY_FILE` environment variable.
- `*.tmp` siblings are write-temp artifacts; a crashed write can leave one behind
  and it is safe to delete.

Nothing else belongs in this directory. It is runtime state: do not commit files
from here (the key file and its temp siblings are ignored; keep it that way).
