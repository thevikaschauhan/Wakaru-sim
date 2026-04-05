from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

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

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}/api{path}"
        resp = self._session.get(url)
        self._raise_for_status(resp)
        return resp.json()

    def _post(self, path: str, json: dict | None = None, files=None, data=None) -> dict:
        url = f"{self.base_url}/api{path}"
        resp = self._session.post(url, json=json, files=files, data=data)
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if not resp.ok:
            try:
                msg = resp.json().get("error") or resp.json().get("message") or resp.text
            except Exception:
                msg = resp.text
            raise APIError(resp.status_code, msg)

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
        """
        Run the complete MiroFish pipeline end-to-end and return the final report.

        Args:
            files: List of file paths (PDF, MD, or TXT) to use as seed documents.
            requirement: Natural language description of what to predict/simulate.
            enable_twitter: Include Twitter simulation (default True).
            enable_reddit: Include Reddit simulation (default False for V1 speed).
            simulation_hours: How many simulated hours to run (default 24 for cart recovery).
            on_progress: Optional callback(stage: str, state: object) for progress updates.

        Returns:
            Report object with the full markdown prediction report.
        """
        def _progress(stage: str) -> Callable | None:
            if on_progress is None:
                return None
            return lambda state: on_progress(stage, state)

        # Step 1: Build graph
        project = self.generate_ontology(files, requirement)
        if on_progress:
            on_progress("ontology_generated", project)

        project = self.build_graph(project.project_id, on_progress=_progress("graph_building"))
        if on_progress:
            on_progress("graph_completed", project)

        # Step 2: Prepare simulation
        simulation = self.create_simulation(
            project_id=project.project_id,
            requirement=requirement,
            enable_twitter=enable_twitter,
            enable_reddit=enable_reddit,
        )
        simulation = self.prepare_simulation(simulation.simulation_id, on_progress=_progress("preparing"))
        if on_progress:
            on_progress("simulation_ready", simulation)

        # Step 3: Run simulation
        self.start_simulation(simulation.simulation_id)
        run_status = self.wait_for_simulation(
            simulation.simulation_id,
            timeout=simulation_hours * 120 + 600,  # generous bound
            on_progress=_progress("running"),
        )
        if run_status.runner_status == "failed":
            raise SimulationError(simulation.simulation_id, "Simulation runner failed")
        if on_progress:
            on_progress("simulation_completed", run_status)

        # Step 4: Generate report
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

            data = {"requirement": requirement}
            result = self._post(
                "/graph/ontology/generate",
                files=file_tuples,
                data=data,
            )
        finally:
            for fh in open_files:
                fh.close()

        return Project.from_dict(result.get("project", result))

    def build_graph(
        self,
        project_id: str,
        timeout: int = 180,
        on_progress: Callable[[Task], None] | None = None,
    ) -> Project:
        """Start graph build and poll until complete. Returns the finished Project."""
        result = self._post(f"/graph/build/{project_id}")
        task_id = result.get("task_id", "")

        def fetch() -> Task:
            return Task.from_dict(self._get(f"/graph/status/{task_id}"))

        def is_done(task: Task) -> bool:
            return task.status in ("COMPLETED", "FAILED")

        final_task = poll_until_done(
            fetch_fn=fetch,
            is_done_fn=is_done,
            operation=f"build_graph(project_id={project_id})",
            interval_seconds=3,
            timeout_seconds=timeout,
            on_progress=on_progress,
        )

        if final_task.status == "FAILED":
            raise APIError(500, f"Graph build failed: {final_task.error}")

        # Return the updated project
        return Project.from_dict(self._get(f"/project/{project_id}"))

    def get_project(self, project_id: str) -> Project:
        return Project.from_dict(self._get(f"/project/{project_id}"))

    # ------------------------------------------------------------------
    # Step 2: Simulation setup
    # ------------------------------------------------------------------

    def create_simulation(
        self,
        project_id: str,
        requirement: str,
        enable_twitter: bool = True,
        enable_reddit: bool = False,
    ) -> Simulation:
        """Create a new simulation for a project."""
        result = self._post("/simulation/create", json={
            "project_id": project_id,
            "simulation_requirement": requirement,
            "enable_twitter": enable_twitter,
            "enable_reddit": enable_reddit,
        })
        return Simulation.from_dict(result.get("simulation", result))

    def prepare_simulation(
        self,
        simulation_id: str,
        timeout: int = 1200,
        on_progress: Callable[[dict], None] | None = None,
    ) -> Simulation:
        """Start simulation preparation (profile + config generation) and poll until ready."""
        self._post(f"/simulation/{simulation_id}/prepare")

        def fetch() -> dict:
            return self._get(f"/simulation/{simulation_id}/prepare_status")

        def is_done(state: dict) -> bool:
            status = state.get("status", "")
            return status in ("READY", "FAILED")

        final = poll_until_done(
            fetch_fn=fetch,
            is_done_fn=is_done,
            operation=f"prepare_simulation({simulation_id})",
            interval_seconds=10,
            timeout_seconds=timeout,
            on_progress=on_progress,
        )

        if final.get("status") == "FAILED":
            raise SimulationError(simulation_id, final.get("error", "preparation failed"))

        return Simulation.from_dict(
            self._get(f"/simulation/{simulation_id}").get("simulation", {})
        )

    def get_simulation(self, simulation_id: str) -> Simulation:
        result = self._get(f"/simulation/{simulation_id}")
        return Simulation.from_dict(result.get("simulation", result))

    # ------------------------------------------------------------------
    # Step 3: Run simulation
    # ------------------------------------------------------------------

    def start_simulation(self, simulation_id: str) -> None:
        """Launch the OASIS simulation processes."""
        self._post(f"/simulation/{simulation_id}/start")

    def get_run_status(self, simulation_id: str, include_actions: bool = False) -> RunStatus:
        """Fetch the current execution state of a running simulation."""
        path = f"/simulation/{simulation_id}/run_status_detail" if include_actions else f"/simulation/{simulation_id}/run_status"
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
        self._post(f"/simulation/{simulation_id}/stop")

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
        self._post(f"/report/{simulation_id}/generate")

        def fetch() -> dict:
            return self._get(f"/report/{simulation_id}/status")

        def is_done(state: dict) -> bool:
            return state.get("status") in ("completed", "failed")

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

        result = self._get(f"/report/{simulation_id}/full")
        return Report.from_dict({"simulation_id": simulation_id, "status": "completed", **result})

    def get_report(self, simulation_id: str) -> Report:
        result = self._get(f"/report/{simulation_id}/full")
        return Report.from_dict({"simulation_id": simulation_id, "status": "completed", **result})

    # ------------------------------------------------------------------
    # Step 5: Interact
    # ------------------------------------------------------------------

    def interview_agent(self, simulation_id: str, query: str) -> str:
        """Ask a question to the simulation's ReportAgent or a specific agent."""
        result = self._post(f"/report/{simulation_id}/interview", json={"query": query})
        return result.get("response", result.get("answer", ""))
