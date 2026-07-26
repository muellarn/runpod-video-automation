from __future__ import annotations

import shlex
import socket
import subprocess
import time
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from runpod_video_automation.config import ModelFile, ModelPathAlias

if TYPE_CHECKING:
    from runpod_video_automation.prompt_refiner.config import PromptRefinerProfile


class RemoteWorker:
    def __init__(self, *, host: str, port: int, ssh_key: Path) -> None:
        self.host = host
        self.port = port
        self.ssh_key = ssh_key.expanduser()

    @property
    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.ssh_key),
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "StrictHostKeyChecking=accept-new",
            f"root@{self.host}",
        ]

    def wait_for_ssh(self, timeout_seconds: int = 300) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = "no SSH response"
        while time.monotonic() < deadline:
            result = subprocess.run(
                [*self._ssh_base, "true"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return
            last_error = (result.stderr or result.stdout).strip() or last_error
            time.sleep(5)
        raise TimeoutError(
            f"SSH did not become ready on {self.host}:{self.port}: {last_error}"
        )

    def run(self, command: str, *, timeout: int | None = None) -> None:
        subprocess.run([*self._ssh_base, command], check=True, timeout=timeout)

    def verify_models(
        self,
        models: tuple[ModelFile, ...],
        aliases: tuple[ModelPathAlias, ...] = (),
    ) -> None:
        if not models:
            return
        checks: list[str] = ["status=0"]
        model_aliases: dict[str, str] = {}
        for model in models:
            destination = f"/runpod-volume/{model.path}"
            size_check = (
                f"test $(stat -c%s {shlex.quote(destination)} 2>/dev/null || echo 0) "
                f"-eq {model.size}"
                if model.size is not None
                else f"test -s {shlex.quote(destination)}"
            )
            checks.append(
                f"if ! test -f {shlex.quote(destination)} || ! {size_check}; then "
                f"echo {shlex.quote('Missing or invalid prewarmed model: ' + model.path)} "
                ">&2; status=1; fi"
            )
            for alias in aliases:
                try:
                    relative = Path(model.path).relative_to(alias.source)
                except ValueError:
                    continue
                alias_path = f"/runpod-volume/{Path(alias.target) / relative}"
                existing = model_aliases.get(alias_path)
                if existing is not None and existing != destination:
                    raise ValueError(f"Model path alias collision at {alias_path}")
                model_aliases[alias_path] = destination
        if model_aliases:
            alias_directories = " ".join(
                shlex.quote(str(Path(alias).parent)) for alias in model_aliases
            )
            alias_commands = " && ".join(
                f"ln -sfn {shlex.quote(source)} {shlex.quote(alias)}"
                for alias, source in model_aliases.items()
            )
            checks.append(
                f'if test "$status" -eq 0; then mkdir -p {alias_directories} '
                f"&& {alias_commands} || status=1; fi"
            )
        checks.append('exit "$status"')
        self.run("; ".join(checks), timeout=5 * 60)
        print(f"Models: {len(models)}/{len(models)} prewarmed files ready", flush=True)

    def ensure_comfy_args(
        self,
        args: tuple[str, ...],
    ) -> None:
        digest = sha256("\0".join(args).encode()).hexdigest()
        quoted_args = " ".join(shlex.quote(arg) for arg in args)
        self.run(
            "pid_file=/tmp/comfyui.pid; marker=/tmp/runpod-video-comfy-args; "
            'pid=$(cat "$pid_file" 2>/dev/null || true); '
            'configured=$(cat "$marker" 2>/dev/null || true); '
            f'if test "$configured" != "$pid {digest}" || '
            'test -z "$pid" || ! kill -0 "$pid" 2>/dev/null; then '
            'if test -n "$pid" && kill -0 "$pid" 2>/dev/null; then '
            'kill "$pid"; while kill -0 "$pid" 2>/dev/null; do sleep 1; done; fi; '
            "nohup /opt/venv/bin/python -u /comfyui/main.py "
            "--disable-auto-launch --disable-metadata --listen --verbose INFO "
            f"--log-stdout {quoted_args} "
            ">/tmp/comfyui-profile.log 2>&1 </dev/null & "
            'pid=$!; echo "$pid" > "$pid_file"; '
            f'echo "$pid {digest}" > "$marker"; fi',
            timeout=5 * 60,
        )

    def stop_comfyui(self) -> None:
        self.run(
            "pids=$(pgrep -f '/[c]omfyui/main.py' || true); "
            'if test -n "$pids"; then kill $pids 2>/dev/null || true; '
            "deadline=$((SECONDS + 60)); "
            'while test -n "$(pgrep -f \'/[c]omfyui/main.py\' || true)" '
            '&& test "$SECONDS" -lt "$deadline"; do sleep 1; done; '
            "pids=$(pgrep -f '/[c]omfyui/main.py' || true); "
            'if test -n "$pids"; then kill -KILL $pids 2>/dev/null || true; fi; fi; '
            "rm -f /tmp/comfyui.pid /tmp/runpod-video-comfy-args",
            timeout=90,
        )

    def stop_koboldcpp(self) -> None:
        self.run(
            "pid_file=/tmp/runpod-video-koboldcpp.pid; "
            "runtime_file=/tmp/runpod-video-koboldcpp-runtime; "
            'pid=$(cat "$pid_file" 2>/dev/null || true); '
            'runtime=$(cat "$runtime_file" 2>/dev/null || true); '
            'if test -n "$pid" && test -n "$runtime" '
            '&& test -r "/proc/$pid/cmdline" '
            "&& tr '\\0' ' ' < \"/proc/$pid/cmdline\" | "
            'grep -Fq -- "$runtime"; then '
            'kill -- -"$pid" 2>/dev/null || true; deadline=$((SECONDS + 60)); '
            'while kill -0 -- -"$pid" 2>/dev/null '
            '&& test "$SECONDS" -lt "$deadline"; do sleep 1; done; '
            'if kill -0 -- -"$pid" 2>/dev/null; then '
            'kill -KILL -- -"$pid" 2>/dev/null || true; sleep 1; fi; '
            'if kill -0 -- -"$pid" 2>/dev/null; then exit 1; fi; fi; '
            'rm -f "$pid_file" "$runtime_file"',
            timeout=90,
        )

    @contextmanager
    def koboldcpp_process(
        self,
        profile: PromptRefinerProfile,
    ) -> Iterator[None]:
        self.stop_koboldcpp()
        runtime = shlex.quote(profile.remote_runtime_path)
        model = shlex.quote(profile.remote_model_path)
        command = (
            f"chmod 0755 {runtime}; "
            "setsid nohup "
            f"{runtime} --model {model} --usecuda 0 "
            f"--gpulayers {profile.gpu_layers} "
            f"--contextsize {profile.context_size} "
            f"--host 127.0.0.1 --port {profile.port} "
            "--jinja --jinja_kwargs '{\"enable_thinking\":false}' "
            "--quiet --skiplauncher "
            ">/tmp/runpod-video-koboldcpp.log 2>&1 </dev/null & "
            "echo $! >/tmp/runpod-video-koboldcpp.pid; "
            f"printf '%s\\n' {runtime} >/tmp/runpod-video-koboldcpp-runtime"
        )
        self.run(command, timeout=30)
        try:
            yield
        finally:
            self.stop_koboldcpp()

    @contextmanager
    def tunnel(self, remote_port: int) -> Iterator[str]:
        if not 1 <= remote_port <= 65535:
            raise ValueError("Remote tunnel port is out of range")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            local_port = sock.getsockname()[1]
        process = subprocess.Popen(
            [
                *self._ssh_base[:-1],
                "-N",
                "-L",
                f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
                self._ssh_base[-1],
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(1)
            if process.poll() is not None:
                error = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"SSH tunnel failed: {error.strip()}")
            yield f"http://127.0.0.1:{local_port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    @contextmanager
    def comfy_tunnel(self) -> Iterator[str]:
        with self.tunnel(8188) as base_url:
            yield base_url
