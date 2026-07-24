from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator, cast

from runpod_video_automation.prompt_refiner.client import KoboldClient
from runpod_video_automation.prompt_refiner.config import PromptRefinerProfile


MAX_REQUEST_BYTES = 1024 * 1024
MAX_MESSAGES = 50
MAX_MESSAGE_CHARACTERS = 200_000
DEFAULT_CONTEXT_MAX_OUTPUT_TOKENS = 32_768

CHAT_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scene Refiner</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #152033;
      --muted: #697386;
      --paper: #f4efe5;
      --panel: #fffdf8;
      --line: #d8d0c1;
      --accent: #c44d2b;
      --accent-dark: #8f331a;
      --user: #e2ebee;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(90deg, transparent 49.8%, rgba(21,32,51,.04) 50%, transparent 50.2%),
        var(--paper);
      color: var(--ink);
      font-family: Georgia, "Times New Roman", serif;
    }
    .shell {
      width: min(980px, calc(100% - 32px));
      min-height: 100vh;
      margin: 0 auto;
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 18px;
      padding: 28px 0 24px;
    }
    header {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 20px;
      border-bottom: 2px solid var(--ink);
      padding-bottom: 14px;
    }
    h1 { margin: 0; font-size: clamp(2rem, 6vw, 4.5rem); line-height: .88; }
    .kicker, .badge, button, textarea, .role, .hint {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .kicker { color: var(--accent); font-size: .78rem; letter-spacing: .14em; text-transform: uppercase; }
    .badge {
      border: 1px solid var(--ink);
      background: var(--panel);
      padding: 8px 10px;
      font-size: .72rem;
      white-space: nowrap;
    }
    #messages {
      align-self: stretch;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 260px;
      max-height: 58vh;
      overflow-y: auto;
      padding-right: 6px;
    }
    .message {
      width: min(86%, 760px);
      border: 1px solid var(--line);
      background: var(--panel);
      box-shadow: 4px 4px 0 rgba(21,32,51,.08);
      padding: 14px 16px 16px;
    }
    .message.user { align-self: end; background: var(--user); }
    .role { color: var(--accent-dark); font-size: .68rem; letter-spacing: .1em; text-transform: uppercase; }
    .content { margin: 8px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.48; }
    form {
      border-top: 2px solid var(--ink);
      padding-top: 14px;
    }
    textarea {
      width: 100%;
      min-height: 126px;
      resize: vertical;
      border: 1px solid var(--ink);
      border-radius: 0;
      background: var(--panel);
      color: var(--ink);
      padding: 14px;
      font-size: .9rem;
      line-height: 1.45;
    }
    textarea:focus { outline: 3px solid rgba(196,77,43,.28); outline-offset: 2px; }
    .actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 10px; }
    .buttons { display: flex; gap: 8px; }
    button {
      border: 1px solid var(--ink);
      border-radius: 0;
      background: var(--panel);
      color: var(--ink);
      cursor: pointer;
      padding: 9px 13px;
    }
    button[type="submit"] { background: var(--accent); color: white; border-color: var(--accent-dark); }
    button:disabled { cursor: wait; opacity: .55; }
    .hint { color: var(--muted); font-size: .68rem; }
    @media (max-width: 620px) {
      .shell { width: min(100% - 20px, 980px); padding-top: 16px; }
      header { align-items: start; flex-direction: column; }
      .message { width: 94%; }
      .actions { align-items: stretch; flex-direction: column; }
      .buttons { justify-content: flex-end; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div><div class="kicker">Context-bound workspace</div><h1>Scene<br>Refiner</h1></div>
      <div class="badge">REFERENCE ACTIVE / OUTPUT __MAX_OUTPUT_TOKENS__ TOKENS</div>
    </header>
    <section id="messages" aria-live="polite"></section>
    <form id="chat-form">
      <textarea id="prompt" required placeholder="Paste a scene prompt payload or request a refinement..."></textarea>
      <div class="actions">
        <span class="hint">Ctrl/Command + Enter to send. One response may use up to __MAX_OUTPUT_TOKENS__ tokens.</span>
        <div class="buttons">
          <button id="clear" type="button">New session</button>
          <button id="send" type="submit">Refine</button>
        </div>
      </div>
    </form>
  </main>
  <script>
    const messages = [];
    const list = document.querySelector('#messages');
    const form = document.querySelector('#chat-form');
    const prompt = document.querySelector('#prompt');
    const send = document.querySelector('#send');

    function renderMessage(role, content) {
      const article = document.createElement('article');
      article.className = `message ${role}`;
      const label = document.createElement('div');
      label.className = 'role';
      label.textContent = role === 'user' ? 'You' : 'Refiner';
      const body = document.createElement('pre');
      body.className = 'content';
      body.textContent = content;
      article.append(label, body);
      list.append(article);
      list.scrollTop = list.scrollHeight;
    }

    function reset() {
      messages.length = 0;
      list.replaceChildren();
      renderMessage('assistant', 'The scene-manifest reference and refiner system prompt are active. Paste the prompt payload you want to refine.');
      prompt.focus();
    }

    async function submit() {
      const content = prompt.value.trim();
      if (!content || send.disabled) return;
      messages.push({role: 'user', content});
      renderMessage('user', content);
      prompt.value = '';
      send.disabled = true;
      send.textContent = 'Working...';
      try {
        const response = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({messages}),
        });
        const value = await response.json();
        if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`);
        messages.push({role: 'assistant', content: value.content});
        renderMessage('assistant', value.content);
      } catch (error) {
        messages.pop();
        renderMessage('assistant', `Error: ${error.message}`);
      } finally {
        send.disabled = false;
        send.textContent = 'Refine';
        prompt.focus();
      }
    }

    form.addEventListener('submit', event => { event.preventDefault(); submit(); });
    prompt.addEventListener('keydown', event => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        submit();
      }
    });
    document.querySelector('#clear').addEventListener('click', reset);
    reset();
  </script>
</body>
</html>
"""


def _validate_messages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MESSAGES:
        raise ValueError(f"messages must contain between 1 and {MAX_MESSAGES} items")
    messages: list[dict[str, str]] = []
    total_characters = 0
    expected_role = "user"
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or set(item) != {"role", "content"}:
            raise ValueError(f"message {index} must contain role and content")
        role = item.get("role")
        content = item.get("content")
        if role != expected_role:
            raise ValueError(f"message {index} must use role {expected_role!r}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"message {index} content must be a non-empty string")
        content = content.strip()
        total_characters += len(content)
        if total_characters > MAX_MESSAGE_CHARACTERS:
            raise ValueError("chat history is too large")
        messages.append({"role": role, "content": content})
        expected_role = "assistant" if role == "user" else "user"
    if messages[-1]["role"] != "user":
        raise ValueError("the final message must use role 'user'")
    return messages


class _ContextChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        client: KoboldClient,
        profile: PromptRefinerProfile,
        max_output_tokens: int,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _ContextChatHandler)
        self.client = client
        self.profile = profile
        self.max_output_tokens = max_output_tokens
        self.system_prompt = profile.system_prompt()
        self.chat_html = CHAT_HTML.replace(
            "__MAX_OUTPUT_TOKENS__", f"{max_output_tokens:,}"
        )
        self.generation_lock = threading.Lock()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _ContextChatHandler(BaseHTTPRequestHandler):
    server: _ContextChatServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'none'; frame-ancestors 'none'",
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, ensure_ascii=True).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path not in {"/", "/index.html"}:
            self._send_json(404, {"error": "not found"})
            return
        self._send(200, self.server.chat_html.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self._send_json(404, {"error": "not found"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._send_json(403, {"error": "cross-origin requests are not allowed"})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "content type must be application/json"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= MAX_REQUEST_BYTES:
            self._send_json(413, {"error": "request body size is invalid"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict) or set(payload) != {"messages"}:
                raise ValueError("request must contain only messages")
            messages = _validate_messages(payload.get("messages"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._send_json(400, {"error": str(error)})
            return
        try:
            with self.server.generation_lock:
                content = self.server.client.chat_messages(
                    system_prompt=self.server.system_prompt,
                    messages=messages,
                    profile=self.server.profile,
                    max_tokens=self.server.max_output_tokens,
                )
        except Exception as error:
            self._send_json(502, {"error": str(error)})
            return
        self._send_json(200, {"content": content})


@contextmanager
def context_chat_server(
    client: KoboldClient,
    profile: PromptRefinerProfile,
    *,
    max_output_tokens: int | None = None,
) -> Iterator[str]:
    output_tokens = max_output_tokens
    if output_tokens is None:
        output_tokens = min(
            DEFAULT_CONTEXT_MAX_OUTPUT_TOKENS,
            profile.context_size // 2,
        )
    if not 0 < output_tokens < profile.context_size:
        raise ValueError("Output token limit must be below the context size")
    server = _ContextChatServer(client, profile, output_tokens)
    thread = threading.Thread(
        target=server.serve_forever,
        name="runpod-video-context-chat",
        daemon=True,
    )
    thread.start()
    try:
        yield server.origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
