from browser_workflows import (
    DLD_PROJECT_SEARCH_URL,
    run_dld_project_status,
)


def _executor_factory(states):
    calls = []

    def execute(name, args):
        calls.append((name, args))
        if name == "browser_open":
            return {"status": "ok", "url": args["url"]}
        if name == "browser_state":
            return states.pop(0)
        if name in {"browser_fill", "browser_click"}:
            return {"status": "ok"}
        raise AssertionError(name)

    return execute, calls


SEARCH_FORM_STATE = {
    "status": "ok",
    "elements": [
        {"text": "Project name", "kind": "textbox", "index": 3},
        {"text": "Search", "kind": "button", "index": 4},
    ],
}


def test_dld_project_workflow_uses_search_form_and_visible_steps():
    results_state = {
        "status": "ok",
        "title": "Search results",
        "raw_output": "1 result found",
        "elements": [
            {"text": "Marina Sail project details", "kind": "link", "index": 7},
        ],
    }
    detail_state = {
        "status": "ok",
        "title": "Marina Sail",
        "raw_output": "Construction status: Under construction",
    }
    execute, calls = _executor_factory([SEARCH_FORM_STATE, results_state, detail_state])

    result = run_dld_project_status(execute, "session-1", "Marina Sail")

    assert result.status == "complete"
    assert [step.status for step in result.steps] == ["ok"] * 5
    assert result.source_url.startswith(DLD_PROJECT_SEARCH_URL)
    assert "DLD record" in result.content

    opened = calls[0]
    assert opened[0] == "browser_open"
    assert "dubailand.gov.ae" in opened[1]["url"]
    fills = [call for call in calls if call[0] == "browser_fill"]
    assert fills and fills[0][1]["text"] == "Marina Sail"


def test_dld_project_workflow_surfaces_human_verification_steps():
    captcha_state = {
        "status": "ok",
        "title": "Verify",
        "raw_output": "Please enter the captcha to continue",
    }
    execute, calls = _executor_factory([SEARCH_FORM_STATE, captcha_state])

    result = run_dld_project_status(execute, "session-1", "Marina Sail")

    assert result.status == "needs_input"
    assert result.steps[3].status == "needs_input"
    assert "verification" in result.content.lower()
    # The workflow stops before clicking anything on the verification wall.
    assert all(call[0] != "browser_click" or call[1].get("step_index", 0) <= 3 for call in calls)


def test_dld_project_workflow_requires_a_project_name():
    execute, calls = _executor_factory([])

    result = run_dld_project_status(execute, "session-1", "")

    assert calls == []
    assert result.steps[0].status == "skipped"
    assert "project name" in result.content.lower()
