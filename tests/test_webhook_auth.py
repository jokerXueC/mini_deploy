from __future__ import annotations

import hashlib
import hmac
import io
import json
from dataclasses import replace

import pytest

import agent


WEBHOOK_SECRET = "test-webhook-secret-0123456789abcdef"
BODY = b'{"ref":"refs/heads/main"}'


@pytest.fixture
def project() -> agent.DeployProject:
    return agent._project_from_config(
        {
            "key": "api",
            "repo": "git@github.com:example/api.git",
            "webhook_secret": WEBHOOK_SECRET,
        },
        "api",
    )


@pytest.mark.parametrize(
    "header_name",
    ["X-Gitee-Token", "X-Gitlab-Token", "X-Webhook-Token", "X-Hook-Token"],
)
def test_project_webhook_accepts_supported_token_headers(project: agent.DeployProject, header_name: str) -> None:
    assert agent._valid_signature_for_project(project, {header_name: WEBHOOK_SECRET}, BODY, {})


def test_project_webhook_accepts_valid_github_hmac(project: agent.DeployProject) -> None:
    digest = hmac.new(WEBHOOK_SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={digest}"}

    assert agent._valid_signature_for_project(project, headers, BODY, {})
    assert not agent._valid_signature_for_project(project, headers, BODY + b" ", {})


@pytest.mark.parametrize("query_key", ["token", "secret"])
def test_query_token_is_rejected_by_default(
    monkeypatch: pytest.MonkeyPatch,
    project: agent.DeployProject,
    query_key: str,
) -> None:
    monkeypatch.setattr(agent, "ALLOW_QUERY_WEBHOOK_TOKEN", False)

    assert not agent._valid_signature_for_project(project, {}, BODY, {query_key: [WEBHOOK_SECRET]})


@pytest.mark.parametrize("query_key", ["token", "secret"])
def test_query_token_can_be_enabled_for_legacy_integrations(
    monkeypatch: pytest.MonkeyPatch,
    project: agent.DeployProject,
    query_key: str,
) -> None:
    monkeypatch.setattr(agent, "ALLOW_QUERY_WEBHOOK_TOKEN", True)

    assert agent._valid_signature_for_project(project, {}, BODY, {query_key: [WEBHOOK_SECRET]})


def test_project_webhook_rejects_missing_or_incorrect_secret(project: agent.DeployProject) -> None:
    assert not agent._valid_signature_for_project(project, {}, BODY, {})
    assert not agent._valid_signature_for_project(project, {"X-Gitee-Token": "wrong"}, BODY, {})

    project_without_secret = replace(project, webhook_secret="")
    assert not agent._valid_signature_for_project(project_without_secret, {}, BODY, {})


@pytest.mark.parametrize("weak_secret", ["x" * 31, "replace-with-a-long-random-token"])
def test_project_webhook_rejects_an_exact_match_for_a_weak_secret(
    project: agent.DeployProject,
    weak_secret: str,
) -> None:
    weak_project = replace(project, webhook_secret=weak_secret)

    assert not agent._valid_signature_for_project(
        weak_project,
        {"X-Gitee-Token": weak_secret},
        BODY,
        {},
    )


def test_global_webhook_auth_obeys_query_token_compatibility_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setattr(agent, "ALLOW_QUERY_WEBHOOK_TOKEN", False)
    assert not agent._valid_signature({}, BODY, {"token": [WEBHOOK_SECRET]})

    monkeypatch.setattr(agent, "ALLOW_QUERY_WEBHOOK_TOKEN", True)
    assert agent._valid_signature({}, BODY, {"token": [WEBHOOK_SECRET]})


@pytest.mark.parametrize("query_key", ["token", "secret", "signature", "access_token", "password", "key"])
def test_http_log_redacts_sensitive_query_values(query_key: str) -> None:
    message = f'"POST /webhook?project=api&{query_key}=do-not-log&next=ok HTTP/1.1" 202 -'

    redacted = agent._redact_http_log_message(message)

    assert "do-not-log" not in redacted
    assert f"{query_key}=[redacted]" in redacted
    assert "project=api" in redacted
    assert "next=ok" in redacted


@pytest.mark.parametrize("query_key", ["%74oken", "access%5Ftoken", "webhook-token"])
def test_http_log_redacts_encoded_and_aliased_sensitive_query_names(query_key: str) -> None:
    message = f'"POST /webhook?{query_key}=do-not-log HTTP/1.1" 403 -'

    redacted = agent._redact_http_log_message(message)

    assert "do-not-log" not in redacted
    assert f"{query_key}=[redacted]" in redacted


def test_audit_event_redacts_paths_nested_urls_and_named_secret_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    audit_log = tmp_path / "audit.jsonl"
    monkeypatch.setattr(agent, "AUDIT_LOG_FILE", audit_log)

    agent._audit_event(
        "csrf_rejected",
        target="/action?project=api&%74oken=path-secret",
        detail={
            "secret": "named-secret",
            "nested": {"url": "https://example.test/hook?access_token=url-secret"},
        },
        success=False,
    )

    raw = audit_log.read_text(encoding="utf-8")
    event = json.loads(raw)
    assert "path-secret" not in raw
    assert "named-secret" not in raw
    assert "url-secret" not in raw
    assert event["target"].endswith("%74oken=[redacted]")
    assert event["detail"]["secret"] == "[redacted]"
    assert event["detail"]["nested"]["url"].endswith("access_token=[redacted]")


def test_unknown_project_and_invalid_secret_have_same_forbidden_response(
    monkeypatch: pytest.MonkeyPatch,
    project: agent.DeployProject,
) -> None:
    monkeypatch.setattr(agent, "PROJECTS", {project.key: project})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", project.key)
    monkeypatch.setattr(agent, "_log", lambda _: None)

    def request(path: str) -> tuple[int, dict]:
        responses: list[tuple[int, dict]] = []
        handler = object.__new__(agent.Handler)
        handler.headers = {"Content-Length": str(len(BODY)), "X-Gitee-Token": "wrong-secret-value-that-is-long-enough"}
        handler.rfile = io.BytesIO(BODY)
        handler.client_address = ("198.51.100.9", 1234)
        handler._write_json = lambda status, payload: responses.append((status, payload))
        agent.Handler._handle_webhook(handler, agent.urlparse(path))
        return responses[-1]

    unknown = request("/webhook?project=missing")
    invalid = request("/webhook?project=api")

    assert unknown == (403, {"error": "forbidden"})
    assert invalid == unknown


def test_disabled_project_state_is_only_returned_after_valid_authentication(
    monkeypatch: pytest.MonkeyPatch,
    project: agent.DeployProject,
) -> None:
    disabled = replace(project, enabled=False)
    monkeypatch.setattr(agent, "PROJECTS", {disabled.key: disabled})
    monkeypatch.setattr(agent, "DEFAULT_PROJECT_KEY", disabled.key)
    monkeypatch.setattr(agent, "_log", lambda _: None)
    monkeypatch.setattr(agent, "_update_state", lambda **_: None)
    responses: list[tuple[int, dict]] = []
    handler = object.__new__(agent.Handler)
    handler.headers = {"Content-Length": str(len(BODY)), "X-Gitee-Token": WEBHOOK_SECRET}
    handler.rfile = io.BytesIO(BODY)
    handler.client_address = ("198.51.100.9", 1234)
    handler._write_json = lambda status, payload: responses.append((status, payload))

    agent.Handler._handle_webhook(handler, agent.urlparse("/webhook?project=api"))

    assert responses[-1] == (
        202,
        {"status": "ignored", "project": "api", "reason": "project_disabled"},
    )
