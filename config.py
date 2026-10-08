"""Model endpoint settings for the standalone runner, server and eval (the Claude Code plugin needs none of
this: Claude Code supplies the model).

Any OpenAI-compatible chat-completions endpoint works: vLLM, Ollama, LiteLLM, OpenAI, OpenRouter, Zhipu,
Together, Groq, llama.cpp server, ... Settings come from the environment or from .env next to this file
(plain KEY=VALUE, no dependency). Names, in order of preference:

  MODEL_BASE_URL   e.g. https://host/v1  (Ollama: http://localhost:11434/v1)
  MODEL_API_KEY    a key; "none" for servers without auth; "cmd:<shell command>" to run a command that prints
                   the key (e.g. "cmd:oc whoami -t" for an OpenShift AI route); "oc" is a shortcut for that one
  MODEL_NAME       the model id the endpoint serves
  MODEL_FAST       cheaper model for classification and judging (defaults to MODEL_NAME)
  MODEL_EXTRA_BODY JSON merged into every request body, for provider-specific knobs, e.g.
                   {"chat_template_kwargs":{"enable_thinking":false}}  (vLLM reasoning models)
                   {"reasoning_effort":"low"}                           (OpenAI-style reasoning)
                   {"think":false}                                      (Ollama)
  MODEL_TIMEOUT    seconds, default 120.   MODEL_VERIFY_TLS  1/0, default 1.
  MODEL_VISION_NAME, MODEL_VISION_BASE_URL, MODEL_VISION_API_KEY, MODEL_VISION_MAX_FIGURES
                   optional vision-capable model that describes figures in documents (see vision.py), used by
                   the standalone tools and by the plugin alike. Unset = figures get OCR text only.
  MODEL_OCR_NAME, MODEL_OCR_BASE_URL, MODEL_OCR_API_KEY
                   optional model that replaces RapidOCR for reading text out of images and scanned pages.

Settings files, in order (first definition wins, environment first): $CONTEXT_GRAPH_ENV,
~/.config/context-graph/.env, then .env next to this file. Plugin users put theirs in ~/.config/context-graph/.env.

Fallbacks, so existing setups keep working: GLM_* names (older .env files) and Claude Code's ANTHROPIC_BASE_URL /
ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL when the base URL is a proxy such as LiteLLM
that also serves /v1/chat/completions (api.anthropic.com itself does not, so that case is not supported here).
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def env_files():
    """Where settings are read from, first wins over later: the process environment, then
    $CONTEXT_GRAPH_ENV, then ~/.config/context-graph/.env (the place for plugin users, since the plugin
    folder is replaced on update), then .env next to the code (standalone checkouts)."""
    out = []
    if os.environ.get("CONTEXT_GRAPH_ENV"):
        out.append(Path(os.environ["CONTEXT_GRAPH_ENV"]).expanduser())
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    out.append(xdg / "context-graph" / ".env")
    out.append(HERE / ".env")
    return out


def _load_env():
    if os.environ.get("CONTEXT_GRAPH_SKIP_DOTENV"):
        return
    for p in env_files():
        if not p.is_file():
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _first(*names, default=""):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def _anthropic_base() -> str:
    """Claude Code's ANTHROPIC_BASE_URL points at a proxy root; the OpenAI-style path lives under /v1."""
    b = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    if not b or "api.anthropic.com" in b:
        return ""
    return b if b.endswith("/v1") else b + "/v1"


def _resolve_key(raw: str) -> str:
    """Literal key, "none" for no auth, "oc" or "cmd:<command>" to obtain it from a command."""
    import shlex
    import subprocess
    raw = (raw or "").strip()
    if raw.lower() == "none":
        return "none"
    cmd = None
    if raw.lower() == "oc":
        cmd = ["oc", "whoami", "-t"]
    elif raw.lower().startswith("cmd:"):
        cmd = shlex.split(raw[4:])
    if cmd:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return ""
    return raw


MODEL_BASE_URL = _first("MODEL_BASE_URL", "GLM_BASE_URL", default=_anthropic_base()).rstrip("/")
MODEL_API_KEY = _resolve_key(_first("MODEL_API_KEY", "GLM_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"))
MODEL_NAME = _first("MODEL_NAME", "GLM_MODEL", "ANTHROPIC_MODEL")
MODEL_FAST = _first("MODEL_FAST", "ANTHROPIC_SMALL_FAST_MODEL", default=MODEL_NAME)
MODEL_TIMEOUT = float(_first("MODEL_TIMEOUT", "GLM_TIMEOUT", default="120"))
MODEL_VERIFY_TLS = _first("MODEL_VERIFY_TLS", "GLM_VERIFY_TLS", default="1") not in ("0", "false", "no")

# Provider-specific request fields. GLM_THINKING=0 is kept as a shortcut for the vLLM reasoning switch.
try:
    EXTRA_BODY = json.loads(os.environ.get("MODEL_EXTRA_BODY") or "{}")
except ValueError:
    raise SystemExit("MODEL_EXTRA_BODY must be a JSON object")
if os.environ.get("GLM_THINKING", "1") in ("0", "false", "no"):
    EXTRA_BODY.setdefault("chat_template_kwargs", {}).setdefault("enable_thinking", False)
THINKING = not (EXTRA_BODY.get("chat_template_kwargs", {}).get("enable_thinking") is False
                or EXTRA_BODY.get("think") is False or EXTRA_BODY.get("reasoning_effort") in ("none", "minimal"))

# Backwards-compatible aliases for older code.
GLM_BASE_URL, GLM_API_KEY, GLM_MODEL, GLM_THINKING = MODEL_BASE_URL, MODEL_API_KEY, MODEL_NAME, THINKING


def describe() -> str:
    key = "none" if MODEL_API_KEY == "none" else ("set" if MODEL_API_KEY else "MISSING")
    return f"{MODEL_NAME or 'MISSING'} @ {MODEL_BASE_URL or 'MISSING'} (key {key}, fast {MODEL_FAST or '-'}, extra {EXTRA_BODY or '{}'})"


def require():
    missing = [k for k, v in (("MODEL_BASE_URL", MODEL_BASE_URL), ("MODEL_API_KEY", MODEL_API_KEY),
                              ("MODEL_NAME", MODEL_NAME)) if not v]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}: copy .env.example to .env in {HERE} and fill it in. "
                         "MODEL_API_KEY may be a key, 'none', or 'cmd:<command that prints the key>' "
                         "(e.g. 'cmd:oc whoami -t'; make sure that login is current).")


def client():
    """OpenAI-compatible client for the configured endpoint."""
    require()
    import httpx
    from openai import OpenAI
    http = httpx.Client(verify=MODEL_VERIFY_TLS, timeout=MODEL_TIMEOUT)
    return OpenAI(base_url=MODEL_BASE_URL, api_key=MODEL_API_KEY if MODEL_API_KEY != "none" else "none", http_client=http)
