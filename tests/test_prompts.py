import pytest

from subbench.agents.prompts import render_prompt


def test_render_prompt_determinism_and_hash():
    p1 = render_prompt(
        "minimal-v1",
        instance_id="pytest-dev__pytest-5809",
        repo="pytest-dev/pytest",
        workspace="/testbed",
        problem_statement="Fix bug in pastebin",
    )
    p2 = render_prompt(
        "minimal-v1",
        instance_id="pytest-dev__pytest-5809",
        repo="pytest-dev/pytest",
        workspace="/testbed",
        problem_statement="Fix bug in pastebin",
    )
    assert p1.text == p2.text
    assert p1.sha256_hash == p2.sha256_hash
    assert len(p1.sha256_hash) == 64
    assert p1.template_id == "minimal-v1"
    assert "pytest-dev/pytest" in p1.text
    assert "/testbed" in p1.text
    assert "Fix bug in pastebin" in p1.text


def test_render_prompt_unknown_template():
    with pytest.raises(ValueError, match="Unknown prompt template"):
        render_prompt(
            "bogus-template",
            instance_id="x",
            repo="r",
            workspace="/w",
            problem_statement="p",
        )
