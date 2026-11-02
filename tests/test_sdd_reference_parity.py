"""Differential tests run the original JavaScript gate against the native Python gate.

Node is a test-only oracle. Both transports are offline; no credentials, corporate
services, wall-clock sleeps, or Node runtime dependency are introduced in the CLI.
"""
import hashlib
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from scripts import release_checker as rc

FIXTURES = Path(__file__).parent / 'fixtures'
REFERENCE = FIXTURES / 'release-gate-sdd-reference.mjs'
REFERENCE_SHA256 = '5186150ba09b1651e8cee81ecdb6860c66a975cf9335893980717d5e67833202'
DATASET = FIXTURES / 'sdd_parity_cases.json'
ORACLE = Path(__file__).parent / 'helpers' / 'sdd_reference_oracle.mjs'
DEFAULTS = {'key': 'ST-1', 'labels': ['SDD'], 'issue_id': '42', 'repo_count': 1}


def _dataset():
    data = json.loads(DATASET.read_text())
    cases = data if isinstance(data, list) else data['cases']
    fuzz = json.loads((FIXTURES / 'sdd_fuzz_paths.json').read_text())
    return cases + fuzz['cases']


def _response(status, payload):
    # Deep copies prevent either implementation mutating the other one's fixtures.
    return SimpleNamespace(status_code=status, headers={}, json=lambda: json.loads(json.dumps(payload)))


def _python_result(original, monkeypatch):
    case = {**DEFAULTS, **original}
    trace, sleeps, stories, findings = [], [], [], []
    detail_attempt = 0
    session = Mock()
    session.headers = {}

    def get(url, **kwargs):
        nonlocal detail_attempt
        path = urlsplit(url).path
        params = kwargs.get('params', {})
        normalized = {name: str(value) for name, value in params.items() if name != '_'}
        trace.append({'path': path, 'params': normalized})
        if path.endswith('/search'):
            return _response(case.get('search_status', 200), case.get('search_payload', {
                'issues': case.get('issues', [{'key': case['key']}])}))
        if path.endswith('/issue/summary'):
            return _response(case.get('summary_status', 200), case.get('summary_payload', {
                'summary': {'repository': {'overall': {'count': case['repo_count']}}}}))
        if path.endswith('/issue/detail'):
            sequence = case.get('detail_responses')
            item = sequence[min(detail_attempt, len(sequence) - 1)] if sequence else None
            detail_attempt += 1
            return _response(item.get('status', 200), item.get('payload')) if item else _response(
                200, case.get('detail', {'detail': []}))
        if '/rest/api/2/issue/' in path and path.rsplit('/', 1)[-1]:
            if params.get('fields') == 'labels':
                return _response(case.get('issue_status', 200), case.get('labels_payload', {
                    'fields': {'labels': case['labels']}}))
            return _response(case.get('resolved_status', 200), case.get('resolved_payload', {'id': case['issue_id']}))
        raise AssertionError(f'Unexpected offline fixture request: {path}')

    class FixtureSession:
        def __enter__(self):
            return session

        def __exit__(self, *_):
            return False

    session.get.side_effect = get
    checker = rc.ReleaseValidator.__new__(rc.ReleaseValidator)
    checker._log_issue = lambda issue, severity, message: findings.append((severity, message))
    original_story_result = rc.sdd_story_result

    def story_result(*args, **kwargs):
        has_sdd, openspec = original_story_result(*args, **kwargs)
        stories.append({'key': args[2], 'hasSddLabel': has_sdd, 'hasOpenspec': openspec})
        return has_sdd, openspec

    with monkeypatch.context() as patch:
        patch.setattr(rc.requests, 'Session', FixtureSession)
        patch.setattr(rc.time, 'sleep', lambda seconds: sleeps.append(seconds * 1000))
        patch.setattr(rc, 'sdd_story_result', story_result)
        patch.setattr(rc, 'config', {'jira': {'url': 'https://jira.example', 'token': 'fixture-token'}})
        patch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '0')
        try:
            with redirect_stdout(StringIO()):
                checker._check_sdd_gate('REL-1')
            errors = [message for severity, message in findings if severity == 'error']
            exit_code = 2 if any('SDD: проверка не выполнена' in message for message in errors) else int(bool(errors))
        except Exception:
            # The executable JS oracle exits 2 on malformed DTOs/fatal errors.
            exit_code = 2
    return {'id': case['id'], 'exit_code': exit_code,
            'stories': [] if exit_code == 2 else stories, 'trace': trace, 'sleeps': sleeps}


@pytest.fixture(scope='module')
def oracle_results():
    node = shutil.which('node')
    assert node, 'Node.js is required to execute the JavaScript reference parity tests.'
    process = subprocess.run([node, str(ORACLE)], input=json.dumps(_dataset(), ensure_ascii=True), text=True,
                             capture_output=True, timeout=60, check=True)
    return {result['id']: result for result in json.loads(process.stdout)}


def test_reference_is_original_and_unchanged():
    assert hashlib.sha256(REFERENCE.read_bytes()).hexdigest() == REFERENCE_SHA256


@pytest.mark.parametrize('case', _dataset(), ids=lambda case: case['id'])
def test_native_sdd_matches_executable_reference(case, oracle_results, monkeypatch):
    actual = _python_result(case, monkeypatch)
    expected = oracle_results[case['id']]
    assert actual['exit_code'] == expected['exit_code']
    assert actual['stories'] == expected['stories']
    assert actual['trace'] == expected['trace']
    assert actual['sleeps'] == expected['sleeps']
