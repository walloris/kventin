from pathlib import Path

from agent.llm import local_openai_client
from agent.tasks import task_testing


def test_repo_root_points_to_project_root() -> None:
    expected = Path(__file__).resolve().parents[1]

    assert task_testing._repo_root() == expected
    assert (task_testing._repo_root() / "skills").is_dir()


def test_missing_xml_validator_fails_closed(tmp_path, monkeypatch) -> None:
    generated = tmp_path / "generated.xml"
    example = tmp_path / "example.xml"
    generated.write_text("<root/>")
    example.write_text("<root/>")
    monkeypatch.setattr(
        task_testing,
        "_test_case_writer_paths",
        lambda: (example, tmp_path / "missing-validator.py"),
    )

    ok, message = task_testing._validate_xml(generated, example)

    assert ok is False
    assert "не найден" in message


def test_generated_xml_is_sanitized_and_published_atomically(tmp_path, monkeypatch) -> None:
    example = tmp_path / "example.xml"
    validator = tmp_path / "validate_xml.py"
    example.write_text("<testcases/>")
    validator.write_text("# test stub")

    class FakeClient:
        def query(self, *_args, **_kwargs):
            return "<testcases><testcase/></testcases>"

    monkeypatch.setattr(task_testing, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(task_testing, "DOC_AS_CODE_DIR", "docs")
    monkeypatch.setattr(task_testing, "_test_case_writer_paths", lambda: (example, validator))
    monkeypatch.setattr(task_testing, "_validate_xml", lambda *_args: (True, "VALID"))
    monkeypatch.setattr(local_openai_client, "LocalOpenAIClient", FakeClient)

    output, status = task_testing.generate_test_cases_xml("../QA 1", "Summary", "Description")

    assert status == "VALID"
    assert output == tmp_path / "docs" / "test-cases" / "QA_1.xml"
    assert output.read_text() == "<testcases><testcase/></testcases>"
    assert task_testing._source_digest_path(output).read_text() == task_testing._task_input_digest(
        "Summary", "Description"
    )
    assert output.with_suffix(".xml.tmp").exists() is False


def test_process_task_reuses_only_matching_cached_requirements(tmp_path, monkeypatch) -> None:
    example = tmp_path / "example.xml"
    example.write_text("<testcases/>")
    output = tmp_path / "docs" / "test-cases" / "QA-1.xml"
    output.parent.mkdir(parents=True)
    output.write_text("<testcases/>")
    task_testing._source_digest_path(output).write_text(
        task_testing._task_input_digest("Summary", "Description")
    )
    calls = []

    monkeypatch.setattr(task_testing, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(task_testing, "DOC_AS_CODE_DIR", "docs")
    monkeypatch.setattr(task_testing, "_test_case_writer_paths", lambda: (example, tmp_path / "validator"))
    monkeypatch.setattr(task_testing, "_validate_xml", lambda *_args: (True, "VALID"))
    monkeypatch.setattr(
        task_testing,
        "get_issue_with_changelog",
        lambda _key: (
            200,
            {"fields": {"summary": "Summary", "description": "Description", "comment": {"comments": []}}},
            "",
        ),
    )
    monkeypatch.setattr(task_testing, "extract_description_text", lambda _fields: "Description")
    monkeypatch.setattr(task_testing, "_run_exploratory", lambda _url: calls.append("explore") or "")
    monkeypatch.setattr(
        task_testing,
        "generate_test_cases_xml",
        lambda *_args: (calls.append("generate") or output, "VALID"),
    )
    monkeypatch.setattr(task_testing, "attach_file_to_issue", lambda *_args: calls.append("attach") or True)
    monkeypatch.setattr(task_testing, "add_issue_comment", lambda *_args: calls.append("comment") or True)

    assert task_testing.process_task_issue("QA-1") is True
    assert calls == ["attach", "comment"]


def test_process_task_regenerates_cache_for_changed_requirements(tmp_path, monkeypatch) -> None:
    example = tmp_path / "example.xml"
    example.write_text("<testcases/>")
    output = tmp_path / "docs" / "test-cases" / "QA-1.xml"
    output.parent.mkdir(parents=True)
    output.write_text("<testcases/>")
    task_testing._source_digest_path(output).write_text(
        task_testing._task_input_digest("Summary", "Old description")
    )
    calls = []

    monkeypatch.setattr(task_testing, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(task_testing, "DOC_AS_CODE_DIR", "docs")
    monkeypatch.setattr(task_testing, "_test_case_writer_paths", lambda: (example, tmp_path / "validator"))
    monkeypatch.setattr(task_testing, "_validate_xml", lambda *_args: (True, "VALID"))
    monkeypatch.setattr(
        task_testing,
        "get_issue_with_changelog",
        lambda _key: (
            200,
            {"fields": {"summary": "Summary", "description": "New description", "comment": {"comments": []}}},
            "",
        ),
    )
    monkeypatch.setattr(task_testing, "extract_description_text", lambda _fields: "New description")
    monkeypatch.setattr(task_testing, "_run_exploratory", lambda _url: calls.append("explore") or "evidence")
    monkeypatch.setattr(
        task_testing,
        "generate_test_cases_xml",
        lambda *_args: (calls.append("generate") or output, "VALID"),
    )
    monkeypatch.setattr(task_testing, "attach_file_to_issue", lambda *_args: calls.append("attach") or True)
    monkeypatch.setattr(task_testing, "add_issue_comment", lambda *_args: calls.append("comment") or True)

    assert task_testing.process_task_issue("QA-1") is True
    assert calls == ["explore", "generate", "attach", "comment"]


def test_process_task_requires_attachment_before_success_comment(monkeypatch) -> None:
    output = Path("/tmp/QA-1.xml")
    monkeypatch.setattr(
        task_testing,
        "get_issue_with_changelog",
        lambda _key: (200, {"fields": {"summary": "S", "description": "D"}}, ""),
    )
    monkeypatch.setattr(task_testing, "extract_description_text", lambda _fields: "D")
    monkeypatch.setattr(task_testing, "_test_case_writer_paths", lambda: (Path("/missing"), Path("/missing")))
    monkeypatch.setattr(task_testing, "_run_exploratory", lambda _url: "")
    monkeypatch.setattr(task_testing, "generate_test_cases_xml", lambda *_args: (output, "VALID"))
    monkeypatch.setattr(task_testing, "attach_file_to_issue", lambda *_args: False)
    comments = []
    monkeypatch.setattr(task_testing, "add_issue_comment", lambda *_args: comments.append(_args) or True)

    assert task_testing.process_task_issue("QA-1") is False
    assert comments == []


def test_task_exploratory_uses_its_own_bounded_agent_mode(monkeypatch) -> None:
    from agent.core import agent as core_agent

    calls = []
    monkeypatch.setattr(task_testing, "JIRA_TASK_EXPLORATORY_STEPS", 7)
    monkeypatch.setattr(
        core_agent,
        "run_agent",
        lambda **kwargs: calls.append(kwargs) or {"steps": 7, "defects": 1},
    )

    evidence = task_testing._run_exploratory("https://example.test/feature")

    assert calls == [
        {
            "start_url": "https://example.test/feature",
            "max_steps": 7,
            "enable_qa_retests": False,
        }
    ]
    assert "Шагов: 7" in evidence
    assert "дефектов заведено: 1" in evidence
