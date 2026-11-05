import json
from datetime import datetime, timedelta, timezone

from ehv_agent.store import TaskStore


def test_external_queue_scheduling_claim_and_retry(setup):
    _, home, _, _, _, _ = setup
    store = TaskStore(home)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    store.submit("future", not_before=future)
    (store.folder("coding", "pending") / "external.json").write_text(
        json.dumps({"prompt": "fix it"})
    )
    task = store.next_pending()
    assert task["id"] == "external" and task["role"] == "coding"
    store.claim(task)
    assert store.next_pending() is None
    store.finish(task, "failed", "failure")
    retried = store.retry("external")
    assert retried["retry_of"] == "external"
    assert retried["prompt"] == "fix it"


def test_restart_preserves_pending_root_and_marks_children_interrupted(setup):
    _, home, _, _, _, _ = setup
    store = TaskStore(home)
    parent = store.submit("working")
    store.claim(parent)
    child = store.submit("child", "coding", parent_id=parent["id"])
    pending = store.submit("waiting")
    assert store.recover() == 2
    assert store.next_pending()["id"] == pending["id"]
    for task in (parent, child):
        saved = json.loads(
            (store.folder(task["role"], "results") / f"{task['id']}.json").read_text()
        )
        assert saved["status"] == "interrupted"


def test_invalid_json_is_quarantined_without_blocking_valid_tasks(setup):
    _, home, _, _, _, _ = setup
    store = TaskStore(home)
    (store.folder("father", "pending") / "broken.json").write_text("{")
    task = store.submit("valid")
    assert store.next_pending()["id"] == task["id"]
    assert (
        json.loads((store.folder("father", "results") / "broken.json").read_text())["status"]
        == "invalid"
    )


def test_malformed_timestamps_and_interrupted_files_do_not_block_queue(setup):
    _, home, _, _, _, _ = setup
    store = TaskStore(home)
    for filename, value in [
        ("bad-created", {"created_at": 1}),
        ("bad-timezone", {"not_before": "2026-11-05T09:00:00"}),
    ]:
        (store.folder("father", "pending") / f"{filename}.json").write_text(
            json.dumps({"prompt": "bad", **value})
        )
    (store.folder("coding", "running") / "broken.json").write_text("{")
    task = store.submit("valid")
    store.recover()
    assert store.next_pending()["id"] == task["id"]
    assert len(list((home / "agents").glob("*/results/*.json"))) == 3
