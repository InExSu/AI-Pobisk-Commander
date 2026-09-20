"""http.py — one OpenAI-compatible client for every skill.

Replaces four near-identical copies of the same urllib block that live inside
bash heredocs across NVIDIA, TokenHarbor, FreeBuff and the probes. Kept in
_shared/lib/ so skills can import it instead of pasting it.

Zero dependencies: urllib only.
"""

import json
import urllib.error
import urllib.request


class ApiError(Exception):
    """An upstream call failed. Carries the HTTP code when there is one."""

    def __init__(self, message, code=None, body=""):
        super().__init__(message)
        self.code = code
        self.body = body[:2000]


def request(url, key=None, method="GET", payload=None, timeout=60,
            headers=None):
    """One HTTP call. Returns the parsed JSON body, or {} on 204/empty.

    Raises ApiError with `.code` set — callers classify on it, which is why
    the status code is preserved rather than flattened into a string.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Accept": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    if data:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)

    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise ApiError("HTTP %s" % e.code, code=e.code, body=body)
    except urllib.error.URLError as e:
        raise ApiError("network: %s" % e.reason)
    except json.JSONDecodeError as e:
        raise ApiError("bad JSON: %s" % e)
    except Exception as e:
        raise ApiError("%s: %s" % (type(e).__name__, e))


def chat(base_url, key, model, messages, max_tokens=2048, timeout=180,
         extra=None):
    """One chat completion. Returns (text, http_code).

    Reasoning models (glm-5.3-flash, kimi-k3) leave `content` null and put the
    answer in `reasoning_content` — read both, or a short token budget looks
    like an empty reply.
    """
    payload = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if extra:
        payload.update(extra)
    d = request(base_url.rstrip("/") + "/chat/completions", key=key,
                method="POST", payload=payload, timeout=timeout)
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or msg.get("reasoning_content") or ""
    return text, None


def models(base_url, key, timeout=30):
    """GET /models -> list of ids."""
    d = request(base_url.rstrip("/") + "/models", key=key, timeout=timeout)
    return [m.get("id") for m in (d.get("data") or []) if m.get("id")]


def balance(base_url, key, timeout=20):
    """GET /balance -> int credits, or None.

    Only some gateways expose it (api.b.ai does; most others 403 on anything
    outside the inference paths). None means "unknown", not "zero" — callers
    must not skip an account on None.
    """
    try:
        d = request(base_url.rstrip("/") + "/balance", key=key, timeout=timeout)
    except ApiError:
        return None
    v = (d.get("data") or {}).get("personal_balance")
    return int(v) if isinstance(v, (int, float)) else None
