"""Versioned prompt templates for SWE-bench task attempts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    template_id: str
    sha256_hash: str


TEMPLATES: dict[str, str] = {
    "minimal-v1": (
        "You are tasked with resolving an issue in the repository {repo}.\n"
        "The workspace is located at {workspace}.\n"
        "The project environment and its dependencies (Python, test runner, etc.)\n"
        "are already installed and active on PATH. You can run tests directly\n"
        "(e.g. `pytest`, `python -m unittest`, etc.).\n\n"
        "An issue has been reported with the following problem statement:\n\n"
        "{problem_statement}\n\n"
        "Instructions:\n"
        "1. Investigate the codebase and reproduce the issue.\n"
        "2. Implement the minimal fix required to resolve the issue.\n"
        "3. Run existing and new tests to verify your fix and ensure no regressions.\n"
        "4. Work autonomously without asking questions or pausing for confirmation.\n"
        "5. Leave your changes cleanly in {workspace}. Do NOT push commits, modify remotes,\n"
        "   or search the web for upstream pull requests or discussions.\n"
    ),
}


def render_prompt(
    template_id: str,
    *,
    instance_id: str,
    repo: str,
    workspace: str = "/testbed",
    problem_statement: str,
) -> RenderedPrompt:
    """Render a prompt template with deterministic sha256 hash."""
    if template_id not in TEMPLATES:
        raise ValueError(
            f"Unknown prompt template '{template_id}'. Available: {list(TEMPLATES.keys())}"
        )
    raw_template = TEMPLATES[template_id]
    text = raw_template.format(
        instance_id=instance_id,
        repo=repo,
        workspace=workspace,
        problem_statement=problem_statement.strip(),
    )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RenderedPrompt(text=text, template_id=template_id, sha256_hash=digest)
