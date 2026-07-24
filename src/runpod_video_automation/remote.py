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


def _quiet_package_setup(
    packages: tuple[str, ...],
    *,
    check_command: str | None = None,
) -> str:
    package_args = " ".join(shlex.quote(package) for package in packages)
    check = check_command or f"dpkg-query -W {package_args} >/dev/null 2>&1"
    return (
        f"if ! {check}; then "
        "apt_log=/tmp/runpod-video-apt.log; "
        "if ! (apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        f"-o=Dpkg::Use-Pty=0 {package_args}) >\"$apt_log\" 2>&1; then "
        "echo 'Package installation failed; recent apt output:' >&2; "
        "tail -n 80 \"$apt_log\" >&2; exit 1; fi; fi"
    )


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

    def ensure_models(
        self,
        models: tuple[ModelFile, ...],
        aliases: tuple[ModelPathAlias, ...] = (),
    ) -> None:
        if not models:
            return
        directories = sorted({str(Path(model.path).parent) for model in models})
        mkdir_args = " ".join(
            shlex.quote(f"/runpod-volume/{directory}") for directory in directories
        )
        self.run(f"mkdir -p {mkdir_args}")
        downloads: list[str] = []
        model_aliases: dict[str, str] = {}
        for index, model in enumerate(models, start=1):
            destination = f"/runpod-volume/{model.path}"
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
            partial = f"{destination}.part"
            print(f"[{index}/{len(models)}] Ensuring {model.path}")
            destination_size_check = (
                f"test $(stat -c%s {shlex.quote(destination)} 2>/dev/null || echo 0) "
                f"-eq {model.size}"
                if model.size is not None
                else f"test -s {shlex.quote(destination)}"
            )
            partial_size_check = (
                f"test $(stat -c%s {shlex.quote(partial)}) -eq {model.size}"
                if model.size is not None
                else f"test -s {shlex.quote(partial)}"
            )
            destination_hash_check = (
                f"test $(sha256sum {shlex.quote(destination)} | cut -d' ' -f1) "
                f"= {shlex.quote(model.sha256)}"
                if model.sha256 is not None
                else "true"
            )
            partial_hash_check = (
                f"test $(sha256sum {shlex.quote(partial)} | cut -d' ' -f1) "
                f"= {shlex.quote(model.sha256)}"
                if model.sha256 is not None
                else "true"
            )
            destination_check = (
                f"({destination_size_check} && {destination_hash_check})"
            )
            partial_check = f"({partial_size_check} && {partial_hash_check})"
            partial_directory = str(Path(partial).parent)
            partial_name = Path(partial).name
            completion_check = f'test "$aria_ok" -eq 1 && {partial_check}'
            downloads.append(
                f"({destination_check} || ("
                f"completed=0; "
                f"for attempt in $(seq 1 24); do "
                f"aria_ok=0; "
                f"timeout --signal=INT 600 aria2c --continue=true "
                f"--max-connection-per-server=4 --split=4 --min-split-size=16M "
                f"--file-allocation=none --auto-file-renaming=false "
                f"--allow-overwrite=true --max-tries=5 --retry-wait=1 "
                f"--connect-timeout=30 --timeout=30 --summary-interval=10 "
                f"--console-log-level=warn --dir={shlex.quote(partial_directory)} "
                f"--out={shlex.quote(partial_name)} {shlex.quote(model.url)} "
                f"&& aria_ok=1 || true; "
                f"if {completion_check}; then completed=1; break; fi; "
                f"echo 'Retrying {model.path} with a fresh connection' >&2; "
                f"done; "
                f'test "$completed" -eq 1 && '
                f"rm -f {shlex.quote(partial + '.aria2')} && "
                f"mv {shlex.quote(partial)} {shlex.quote(destination)}"
                f"))"
            )
        package_setup = _quiet_package_setup(
            ("aria2",),
            check_command="command -v aria2c >/dev/null 2>&1",
        )
        print("Downloader package: aria2 (checking)", flush=True)
        self.run(package_setup, timeout=20 * 60)
        print("Downloader package: aria2 (ready)", flush=True)
        command_parts = ['pids=""']
        for download in downloads:
            command_parts.append(f"{download} & pids=\"$pids $!\"")
        command_parts.extend(
            [
                "status=0",
                'for pid in $pids; do wait "$pid" || status=1; done',
            ]
        )
        if model_aliases:
            alias_directories = " ".join(
                shlex.quote(str(Path(alias).parent)) for alias in model_aliases
            )
            alias_commands = " && ".join(
                f"ln -sfn {shlex.quote(source)} {shlex.quote(alias)}"
                for alias, source in model_aliases.items()
            )
            command_parts.append(
                f'if test "$status" -eq 0; then mkdir -p {alias_directories} '
                f"&& {alias_commands} || status=1; fi"
            )
        command_parts.append('exit "$status"')
        self.run("; ".join(command_parts), timeout=4 * 60 * 60)

    def ensure_system_packages(self, packages: tuple[str, ...]) -> None:
        if not packages:
            return
        label = ", ".join(packages)
        print(f"System packages: {label} (checking)", flush=True)
        self.run(
            _quiet_package_setup(packages),
            timeout=20 * 60,
        )
        print(f"System packages: {label} (ready)", flush=True)

    def ensure_comfy_args(
        self,
        args: tuple[str, ...],
        *,
        system_packages: tuple[str, ...] = (),
    ) -> None:
        digest = sha256("\0".join(args).encode()).hexdigest()
        quoted_args = " ".join(shlex.quote(arg) for arg in args)
        package_setup = ""
        if system_packages:
            label = ", ".join(system_packages)
            print(f"System packages: {label} (checking)", flush=True)
            package_setup = _quiet_package_setup(system_packages) + "; "
        self.run(
            package_setup
            + "pid_file=/tmp/comfyui.pid; marker=/tmp/runpod-video-comfy-args; "
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
            timeout=20 * 60 if system_packages else 5 * 60,
        )
        if system_packages:
            print(f"System packages: {label} (ready)", flush=True)

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
