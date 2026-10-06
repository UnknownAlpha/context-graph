"""Loads GLM settings from .env next to this file (plain KEY=VALUE, no dependency)."""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_env():
    p = HERE / ".env"
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

GLM_BASE_URL = os.environ.get("GLM_BASE_URL", "").rstrip("/")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-53-fp8-v10")
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")


def _oc_token() -> str:
    """GLM_API_KEY=oc means: use the current `oc whoami -t` login token (RHOAI accepts user tokens)."""
    import subprocess
    try:
        return subprocess.run(["oc", "whoami", "-t"], capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


if GLM_API_KEY.strip().lower() == "oc":
    GLM_API_KEY = _oc_token()
GLM_TIMEOUT = float(os.environ.get("GLM_TIMEOUT", "120"))
GLM_VERIFY_TLS = os.environ.get("GLM_VERIFY_TLS", "1") not in ("0", "false", "no")
GLM_THINKING = os.environ.get("GLM_THINKING", "1") not in ("0", "false", "no")

# vLLM passes chat_template_kwargs to the GLM chat template; enable_thinking=false turns
# off the reasoning phase, which is most of the wall time on diagnosis questions.
EXTRA_BODY = {} if GLM_THINKING else {"chat_template_kwargs": {"enable_thinking": False}}


def require():
    missing = [k for k, v in (("GLM_BASE_URL", GLM_BASE_URL), ("GLM_API_KEY", GLM_API_KEY)) if not v]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}: copy .env.example to .env in {HERE} and fill it in "
                         f"(GLM_API_KEY=oc uses your current `oc login`; make sure you are logged into the model's cluster)")


def client():
    """OpenAI-compatible client pointed at the vLLM server."""
    require()
    import httpx
    from openai import OpenAI
    http = httpx.Client(verify=GLM_VERIFY_TLS, timeout=GLM_TIMEOUT)
    return OpenAI(base_url=GLM_BASE_URL, api_key=GLM_API_KEY, http_client=http)
