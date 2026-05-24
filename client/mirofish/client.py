from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import requests

from .exceptions import APIError, SimulationError
from .models import AgentAction, Project, Report, RoundSummary, RunStatus, Simulation, Task
from .polling import poll_until_done


class MiroFishClient:
    """
    Python client for the MiroFish prediction engine REST API.

    Usage:
        client = MiroFishClient("http://localhost:5001")
        report = client.run_full_pipeline(
            files=["customer_data.txt"],
            requirement="Simulate the psychology of this shopper and predict why they abandoned the cart."
        )
        print(report.content)
    """

    def __init__(self, base_url: str = "http://localhost:5001"):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        url = f"{self.base_url}/api{path}"
        resp = self._session.get(url)
        return self._unwrap(resp)

    def _post(self, path: str, json: dict | None = None, files=None, data=None) -> Any:
        url = f"{self.base_url}/api{path}"
        resp = self._session.post(url, json=json, files=files, data=data)
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp: requests.Response) -> Any:
        """Validate HTTP status and unwrap the backend's {success, data, error} envelope."""
        try:
            body = resp.json()
        except ValueError:
            body = None

        if not resp.ok:
            msg = ""
            if isinstance(body, dict):
                msg = body.get("error") or body.get("message") or ""
            raise APIError(resp.status_code, msg or resp.text[:200])

        if not isinstance(body, dict):
            raise APIError(resp.status_code, f"Expected JSON object, got: {resp.text[:200]}")

        if body.get("success") is False:
            raise APIError(resp.status_code, body.get("error") or body.get("message") or "request failed")

        return body.get("data", body)

    # ------------------------------------------------------------------
    # High-level: full pipeline
    # ------------------------------------------------------------------

    def run_full_pipeline(
        self,
        files: list[str | Path],
        requirement: str,
        enable_twitter: bool = True,
        enable_reddit: bool = False,
        simulation_hours: int = 24,
        on_progress: Callable[[str, object], None] | None = None,
    ) -> Report:
        """Run the complete MiroFish pipeline end-to-end and return the final report."""
        def _progress(stage: str) -> Callable | None:
            if on_progress is None:
                return None
            return lambda state: on_progress(stage, state)

        project = self.generate_ontology(files, requirement)
        if on_progress:
            on_progress("ontology_generated", project)

        project = self.build_graph(project.project_id, on_progress=_progress("graph_building"))
        if on_progress:
            on_progress("graph_completed", project)

        simulation = self.create_simulation(
            project_id=project.project_id,
            enable_twitter=enable_twitter,
            enable_reddit=enable_reddit,
        )
        simulation = self.prepare_simulation(simulation.simulation_id, on_progress=_progress("preparing"))
        if on_progress:
            on_progress("simulation_ready", simulation)

        self.start_simulation(simulation.simulation_id)
        run_status = self.wait_for_simulation(
            simulation.simulation_id,
            timeout=simulation_hours * 120 + 600,
            on_progress=_progress("running"),
        )
        if run_status.runner_status == "failed":
            raise SimulationError(simulation.simulation_id, "Simulation runner failed")
        if on_progress:
            on_progress("simulation_completed", run_status)

        report = self.generate_report(simulation.simulation_id, on_progress=_progress("generating_report"))
        return report

    # ------------------------------------------------------------------
    # Step 1: Graph / project
    # ------------------------------------------------------------------

    def generate_ontology(
        self,
        files: list[str | Path],
        requirement: str,
    ) -> Project:
        """Upload seed documents and generate the entity ontology. Returns a Project."""
        open_files = []
        try:
            file_tuples = []
            for f in files:
                fh = open(f, "rb")
                open_files.append(fh)
                file_tuples.append(("files", (Path(f).name, fh)))

            data = {"simulation_requirement": requirement}
            result = self._post(
                "/graph/ontology/generate",
                files=file_tuples,
                data=data,
            )
        finally:
            for fh in open_files:
                fh.close()

        return Project.from_dict(result)

    def build_graph(
        self,
        project_id: str,
        timeout: int = 180,
        on_progress: Callable[[Task], None] | None = None,
    ) -> Project:
        """Start graph build and poll until complete. Returns the finished Project."""
        result = self._post("/graph/build", json={"project_id": project_id})
        task_id = result.get("task_id", "")

        def fetch() -> Task:
            return Task.from_dict(self._get(f"/graph/task/{task_id}"))

        def is_done(task: Task) -> bool:
            return task.status in ("completed", "failed")

        final_task = poll_until_done(
            fetch_fn=fetch,
            is_done_fn=is_done,
            operation=f"build_graph(project_id={project_id})",
            interval_seconds=3,
            timeout_seconds=timeout,
            on_progress=on_progress,
        )

        if final_task.status == "failed":
            raise APIError(500, f"Graph build failed: {final_task.error}")

        return Project.from_dict(self._get(f"/graph/project/{project_id}"))

    def get_project(self, project_id: str) -> Project:
        return Project.from_dict(self._get(f"/graph/project/{project_id}"))

    # ------------------------------------------------------------------
    # Step 2: Simulation setup
    # ------------------------------------------------------------------

    def create_simulation(
        self,
        project_id: str,
        enable_twitter: bool = True,
        enable_reddit: bool = False,
    ) -> Simulation:
        """Create a new simulation for a project."""
        result = self._post("/simulation/create", json={
            "project_id": project_id,
            "enable_twitter": enable_twitter,
            "enable_reddit": enable_reddit,
        })
        return Simulation.from_dict(result)

    def prepare_simulation(
        self,
        simulation_id: str,
        timeout: int = 1200,
        on_progress: Callable[[dict], None] | None = None,
    ) -> Simulation:
        """Start simulation preparation (profile + config generation) and poll until ready."""
        prepare = self._post("/simulation/prepare", json={"simulation_id": simulation_id})

        if not prepare.get("already_prepared"):
            task_id = prepare.get("task_id", "")

            def fetch() -> dict:
                return self._post(
                    "/simulation/prepare/status",
                    json={"task_id": task_id, "simulation_id": simulation_id},
                )

            def is_done(state: dict) -> bool:
                return state.get("status", "") in ("ready", "completed", "failed")

            final = poll_until_done(
                fetch_fn=fetch,
                is_done_fn=is_done,
                operation=f"prepare_simulation({simulation_id})",
                interval_seconds=10,
                timeout_seconds=timeout,
                on_progress=on_progress,
            )

            if final.get("status") == "failed":
                raise SimulationError(simulation_id, final.get("error", "preparation failed"))

        return Simulation.from_dict(self._get(f"/simulation/{simulation_id}"))

    def get_simulation(self, simulation_id: str) -> Simulation:
        return Simulation.from_dict(self._get(f"/simulation/{simulation_id}"))

    # ------------------------------------------------------------------
    # Step 3: Run simulation
    # ------------------------------------------------------------------

    def start_simulation(self, simulation_id: str) -> None:
        """Launch the OASIS simulation processes."""
        self._post("/simulation/start", json={"simulation_id": simulation_id})

    def get_run_status(self, simulation_id: str, include_actions: bool = False) -> RunStatus:
        """Fetch the current execution state of a running simulation."""
        path = (
            f"/simulation/{simulation_id}/run-status/detail"
            if include_actions
            else f"/simulation/{simulation_id}/run-status"
        )
        return RunStatus.from_dict(self._get(path))

    def wait_for_simulation(
        self,
        simulation_id: str,
        timeout: int = 3600,
        on_progress: Callable[[RunStatus], None] | None = None,
    ) -> RunStatus:
        """Poll until the simulation finishes (completed/stopped/failed)."""
        return poll_until_done(
            fetch_fn=lambda: self.get_run_status(simulation_id),
            is_done_fn=lambda s: s.is_done,
            operation=f"run_simulation({simulation_id})",
            interval_seconds=30,
            timeout_seconds=timeout,
            on_progress=on_progress,
        )

    def stop_simulation(self, simulation_id: str) -> None:
        self._post("/simulation/stop", json={"simulation_id": simulation_id})

    def get_actions(self, simulation_id: str) -> list[AgentAction]:
        result = self._get(f"/simulation/{simulation_id}/actions")
        return [AgentAction.from_dict(a) for a in result.get("actions", [])]

    def get_timeline(self, simulation_id: str) -> list[RoundSummary]:
        result = self._get(f"/simulation/{simulation_id}/timeline")
        return [RoundSummary.from_dict(r) for r in result.get("timeline", [])]

    # ------------------------------------------------------------------
    # Step 4: Report
    # ------------------------------------------------------------------

    def generate_report(
        self,
        simulation_id: str,
        timeout: int = 300,
        on_progress: Callable[[dict], None] | None = None,
    ) -> Report:
        """Start report generation and poll until complete. Returns the finished Report."""
        start = self._post("/report/generate", json={"simulation_id": simulation_id})
        task_id = start.get("task_id", "")

        if not start.get("already_generated"):
            def fetch() -> dict:
                return self._post(
                    "/report/generate/status",
                    json={"task_id": task_id, "simulation_id": simulation_id},
                )

            def is_done(state: dict) -> bool:
                return state.get("status", "") in ("completed", "failed")

            final_status = poll_until_done(
                fetch_fn=fetch,
                is_done_fn=is_done,
                operation=f"generate_report({simulation_id})",
                interval_seconds=5,
                timeout_seconds=timeout,
                on_progress=on_progress,
            )

            if final_status.get("status") == "failed":
                raise APIError(500, f"Report generation failed: {final_status.get('error', '')}")

        return self._fetch_report(simulation_id)

    def get_report(self, simulation_id: str) -> Report:
        return self._fetch_report(simulation_id)

    def _fetch_report(self, simulation_id: str) -> Report:
        result = self._get(f"/report/by-simulation/{simulation_id}")
        return Report.from_dict({
            "simulation_id": simulation_id,
            "status": result.get("status", "completed"),
            "content": result.get("markdown_content", ""),
        })

    # ------------------------------------------------------------------
    # Step 5: Interact
    # ------------------------------------------------------------------

    def chat_with_report(
        self,
        simulation_id: str,
        message: str,
        chat_history: list[dict] | None = None,
    ) -> dict:
        """Ask a follow-up question to the simulation's ReportAgent."""
        return self._post("/report/chat", json={
            "simulation_id": simulation_id,
            "message": message,
            "chat_history": chat_history or [],
        })

    def interview_agent(
        self,
        simulation_id: str,
        agent_id: int,
        prompt: str,
        platform: str | None = None,
        timeout: int = 60,
    ) -> dict:
        """Interview a specific in-simulation agent by id. Requires the simulation env to be running."""
        body: dict = {
            "simulation_id": simulation_id,
            "agent_id": agent_id,
            "prompt": prompt,
            "timeout": timeout,
        }
        if platform is not None:
            body["platform"] = platform
        return self._post("/simulation/interview", json=body)
