"""Config resolution without touching .env: three endpoint styles plus the Claude Code fallback."""
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = {
    "ollama, no key": {"MODEL_BASE_URL": "http://localhost:11434/v1", "MODEL_API_KEY": "none", "MODEL_NAME": "qwen2.5-coder"},
    "key from command + extra body": {"MODEL_BASE_URL": "http://x/v1", "MODEL_API_KEY": "cmd:echo secret-from-command",
                                      "MODEL_NAME": "m", "MODEL_EXTRA_BODY": '{"think": false}'},
    "claude code env fallback": {"ANTHROPIC_BASE_URL": "https://litellm.example.com", "ANTHROPIC_AUTH_TOKEN": "sk-x",
                                 "ANTHROPIC_MODEL": "glm-53-flash", "ANTHROPIC_SMALL_FAST_MODEL": "small"},
    "legacy GLM_ names": {"GLM_BASE_URL": "https://old/v1", "GLM_API_KEY": "k", "GLM_MODEL": "old-model", "GLM_THINKING": "0"},
}
for name, env in CASES.items():
    # a clean environment: no .env (we chdir to a temp dir is not enough since config loads .env from its own folder),
    # so run in a subprocess with HOME-independent env and a flag that skips .env
    code = "import config; print(config.describe()); print('thinking', config.THINKING)"
    e = {"PATH": os.environ["PATH"], "CONTEXT_GRAPH_SKIP_DOTENV": "1", **env}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=e, capture_output=True, text=True)
    print(f"== {name}\n{(out.stdout or out.stderr).strip()}")
