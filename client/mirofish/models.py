from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Project (graph build)
# ---------------------------------------------------------------------------

@dataclass
class Project:
    project_id: str
    name: str
    status: str                        # CREATED | ONTOLOGY_GENERATED | GRAPH_BUILDING | GRAPH_COMPLETED | FAILED
    simulation_requirement: str = ""
    graph_id: str = ""
    ontology: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        return cls(
            project_id=d.get("project_id", ""),
            name=d.get("name", ""),
            status=d.get("status", ""),
            simulation_requirement=d.get("simulation_requirement", ""),
            graph_id=d.get("graph_id", ""),
            ontology=d.get("ontology") or {},
            error=d.get("error", ""),
        )


# ---------------------------------------------------------------------------
# Task (async background job)
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    task_type: str
    status: str                        # PENDING | PROCESSING | COMPLETED | FAILED
    progress: int = 0
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            task_id=d.get("task_id", ""),
            task_type=d.get("task_type", ""),
            status=d.get("status", ""),
            progress=d.get("progress", 0),
            message=d.get("message", ""),
            result=d.get("result") or {},
            error=d.get("error", ""),
        )


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class Simulation:
    simulation_id: str
    project_id: str
    status: str                        # CREATED | PREPARING | READY | RUNNING | COMPLETED | STOPPED | FAILED
    simulation_requirement: str = ""
    enable_twitter: bool = True
    enable_reddit: bool = True
    entity_count: int = 0
    profile_count: int = 0
    error: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Simulation":
        return cls(
            simulation_id=d.get("simulation_id", ""),
            project_id=d.get("project_id", ""),
            status=d.get("status", ""),
            simulation_requirement=d.get("simulation_requirement", ""),
            enable_twitter=d.get("enable_twitter", True),
            enable_reddit=d.get("enable_reddit", True),
            entity_count=d.get("entity_count", 0),
            profile_count=d.get("profile_count", 0),
            error=d.get("error", ""),
        )


# ---------------------------------------------------------------------------
# Run status (real-time execution state)
# ---------------------------------------------------------------------------

@dataclass
class RunStatus:
    simulation_id: str
    runner_status: str                 # running | completed | stopped | failed
    current_round: int = 0
    total_rounds: int = 0
    simulated_hours: float = 0.0
    progress_percent: float = 0.0
    twitter_current_round: int = 0
    reddit_current_round: int = 0
    twitter_completed: bool = False
    reddit_completed: bool = False
    twitter_actions_count: int = 0
    reddit_actions_count: int = 0
    total_actions_count: int = 0
    recent_actions: list["AgentAction"] = field(default_factory=list)

    @property
    def is_done(self) -> bool:
        return self.runner_status in ("completed", "stopped", "failed")

    @classmethod
    def from_dict(cls, d: dict) -> "RunStatus":
        return cls(
            simulation_id=d.get("simulation_id", ""),
            runner_status=d.get("runner_status", ""),
            current_round=d.get("current_round", 0),
            total_rounds=d.get("total_rounds", 0),
            simulated_hours=d.get("simulated_hours", 0.0),
            progress_percent=d.get("progress_percent", 0.0),
            twitter_current_round=d.get("twitter_current_round", 0),
            reddit_current_round=d.get("reddit_current_round", 0),
            twitter_completed=d.get("twitter_completed", False),
            reddit_completed=d.get("reddit_completed", False),
            twitter_actions_count=d.get("twitter_actions_count", 0),
            reddit_actions_count=d.get("reddit_actions_count", 0),
            total_actions_count=d.get("total_actions_count", 0),
            recent_actions=[
                AgentAction.from_dict(a)
                for a in d.get("recent_actions", [])
            ],
        )


# ---------------------------------------------------------------------------
# Agent action (one event inside the simulation)
# ---------------------------------------------------------------------------

@dataclass
class AgentAction:
    round_num: int
    timestamp: str
    platform: str                      # twitter | reddit
    agent_id: int
    agent_name: str
    action_type: str                   # CREATE_POST | LIKE | COMMENT | REPOST | FOLLOW | etc.
    action_args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    success: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "AgentAction":
        return cls(
            round_num=d.get("round_num", 0),
            timestamp=d.get("timestamp", ""),
            platform=d.get("platform", ""),
            agent_id=d.get("agent_id", 0),
            agent_name=d.get("agent_name", ""),
            action_type=d.get("action_type", ""),
            action_args=d.get("action_args") or {},
            result=d.get("result", ""),
            success=d.get("success", True),
        )


# ---------------------------------------------------------------------------
# Round summary
# ---------------------------------------------------------------------------

@dataclass
class RoundSummary:
    round_num: int
    platform: str
    active_agents: int
    actions_count: int
    simulated_time: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "RoundSummary":
        return cls(
            round_num=d.get("round_num", 0),
            platform=d.get("platform", ""),
            active_agents=d.get("active_agents", 0),
            actions_count=d.get("actions_count", 0),
            simulated_time=d.get("simulated_time", ""),
        )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class Report:
    simulation_id: str
    status: str                        # generating | completed | failed
    content: str = ""                  # Full markdown report
    summary: str = ""
    error: str = ""

    @property
    def is_ready(self) -> bool:
        return self.status == "completed" and bool(self.content)

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        return cls(
            simulation_id=d.get("simulation_id", ""),
            status=d.get("status", ""),
            content=d.get("content", d.get("report", "")),
            summary=d.get("summary", ""),
            error=d.get("error", ""),
        )
