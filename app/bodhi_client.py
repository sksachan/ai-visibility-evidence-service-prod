from __future__ import annotations

import json
import os
import time
from typing import Any

import requests


class BodhiClient:
    """Small Bodhi API client for server-side Railway orchestration.

    Important behaviour:
    - Use the canonical API base URL directly, for example
      https://sapientaiproducts.com/save
    - Do not rely on Bodhi _links because some responses currently contain
      duplicate /save path fragments.
    - UI nodes are submitted through HITL tasks after run creation.
    """

    def __init__(self, base_url: str | None = None, token: str | None = None):
        self.base_url = (base_url or os.getenv("BODHI_API_BASE_URL") or "https://sapientaiproducts.com/save").rstrip("/")
        self.token = token or os.getenv("BODHI_PAT_TOKEN") or ""
        self.timeout = int(os.getenv("BODHI_HTTP_TIMEOUT_SECONDS", "120"))

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        resp = requests.request(method, url, headers=self.headers(), timeout=self.timeout, allow_redirects=True, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"Bodhi {method} {path} failed: {resp.status_code} {resp.text[:500]}")
        if not resp.text:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}

    def trigger_task_run(self, task_id: str, workflow_id: str | None = None, run_name: str | None = None, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"runName": run_name or "AI Visibility run"}
        overrides: dict[str, Any] = {}
        if workflow_id:
            overrides["workflow"] = workflow_id
        # Inputs are not enough for Bodhi UI nodes; they still need HITL completion.
        # Keep them in payload for workflows that read overrides directly, then submit
        # the same data via HITL after the run reaches the UI node.
        if inputs:
            overrides.update(inputs)
            payload["inputs"] = inputs
        if overrides:
            payload["overrides"] = overrides
        return self._request("POST", f"/api/v1/tasks/{task_id}/runs", data=json.dumps(payload))

    def list_task_runs(self, task_id: str) -> Any:
        return self._request("GET", f"/api/v1/tasks/{task_id}/runs")

    def get_task_run(self, task_id: str, run_id: str) -> Any:
        return self._request("GET", f"/api/v1/tasks/{task_id}/runs/{run_id}")

    def get_run_file(self, run_id: str, srcfile: str) -> Any:
        return self._request("GET", f"/api/v1/tasks/runs/{run_id}/files", params={"srcfile": srcfile})

    def get_hitl_tasks(self, run_id: str) -> list[dict[str, Any]]:
        payload = self._request("GET", f"/api/v1/tasks/runs/{run_id}/hitltasks")
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("hitltasks", "hitlTasks", "tasks", "items", "data"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
                if isinstance(val, dict):
                    for sub in ("hitltasks", "tasks", "items"):
                        arr = val.get(sub)
                        if isinstance(arr, list):
                            return [x for x in arr if isinstance(x, dict)]
        return []

    def get_pending_hitl(self, run_id: str) -> dict[str, Any] | None:
        for task in self.get_hitl_tasks(run_id):
            status = str(task.get("status") or task.get("state") or "").lower()
            if status == "pending":
                return task
        return None

    def submit_hitl(self, run_id: str, hitl_task_id: str, response_data: dict[str, Any]) -> Any:
        return self._request(
            "POST",
            f"/api/v1/tasks/runs/{run_id}/hitltasks",
            data=json.dumps({
                "hitltasks": [
                    {
                        "id": hitl_task_id,
                        "status": "completed",
                        "response": response_data,
                    }
                ]
            }),
        )

    def wait_for_pending_hitl(self, run_id: str, timeout_seconds: int = 240, poll_seconds: int = 2) -> dict[str, Any] | None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            pending = self.get_pending_hitl(run_id)
            if pending:
                return pending
            time.sleep(poll_seconds)
        return None

    def submit_first_ui_hitl(
        self,
        run_id: str,
        response_data: dict[str, Any],
        timeout_seconds: int | None = None,
        poll_seconds: int | None = None,
        required: bool = False,
    ) -> dict[str, Any] | None:
        """Submit the first pending HITL/UI-node task for a Bodhi run.

        Bodhi API-created runs pause at UI nodes. The expected response object is
        the UI field-label dictionary, e.g. {"brand": "Nissan", ...}.
        """
        timeout = timeout_seconds if timeout_seconds is not None else int(os.getenv("BODHI_HITL_TIMEOUT_SECONDS", "240"))
        poll = poll_seconds if poll_seconds is not None else int(os.getenv("BODHI_HITL_POLL_SECONDS", "2"))
        pending = self.wait_for_pending_hitl(run_id, timeout_seconds=timeout, poll_seconds=poll)
        if not pending:
            if required:
                raise TimeoutError(f"No pending HITL/UI task found for Bodhi run {run_id} within {timeout}s")
            return None
        hitl_id = str(pending.get("id") or pending.get("taskId") or pending.get("hitlTaskId") or "")
        if not hitl_id:
            raise RuntimeError(f"Pending HITL task did not include an id: {str(pending)[:500]}")
        submit_response = self.submit_hitl(run_id, hitl_id, response_data)
        return {"hitl_task_id": hitl_id, "hitl_task": pending, "submit_response": submit_response}

    @staticmethod
    def extract_run_id(payload: Any) -> str | None:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return None
        candidates = [
            payload.get("run_id"), payload.get("runId"), payload.get("id"),
            (payload.get("data") or {}).get("run_id") if isinstance(payload.get("data"), dict) else None,
            (payload.get("data") or {}).get("runId") if isinstance(payload.get("data"), dict) else None,
            (payload.get("run") or {}).get("id") if isinstance(payload.get("run"), dict) else None,
        ]
        for c in candidates:
            if c:
                return str(c)
        return None

    @staticmethod
    def normalise_runs(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ["runs", "data", "items", "results"]:
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
                if isinstance(val, dict):
                    for sub in ["runs", "items", "results"]:
                        arr = val.get(sub)
                        if isinstance(arr, list):
                            return [x for x in arr if isinstance(x, dict)]
        return []

    @staticmethod
    def run_status(run: dict[str, Any]) -> str:
        for key in ["status", "state", "runStatus"]:
            if run.get(key):
                return str(run[key]).lower()
        return "unknown"

    def wait_for_run(self, task_id: str, run_id: str, timeout_seconds: int = 900, poll_seconds: int = 10) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        last: dict[str, Any] = {"run_id": run_id, "status": "unknown"}
        complete = {"completed", "success", "succeeded", "finished", "done"}
        failed = {"failed", "error", "cancelled", "canceled"}
        while time.time() < deadline:
            try:
                run = self.get_task_run(task_id, run_id)
                if isinstance(run, dict):
                    last = run
                    status = self.run_status(run)
                    if status in complete:
                        return run
                    if status in failed:
                        raise RuntimeError(f"Bodhi run failed: {run_id} status={status} payload={str(run)[:500]}")
            except RuntimeError:
                raise
            except Exception:
                payload = self.list_task_runs(task_id)
                runs = self.normalise_runs(payload)
                for run in runs:
                    rid = self.extract_run_id(run) or str(run.get("id") or "")
                    if rid == run_id:
                        last = run
                        status = self.run_status(run)
                        if status in complete:
                            return run
                        if status in failed:
                            raise RuntimeError(f"Bodhi run failed: {run_id} status={status} payload={str(run)[:500]}")
            time.sleep(poll_seconds)
        raise TimeoutError(f"Timed out waiting for Bodhi run {run_id}. Last={str(last)[:500]}")
