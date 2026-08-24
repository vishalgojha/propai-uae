"""Deterministic workflows for Maharashtra public property portals.

These workflows deliberately use a small, explicit sequence of browser actions.
They are not a general-purpose autonomous browser agent: if a portal changes its
form or asks for a human-only step, the result is a clear hand-off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import quote as _url_quote
from typing import Any, Callable


ExecuteBrowserTool = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass
class WorkflowStep:
    label: str
    status: str = "pending"
    detail: str = ""


@dataclass
class BrowserWorkflowResult:
    title: str
    content: str
    steps: list[WorkflowStep] = field(default_factory=list)
    source_url: str = ""
    status: str = "needs_input"
    data: dict[str, str] = field(default_factory=dict)

    def activity(self, browser_session_id: str, route: str) -> dict[str, Any]:
        return {
            "type": "activity",
            "title": self.title,
            "body": self.content,
            "steps": [
                f"{step.label}: {step.detail}" if step.detail else step.label
                for step in self.steps
                if step.status != "pending"
            ],
            "trace": {
                "route": route,
                "browser_provider": "agent-browser",
                "browser_session_id": browser_session_id,
                "source_url": self.source_url,
                "workflow_status": self.status,
            },
        }


DLD_PORTAL_URL = "https://dubailand.gov.ae/en/"
DLD_PROJECT_SEARCH_URL = "https://dubailand.gov.ae/en/search/?q="
DLD_TRANSACTION_SEARCH_URL = "https://dubailand.gov.ae/en/"


def _text(result: dict[str, Any]) -> str:
    return " ".join(
        str(value or "")
        for value in (result.get("title"), result.get("summary"), result.get("raw_output"))
    ).strip()


def _elements(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (result.get("elements") or []) if isinstance(item, dict)]


def _find(elements: list[dict[str, Any]], patterns: tuple[str, ...], kinds: tuple[str, ...] = ()) -> dict[str, Any] | None:
    for element in elements:
        label = str(element.get("text") or element.get("raw") or "").lower()
        kind = str(element.get("kind") or "").lower()
        if kinds and kind not in kinds:
            continue
        if any(re.search(pattern, label, re.IGNORECASE) for pattern in patterns):
            return element
    return None


def _run(execute: ExecuteBrowserTool, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return execute(name, args)


def _open_and_snapshot(execute: ExecuteBrowserTool, url: str, session_id: str, step_index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    opened = _run(execute, "browser_open", {"url": url, "browser_session_id": session_id, "step_index": step_index, "session_label": "Portal workflow"})
    if opened.get("status") != "ok":
        return opened, opened
    state = _run(execute, "browser_state", {"browser_session_id": session_id, "step_index": step_index + 1})
    return opened, state


def run_dld_project_status(
    execute: ExecuteBrowserTool,
    session_id: str,
    project_name: str,
) -> BrowserWorkflowResult:
    """Search the DLD portal for a named project and read its public record."""
    name = re.sub(r"\s+", " ", project_name).strip()
    result = BrowserWorkflowResult(
        title="DLD project check",
        content="",
        source_url=DLD_PORTAL_URL,
        steps=[WorkflowStep("Open official DLD portal"), WorkflowStep("Find project search"), WorkflowStep("Search project"), WorkflowStep("Open matching project"), WorkflowStep("Read construction status")],
    )
    if not name:
        result.content = "Tell me the DLD project name or permit number first."
        result.steps[0].status = "skipped"
        result.steps[0].detail = "Project name missing"
        return result

    # The project-search page is a known first-party route. Opening it directly
    # avoids the site's three similarly-labelled search forms.
    result.source_url = DLD_PROJECT_SEARCH_URL
    opened, state = _open_and_snapshot(execute, DLD_PROJECT_SEARCH_URL + _url_quote(name), session_id, 0)
    if state.get("status") != "ok":
        result.steps[0].status = "failed"
        result.steps[0].detail = str(state.get("error") or opened.get("error") or "The official site could not be opened")
        result.content = "I could not open the official DLD site right now."
        result.status = "failed"
        return result
    result.steps[0].status = "ok"
    result.steps[0].detail = "Official site opened"
    elements = _elements(state)
    search_input = _find(elements, (r"project.*name", r"project name", r"certificate", r"registration"), ("textbox", "input", "combobox"))
    if not search_input:
        result.steps[1].status = "failed"
        result.steps[1].detail = "The project search form was not visible"
        result.content = "The DLD portal opened, but its project search form has changed or is not available."
        result.status = "needs_input"
        return result
    result.steps[1].status = "ok"
    result.steps[1].detail = "Project search field found"
    filled = _run(execute, "browser_fill", {"browser_session_id": session_id, "index": search_input.get("index"), "text": name, "step_index": 2})
    search_button = _find(elements, (r"^search$", r"submit", r"find"), ("button", "link"))
    if filled.get("status") != "ok" or not search_button:
        result.steps[2].status = "failed"
        result.steps[2].detail = "Could not complete the project search form"
        result.content = "I reached the DLD portal but could not submit the project search."
        result.status = "failed"
        return result
    clicked = _run(execute, "browser_click", {"browser_session_id": session_id, "index": search_button.get("index"), "step_index": 3})
    result.steps[2].status = "ok" if clicked.get("status") == "ok" else "failed"
    result.steps[2].detail = "Project search submitted" if clicked.get("status") == "ok" else str(clicked.get("error") or "Search could not be submitted")
    if clicked.get("status") != "ok":
        result.content = "The DLD portal could not submit the project search."
        result.status = "failed"
        return result
    final_state = _run(execute, "browser_state", {"browser_session_id": session_id, "step_index": 4})
    page_text = _text(final_state)
    if final_state.get("status") != "ok":
        result.steps[3].status = "failed"
        result.steps[3].detail = str(final_state.get("error") or "Search results could not be read")
        result.content = "The DLD search ran, but I could not read the result."
        result.status = "failed"
        return result
    if re.search(r"captcha|enter.*captcha|login|sign in", page_text, re.IGNORECASE):
        result.steps[3].status = "needs_input"
        result.steps[3].detail = "The portal requires a human verification or login"
        result.content = "The DLD portal is asking for a verification/login step. Please complete it in the browser, then ask me to continue."
        return result
    project_link = _find(_elements(final_state), (re.escape(name.lower()), r"project details", r"view details", r"certificate"), ("link", "button"))
    if not project_link:
        result.steps[3].status = "needs_input"
        result.steps[3].detail = "Search results loaded, but no matching project record link was visible"
        result.content = f"DLD returned results for “{name}”, but I could not safely open the matching project record."
        result.data["page_text"] = page_text[:4000]
        return result
    result.steps[3].status = "ok"
    result.steps[3].detail = "Matching project record opened"
    detail = _run(execute, "browser_click", {"browser_session_id": session_id, "index": project_link.get("index"), "step_index": 5})
    if detail.get("status") != "ok":
        result.steps[3].status = "failed"
        result.steps[3].detail = str(detail.get("error") or "Project record could not be opened")
        result.content = "The DLD portal found a result, but the project record could not be opened."
        result.status = "failed"
        return result
    detail_state = _run(execute, "browser_state", {"browser_session_id": session_id, "step_index": 6})
    detail_text = _text(detail_state)
    if detail_state.get("status") != "ok":
        result.steps[4].status = "failed"
        result.steps[4].detail = str(detail_state.get("error") or "Project details could not be read")
        result.content = "The DLD project record opened, but its details could not be read."
        result.status = "failed"
        return result
    result.steps[4].status = "ok"
    result.steps[4].detail = "Official project details read"
    result.status = "complete"
    result.content = f"I opened the official DLD record for “{name}”. The visible record has been read; review the official completion/progress fields before relying on it."
    result.data["page_text"] = detail_text[:4000]
    return result


def run_dld_transaction_search(
    execute: ExecuteBrowserTool,
    session_id: str,
    identifiers: dict[str, str] | None = None,
) -> BrowserWorkflowResult:
    """Open the DLD portal and guide the user through its transaction search."""
    identifiers = {key: str(value or "").strip() for key, value in (identifiers or {}).items()}
    result = BrowserWorkflowResult(
        title="DLD property transaction search",
        content="",
        source_url=DLD_TRANSACTION_SEARCH_URL,
        steps=[WorkflowStep("Open official DLD portal"), WorkflowStep("Check access requirements"), WorkflowStep("Run transaction/title-deed search"), WorkflowStep("Read official result")],
    )
    opened, state = _open_and_snapshot(execute, DLD_TRANSACTION_SEARCH_URL, session_id, 0)
    if state.get("status") != "ok":
        result.steps[0].status = "failed"
        result.steps[0].detail = str(state.get("error") or opened.get("error") or "The official site could not be opened")
        result.content = "I could not open the official Dubai Land Department site right now."
        result.status = "failed"
        return result
    result.steps[0].status = "ok"
    result.steps[0].detail = "Official DLD portal opened"
    page_text = _text(state)
    if re.search(r"login|user id|password|captcha|otp", page_text, re.IGNORECASE):
        result.steps[1].status = "needs_input"
        result.steps[1].detail = "DLD requires login and human verification"
        result.content = "This DLD service is protected by login/verification. Please log in via UAE Pass in the browser; then provide the title deed or permit number so I can continue."
        return result
    result.steps[1].status = "ok"
    result.steps[1].detail = "Search page available"
    required = [key for key in ("year", "title_deed_number", "permit_number", "plot_number", "building_name") if not identifiers.get(key)]
    if required == ["year"] or (not required and not identifiers.get("building_name")):
        required = []
    if required:
        result.steps[2].status = "needs_input"
        result.steps[2].detail = "Required search identifiers are missing"
        result.content = "To search DLD records, send the year plus one of: title deed number, permit number, plot number, or building name. I will then run the fixed search steps."
        return result
    result.steps[2].status = "needs_input"
    result.steps[2].detail = "Search form is protected; human login must be completed first"
    result.content = "The DLD portal is open, but I will not submit a protected search until the logged-in form is visible. Complete login/UAE Pass verification and ask me to continue."
    return result
