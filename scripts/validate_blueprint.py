import copy
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml


BASE = "http://127.0.0.1:8080/api/v1/main"
FLOW_PATH = Path("flows/github-issue-local-duplicate-review.yaml")
MODEL_REVISION = "751bff37182d3f1213fa05d7196b954e230abad9"


def request(path, method="GET", data=None, content_type=None):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def multipart(values):
    boundary = "kestra-" + uuid.uuid4().hex
    chunks = []
    for key, value in values.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        )
    chunks.append(f"--{boundary}--\r\n")
    return "".join(chunks).encode(), f"multipart/form-data; boundary={boundary}"


def check_metadata(flow):
    assert flow["id"] == FLOW_PATH.stem
    metadata = flow["extend"]
    fields = {
        "title", "metaTitle", "shortDescription", "metaDescription",
        "description", "tags", "subTags", "ee", "demo",
    }
    assert fields <= set(metadata), "Missing catalog metadata"
    assert len(metadata["metaDescription"]) <= 160
    assert 1500 <= len(metadata["description"]) <= 3000
    assert metadata["tags"] == ["AI"]
    assert set(metadata["subTags"]) <= {
        "Agents & Structured Output", "Monitoring & Alerting", "Vector Stores & RAG",
        "Content Generation", "Classification & Moderation", "Policy & Compliance",
        "Incident Response", "RAG & Knowledge Retrieval", "Testing & Quality",
    }
    assert metadata["ee"] is False and metadata["demo"] is False
    for heading in (
        "How it works", "What you get", "Who it's for",
        "Why orchestrate this with Kestra", "Prerequisites",
        "Quick start", "How to extend", "Links",
    ):
        assert f"## {heading}" in metadata["description"]
    assert "### Secrets" in metadata["description"]
    assert flow["triggers"][0]["disabled"] is True
    print("Catalog metadata, taxonomy and disabled schedule verified", flush=True)


def wait_for_server():
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
    raise TimeoutError("Local Kestra did not become healthy")


def create_flow(flow):
    source = yaml.safe_dump(flow, sort_keys=False).encode()
    validation = request("/flows/validate", "POST", source, "application/x-yaml")
    print("Runtime validation:", json.dumps(validation), flush=True)
    if isinstance(validation, list):
        for result in validation:
            if result.get("constraints"):
                raise AssertionError(f"Kestra rejected the flow: {result}")
    created = request("/flows", "POST", source, "application/x-yaml")
    assert created["id"] == flow["id"] and created["namespace"] == flow["namespace"]


def execute(flow, inputs=None):
    data, content_type = multipart(inputs or {})
    suffix = f'/{flow["namespace"]}/{flow["id"]}'
    try:
        execution = request("/executions" + suffix, "POST", data, content_type)
    except urllib.error.HTTPError as error:
        if error.code not in (404, 405):
            raise
        print("Using the documented execution-trigger compatibility route", flush=True)
        execution = request("/executions/trigger" + suffix, "POST", data, content_type)
    execution_id = execution["id"]
    print(f'Running {flow["id"]}: execution {execution_id}', flush=True)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = request("/executions/" + execution_id)
        if result["state"]["current"] in {"SUCCESS", "FAILED", "KILLED", "WARNING", "CANCELLED"}:
            print(f'{execution_id}: {result["state"]["current"]}', flush=True)
            return result
        time.sleep(2)
    raise TimeoutError(f"Execution {execution_id} did not finish")


def report(execution):
    uri = execution["outputs"]["review_json"]
    assert isinstance(uri, str) and uri.startswith("kestra://")
    query = urllib.parse.urlencode({"path": uri})
    return request(f'/executions/{execution["id"]}/file?{query}')


def failed_at(execution, task_id):
    return any(
        task["taskId"] == task_id and task["state"]["current"] == "FAILED"
        for task in execution.get("taskRunList", [])
    )


FIXTURES = {
    "/duplicates": {
        "total_count": 3,
        "incomplete_results": False,
        "items": [
            {"number": 101, "title": "YAML block scalar highlighting loses nested indentation"},
            {"number": 102, "title": "YAML block scalar highlighting loses nested indentation"},
            {"number": 103, "title": "Add a beginner guide for setting up a local development environment"},
        ],
    },
    "/empty": {"total_count": 0, "incomplete_results": False, "items": []},
    "/malformed": {"message": "Intentional test-only malformed successful response"},
}


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in FIXTURES:
            self.send_error(404)
            return
        body = json.dumps(FIXTURES[self.path]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print("Test fixture:", format % args, flush=True)


def fixture_flow(original, suffix, endpoint):
    flow = copy.deepcopy(original)
    flow["id"] += "-" + suffix
    flow["namespace"] = "company.blueprint_test"
    fetch = next(task for task in flow["tasks"] if task["id"] == "fetch_issues")
    fetch["uri"] = "http://127.0.0.1:8766" + endpoint
    return flow


def main():
    original = yaml.safe_load(FLOW_PATH.read_text())
    check_metadata(original)
    original.pop("extend")
    wait_for_server()

    # The original submitted flow must call real GitHub and run the real CPU model.
    create_flow(original)
    live = execute(original)
    assert live["state"]["current"] == "SUCCESS", live
    live_report = report(live)
    assert live_report["scope"]["reviewed_open_issues"] >= 2
    assert live_report["revision"] == MODEL_REVISION
    assert live_report["dimensions"] == 384 and live_report["read_only"] is True
    print("REAL GITHUB + REAL PINNED MODEL:", json.dumps({
        "execution": live["id"],
        "issues": live_report["scope"]["reviewed_open_issues"],
        "candidates": len(live_report["candidates"]),
    }), flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", 8766), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        duplicates = fixture_flow(original, "fixture-duplicates", "/duplicates")
        create_flow(duplicates)
        result = execute(duplicates)
        assert result["state"]["current"] == "SUCCESS", result
        duplicate_report = report(result)
        pair = duplicate_report["candidates"][0]
        assert {pair["left"]["number"], pair["right"]["number"]} == {101, 102}
        assert pair["cosine_similarity"] > 0.999 and pair["lexical_jaccard"] == 1
        print("FIXTURE: identical titles produce a real-model match above0.999", flush=True)

        empty = fixture_flow(original, "fixture-empty", "/empty")
        create_flow(empty)
        result = execute(empty)
        assert result["state"]["current"] == "SUCCESS", result
        assert report(result)["status"] == "not_enough_open_issues"
        assert not any(task["taskId"] == "semantic_vectors" for task in result.get("taskRunList", []))
        print("FIXTURE: no data is explicit; model skipped, no fake healthy result", flush=True)

        malformed = fixture_flow(original, "fixture-malformed", "/malformed")
        create_flow(malformed)
        result = execute(malformed)
        assert result["state"]["current"] == "FAILED" and failed_at(result, "normalize")
        print("FIXTURE: malformed successful HTTP response fails at normalization", flush=True)

        result = execute(original, {"repository": "../invalid?repository"})
        assert result["state"]["current"] == "FAILED" and failed_at(result, "validate_inputs")
        assert not any(task["taskId"] == "fetch_issues" for task in result.get("taskRunList", []))
        print("INPUT GUARD: invalid repository fails before any fetch", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("All catalog, real-runtime and defensive-path checks passed", flush=True)


if __name__ == "__main__":
    main()
