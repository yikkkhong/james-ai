import json
import os
import urllib.error
import urllib.request

from .context import OUTPUT, WINDOW, messages


class LocalModel:
    def __init__(self):
        self.provider = os.getenv("JAMES_PROVIDER", "ollama").lower()
        if self.provider not in {"ollama", "openai"}:
            raise ValueError("JAMES_PROVIDER must be ollama or openai.")
        default = "http://localhost:11434" if self.provider == "ollama" else "http://localhost:8000/v1"
        self.base = os.getenv("JAMES_ENDPOINT", default).rstrip("/")
        if not self.base.startswith(("http://", "https://")):
            raise ValueError("JAMES_ENDPOINT must be an HTTP(S) API base.")
        self.name = os.getenv("JAMES_MODEL", "qwen3-coder:30b")

    def reply(self, chat, structured=True):
        system, user = chat
        chat = messages(system["content"], user["content"])
        payload = {"model": self.name, "messages": chat, "stream": False}
        headers = {"Content-Type": "application/json"}
        if self.provider == "ollama":
            url = self.base + "/api/chat"
            payload["options"] = {"num_ctx": WINDOW, "num_predict": OUTPUT, "temperature": 0.15}
            if structured:
                payload["format"] = structured if isinstance(structured, dict) else "json"
        else:
            url = self.base + "/chat/completions"
            payload.update(max_tokens=OUTPUT, temperature=0.15)
            if structured and os.getenv("JAMES_JSON_MODE", "1") != "0":
                payload["response_format"] = {"type": "json_object"}
            if os.getenv("JAMES_API_KEY"):
                headers["Authorization"] = "Bearer " + os.environ["JAMES_API_KEY"]
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Model HTTP {exc.code}: {exc.read(2000).decode(errors='replace')}") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read model response at {url}: {exc}") from exc
        try:
            msg = data["message"] if self.provider == "ollama" else data["choices"][0]["message"]
            content = msg["content"].strip()
            if structured:
                obj = json.loads(content)
                if not isinstance(obj, dict):
                    raise ValueError("Expected a JSON object.")
                return obj
            return content
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid model response: {exc}") from exc
