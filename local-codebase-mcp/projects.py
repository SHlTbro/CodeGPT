"""项目数据结构与只读 registry（id → Project），声明序保留。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from local_agents.config import ProjectAgentConfig


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    path: Path
    write_enabled: bool = False
    write_roots: tuple[str, ...] = ("docs",)  # v1.7：项目级可写目录白名单（默认 docs 保持 v1.2 行为）
    agent: ProjectAgentConfig = field(default_factory=ProjectAgentConfig)


class ProjectRegistry:
    """只读映射，禁止运行期增删（项目集合只由 config.toml 决定）。"""

    def __init__(self, projects: list[Project]):
        self._projects: dict[str, Project] = {p.id: p for p in projects}

    def __getitem__(self, project_id: str) -> Project:
        return self._projects[project_id]

    def __contains__(self, project_id: object) -> bool:
        return project_id in self._projects

    def __len__(self) -> int:
        return len(self._projects)

    def __iter__(self) -> Iterator[Project]:
        return iter(self._projects.values())

    def get(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def items(self) -> Iterator[tuple[str, Project]]:
        return iter(self._projects.items())
