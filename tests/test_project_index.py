"""The project index lives on disk, not in the browser (P2).

The dashboard used to learn that a project existed only from the browser's
``localStorage``. Wiping the WebView profile left every project folder on disk
and none of them reachable from the app. ``GET /api/projects`` now reads the
projects directory, so the filesystem is the authority.
"""

from __future__ import annotations

import json
import os

import pytest

from modules import project_api


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(project_api, "PROJECTS_DIR", str(root))
    return root


def make(dxf_name="plan.dxf", api=project_api):
    manifest = api.create_project()
    return api.attach_dxf(manifest["project_id"], dxf_name, b"0\nSECTION\n0\nEOF\n")


def test_every_project_on_disk_is_listed(projects_dir):
    a = make("house.dxf")
    b = make("clinic.dxf")
    listed = {p["project_id"] for p in project_api.list_projects()["projects"]}
    assert listed == {a["project_id"], b["project_id"]}


def test_a_listed_project_carries_what_the_dashboard_shows(projects_dir):
    made = make("house.dxf")
    (projects_dir / made["project_id"] / "output" / "model.glb").write_bytes(b"glTF")
    (entry,) = project_api.list_projects()["projects"]
    assert entry["dxf"]["filename"] == "house.dxf"
    assert entry["stage"] == "dxf_uploaded"
    assert entry["has_model"] is True
    assert entry["created_at"] and entry["updated_at"]


def test_a_project_with_no_model_says_so(projects_dir):
    make()
    (entry,) = project_api.list_projects()["projects"]
    assert entry["has_model"] is False


def test_an_unreadable_manifest_is_reported_not_silently_dropped(projects_dir):
    good = make()
    broken = make()
    (projects_dir / broken["project_id"] / "manifest.json").write_text("{not json", encoding="utf-8")
    result = project_api.list_projects()
    assert [p["project_id"] for p in result["projects"]] == [good["project_id"]]
    assert [u["project_id"] for u in result["unreadable"]] == [broken["project_id"]]


def test_folders_that_are_not_projects_are_ignored(projects_dir):
    make()
    (projects_dir / "notes").mkdir()                       # no manifest
    (projects_dir / "stray.txt").write_text("x", encoding="utf-8")
    (projects_dir / "bad id!").mkdir()                     # not a valid id
    (projects_dir / "bad id!" / "manifest.json").write_text("{}", encoding="utf-8")
    result = project_api.list_projects()
    assert len(result["projects"]) == 1
    assert result["unreadable"] == []


def test_a_manifest_copied_into_the_wrong_folder_is_not_trusted(projects_dir):
    made = make()
    impostor = projects_dir / "abcdef123456"
    impostor.mkdir()
    (impostor / "manifest.json").write_text(
        json.dumps({"project_id": made["project_id"], "stage": "created"}), encoding="utf-8")
    result = project_api.list_projects()
    assert [p["project_id"] for p in result["projects"]] == [made["project_id"]]
    assert [u["project_id"] for u in result["unreadable"]] == ["abcdef123456"]


def test_newest_first(projects_dir):
    old = make("old.dxf")
    new = make("new.dxf")
    for pid, stamp in ((old["project_id"], "2026-01-01T00:00:00"),
                       (new["project_id"], "2026-09-01T00:00:00")):
        path = projects_dir / pid / "manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["created_at"] = stamp
        path.write_text(json.dumps(data), encoding="utf-8")
    ids = [p["project_id"] for p in project_api.list_projects()["projects"]]
    assert ids == [new["project_id"], old["project_id"]]


def test_an_empty_or_missing_projects_directory_is_an_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(project_api, "PROJECTS_DIR", str(tmp_path / "does-not-exist"))
    assert project_api.list_projects() == {"projects": [], "unreadable": []}


def test_the_http_route_serves_the_index(projects_dir, monkeypatch):
    from fastapi.testclient import TestClient

    import server

    # server.py imports ``project_api`` from the modules directory, a separate
    # module object from ``modules.project_api``; point that one at the fixture.
    monkeypatch.setattr(server.project_api, "PROJECTS_DIR", str(projects_dir))
    made = make("house.dxf", api=server.project_api)
    response = TestClient(server.app).get("/api/projects")
    assert response.status_code == 200
    body = response.json()
    assert [p["project_id"] for p in body["projects"]] == [made["project_id"]]
    # And the per-project route is still reachable, not shadowed by the list.
    one = TestClient(server.app).get(f"/api/projects/{made['project_id']}")
    assert one.status_code == 200 and one.json()["project_id"] == made["project_id"]
