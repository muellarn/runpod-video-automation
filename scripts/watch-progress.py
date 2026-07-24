#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


DOWNLOAD_RE = re.compile(
    r"^\[#(?P<id>\w+) (?P<done>[\d.]+(?:MiB|GiB))/"
    r"(?P<total>[\d.]+(?:MiB|GiB))\((?P<percent>\d+)%\) "
    r"CN:(?P<connections>\d+) DL:(?P<rate>[^ ]+)"
    r"(?: ETA:(?P<eta>[^]]+))?"
)
PROGRESS_RE = re.compile(
    r"^\[(?P<kind>start image|shot) (?P<index>\d+)/(?P<total>\d+)\] "
    r"ComfyUI progress (?P<node>\S+) (?P<class_type>[^:]+): "
    r"(?P<value>\d+)/(?P<maximum>\d+) \((?P<percent>\d+)%\)"
)
NODE_RE = re.compile(
    r"^\[(?P<kind>start image|shot) (?P<index>\d+)/(?P<total>\d+)\] "
    r"ComfyUI node (?P<node>\S+): (?P<class_type>.+)"
)
COMPLETE_RE = re.compile(
    r"^\[(?P<kind>start image|shot) (?P<index>\d+)/(?P<total>\d+)\] "
    r"ComfyUI execution completed"
)
DOWNLOAD_COMPLETE_RE = re.compile(r"^(?P<id>[A-Za-z0-9]+)\|OK")


@dataclass
class TaskProgress:
    total: int
    current_node: str = "waiting"
    nodes: dict[str, int] = field(default_factory=dict)
    completed: bool = False


@dataclass
class Status:
    shot_total: int = 0
    download: str = "not required or not started"
    download_complete: bool = False
    active_downloads: set[str] = field(default_factory=set)
    start_images: dict[int, TaskProgress] = field(default_factory=dict)
    shots: dict[int, TaskProgress] = field(default_factory=dict)
    outputs: set[str] = field(default_factory=set)
    assembled: str | None = None
    last_event: str = "starting"
    error: str | None = None

    def parse(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if match := re.match(r"^Shots: (\d+)", line):
            self.shot_total = int(match.group(1))
        if match := DOWNLOAD_RE.match(line):
            self.active_downloads.add(match.group("id"))
            self.download_complete = False
            eta = f", ETA {match.group('eta')}" if match.group("eta") else ""
            self.download = (
                f"{match.group('done')}/{match.group('total')} "
                f"({match.group('percent')}%), {match.group('rate')}, "
                f"{match.group('connections')} connections{eta}"
            )
            self.last_event = f"model download {self.download}"
            return
        if match := DOWNLOAD_COMPLETE_RE.match(line):
            self.active_downloads.discard(match.group("id"))
            self.download_complete = not self.active_downloads
            self.last_event = (
                "model downloads completed"
                if self.download_complete
                else f"{len(self.active_downloads)} model downloads remaining"
            )
            return
        if match := PROGRESS_RE.match(line):
            target = self.start_images if match.group("kind") == "start image" else self.shots
            index = int(match.group("index"))
            task = target.setdefault(index, TaskProgress(total=int(match.group("total"))))
            node = match.group("node")
            task.nodes[node] = int(match.group("percent"))
            task.current_node = f"{match.group('class_type')} node {node}"
            self.last_event = line
            return
        if match := NODE_RE.match(line):
            target = self.start_images if match.group("kind") == "start image" else self.shots
            index = int(match.group("index"))
            task = target.setdefault(index, TaskProgress(total=int(match.group("total"))))
            task.current_node = f"{match.group('class_type')} node {match.group('node')}"
            self.last_event = line
            return
        if match := COMPLETE_RE.match(line):
            target = self.start_images if match.group("kind") == "start image" else self.shots
            index = int(match.group("index"))
            task = target.setdefault(index, TaskProgress(total=int(match.group("total"))))
            task.completed = True
            task.current_node = "complete"
            self.last_event = line
            return
        if line.startswith("Generated start keyframe:"):
            self.last_event = line
        elif line.startswith("Downloaded:"):
            output = line.partition(":")[2].strip()
            self.outputs.add(output)
            self.last_event = line
        elif line.startswith("Scene assembled:"):
            self.assembled = line.partition(":")[2].strip()
            self.last_event = line
        elif line.startswith("Shot rendered:"):
            self.assembled = line.partition(":")[2].strip()
            self.last_event = line
        elif line.startswith("Error:"):
            self.error = line
            self.last_event = line

    @staticmethod
    def _task_text(label: str, index: int, task: TaskProgress) -> str:
        if task.completed:
            return f"{label} {index}/{task.total}: complete"
        phases = []
        if "57" in task.nodes:
            phases.append(f"high {task.nodes['57']}%")
        if "58" in task.nodes:
            phases.append(f"low {task.nodes['58']}%")
        if "3" in task.nodes:
            phases.append(f"sampling {task.nodes['3']}%")
        progress = ", ".join(phases) if phases else task.current_node
        return f"{label} {index}/{task.total}: {progress}"

    def summary(self, running: bool, output_dir: Path) -> str:
        parts = []
        if self.download_complete:
            parts.append("models: complete")
        elif self.download != "not required or not started":
            parts.append(f"models: {self.download}")
        for index, task in sorted(self.start_images.items()):
            parts.append(self._task_text("start image", index, task))
        for index, task in sorted(self.shots.items()):
            parts.append(self._task_text("shot", index, task))
        output_count = len(self.outputs)
        if output_dir.exists():
            output_count = max(
                output_count,
                len(list(output_dir.rglob("*.webm"))),
            )
        parts.append(f"outputs: {output_count}")
        if self.assembled:
            parts.append(f"assembled: {self.assembled}")
        if self.error:
            parts.append(self.error)
        parts.append("orchestrator: running" if running else "orchestrator: stopped")
        return " | ".join(parts)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("pid", type=int)
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    status = Status()
    position = 0
    while True:
        if args.log.exists():
            with args.log.open(errors="replace") as log_file:
                log_file.seek(position)
                for line in log_file:
                    status.parse(line)
                position = log_file.tell()
        running = process_exists(args.pid)
        print(
            f"{time.strftime('%H:%M:%S')} {status.summary(running, args.output)}",
            flush=True,
        )
        if not running:
            return 0 if status.assembled else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
