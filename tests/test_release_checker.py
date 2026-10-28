"""Release policy regressions; never connect to Jira or publish comments."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
from contextlib import nullcontext

import pytest
import requests

from scripts import release_checker as rc

REAL_SDD_RELEASE_ARTIFACTS = rc.sdd_release_artifacts


@pytest.fixture
def validator(monkeypatch):
    checker = rc.ReleaseValidator.__new__(rc.ReleaseValidator)
    checker.jira_main = Mock()
    checker.jira_http = Mock()
    checker._log_issue = Mock()
    checker._dev_status_payload_cache = {}
    checker._pull_request_only_evidence_cache = {}
    monkeypatch.setattr(rc.time, 'sleep', lambda _: None)
    return checker


@pytest.mark.parametrize('payload', [
    {'detail': [{'_instance': {'id': '1'}, 'pullRequests': []}]},
    {'pullrequest': {'overall': {'count': 0}, 'stateCount': 0}},
    {'pullRequest': False},
    {'repositories': [{'id': 42, 'commits': [{'id': 'abc123'}]}]},
    {'description': 'pull request #42 will be created later'},
])
def test_empty_metadata_and_commits_are_not_pr(validator, payload):
    assert validator._dev_status_pull_request_evidence('pullrequest', payload) is None


@pytest.mark.parametrize('payload', [
    {'detail': [{'pullRequests': [{'id': '42', 'status': 'OPEN'}]}]},
    {'summary': {'pullrequest': {'overall': {'count': 1}}}},
    {'summary': {'pullrequest': {'byInstanceType': {'custom': {'count': 1}}}}},
    {'customfield_42': '{"pullrequest": {"stateCount": 1}}'},
    {'customfield_42': '{pullrequest={dataType=pullrequest, state=MERGED, stateCount=1}}'},
    {'pullRequestId': '42'},
    {'description': 'See https://git.example/team/repo/pull/42'},
    {'object': {'url': 'https://git.example/team/repo/-/merge_requests/42'}},
])
def test_supported_pr_sources(validator, payload):
    assert validator._extract_pull_request_evidence(payload)


def response(status, payload=None, headers=None):
    def raise_for_status():
        if status >= 400:
            raise requests.HTTPError(f'HTTP {status}')
    return NS(status_code=status, json=lambda: payload, headers=headers or {},
              raise_for_status=raise_for_status)


def test_http_retries_timeout_throttle_and_success(validator):
    validator.jira_http.get.side_effect = [requests.Timeout(), response(200, {'ok': True})]
    assert validator._pr_get_json('https://jira.example') == {'ok': True}
    assert validator.jira_http.get.call_count == 2


def test_invalid_json_retried(validator):
    invalid = NS(status_code=200, json=Mock(side_effect=ValueError('invalid json')))
    validator.jira_http.get.side_effect = [invalid, response(200, {})]
    assert validator._pr_get_json('https://jira.example') == {}
    assert validator.jira_http.get.call_count == 2


def test_permanent_http_error_not_retried(validator):
    validator.jira_http.get.return_value = response(403)
    assert validator._pr_get_json('https://jira.example') is None
    assert validator.jira_http.get.call_count == 1
    assert 'HTTP 403' in validator._pr_lookup_failures


def test_summary_discovers_provider_and_old_api_fallback(validator):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return {'summary': {'pullrequest': {'byInstanceType': {'custom-git': {'count': 0}}}}}
        if '/1.0/' in url and kwargs['params']['applicationType'] == 'custom-git':
            return {'detail': [{'pullRequests': [{'id': 42}]}]}
        return {'detail': []}
    validator._pr_get_json = Mock(side_effect=fetch)
    payloads = validator._get_dev_status_payloads('123', data_types=('pullrequest',))
    assert any(provider == 'custom-git' and validator._extract_pull_request_evidence(payload)
               for provider, _, payload in payloads)


def test_missing_summary_still_tries_known_integrations(validator):
    validator._pr_get_json = Mock(return_value={})
    with pytest.raises(RuntimeError, match='development'):
        validator._get_dev_status_payloads('123', data_types=('pullrequest',))
    assert validator._pr_get_json.call_count == 8
    details = [c for c in validator._pr_get_json.call_args_list if c.args[0].endswith('/detail')]
    assert {c.kwargs['params']['applicationType'] for c in details} == set(rc.PR_LEGACY_PROVIDERS)
    assert all(c.kwargs['params']['dataType'] == 'pullrequest' for c in details)


def test_lookup_negative_result_not_cached(validator):
    issue = NS(key='ABC-1', id='123')
    validator._find_issue_pull_request_only_evidence_uncached = Mock(side_effect=[None, 'PR 42'])
    assert validator._find_issue_pull_request_only_evidence(issue) is None
    assert validator._find_issue_pull_request_only_evidence(issue) == 'PR 42'
    assert validator._find_issue_pull_request_only_evidence(issue) == 'PR 42'
    assert validator._find_issue_pull_request_only_evidence_uncached.call_count == 2


def test_remote_link_fallback_never_loads_fields_properties_or_details(validator):
    def fetch(url, **kwargs):
        if url.endswith('/remotelink'):
            return [{'object': {'url': 'https://git.example/repo/pull/42'}}]
        return None
    validator._pr_get_json = Mock(side_effect=fetch)
    assert 'remote links' in validator._find_issue_pull_request_only_evidence_uncached('ABC-1', '123')
    assert validator._pr_get_json.call_count == 3
    assert all(call.args[0].endswith(('/summary', '/remotelink')) for call in validator._pr_get_json.call_args_list)


def test_pr_on_subtask_does_not_count_for_story(validator):
    validator._pr_get_json = Mock(side_effect=lambda url, **kwargs:
        {'fields': {'subtasks': [{'fields': {'description': 'https://git.example/repo/pull/42'}}]}}
        if url.endswith('/ABC-1') else {})
    assert validator._find_issue_pull_request_only_evidence_uncached('ABC-1', '') is None


def test_budget_exhaustion_prevents_request(validator):
    validator._pr_request_deadline = rc.time.monotonic() - 1
    assert validator._pr_get_json('https://jira.example') is None
    validator.jira_http.get.assert_not_called()


def test_lakutin_and_comment_exceptions(validator):
    assert rc.is_allowed_tester('Лакутин Роман Денисович')
    for name in ('Метляев Игорь Андреевич', 'Никонов Александр Алексеевич'):
        assert validator._is_worklog_exempt_comment_author(name, 'HRM')
        assert not validator._is_worklog_exempt_comment_author(name, 'HRC')


def history(author, status='Closed', date='2026-10-01T10:00:00+00:00'):
    return NS(created=date, author=NS(displayName=author), items=[NS(field='status', toString=status)])


def architecture_story(validator, histories=None, status='Closed', total=None):
    child = NS(key='ABC-2', fields=NS(summary='Анализ архитектуры'))
    story = NS(key='ABC-1', fields=NS(subtasks=[child]))
    histories = histories or []
    validator.jira_main.issue.return_value = NS(
        fields=NS(status=NS(name=status)),
        changelog=NS(histories=histories, total=len(histories) if total is None else total))
    return story


@pytest.mark.parametrize('author', sorted(rc.ALLOWED_ARCHITECTURE_CLOSERS))
def test_architecture_closed_by_allowed_author(validator, author):
    story = architecture_story(validator, [history(author)])
    validator._check_story_architecture_subtask(story)
    assert validator._log_issue.call_args.args[1] == 'success'


def test_pokhotnyuk_is_an_allowed_architecture_closer(validator):
    story = architecture_story(validator, [history('Плохотнюк Анна Дмитриевна')])
    validator._check_story_architecture_subtask(story)
    assert validator._log_issue.call_args.args[1] == 'success'


@pytest.mark.parametrize('status,histories,total', [
    ('In Progress', [history('Черешнев Антон Олегович')], None),
    ('Closed', [], None),
    ('Closed', [history('Другой Автор')], None),
    ('Closed', [history('Черешнев Антон Олегович')], 100),
    ('Closed', [history('Черешнев Антон Олегович'),
                history('Другой Автор', 'Reopened', '2026-10-02T10:00:00+00:00'),
                history('Другой Автор', date='2026-10-03T10:00:00+00:00')], None),
])
def test_architecture_rejects_unverified_closure(validator, status, histories, total):
    story = architecture_story(validator, histories, status, total)
    validator._check_story_architecture_subtask(story)
    assert validator._log_issue.call_args.args[1] == 'error'


def test_missing_architecture_task(validator):
    story = NS(fields=NS(subtasks=[]))
    validator._check_story_architecture_subtask(story)
    assert 'отсутствует' in validator._log_issue.call_args.args[2]


@pytest.mark.parametrize('days,expected', [(0, 'success'), (1, 'success'), (6.9, 'success'), (6.9001, 'warning')])
def test_testing_duration_boundary_and_no_minimum(validator, days, expected):
    story = NS(key='HRC-1', fields=NS(
        description='Test', status=NS(name='In Progress'), issuelinks=[],
        customfield_24000='Да', customfield_18400='Нет', customfield_10006='HRC-10'))
    validator._get_consist_of_issues = Mock(return_value=['HRC-1'])
    validator.jira_main.search_issues.return_value = [story]
    validator.jira_main.issue.return_value = NS(changelog=NS(histories=[]))
    validator._check_story_architecture_subtask = Mock()
    validator._check_required_platform_label = Mock()
    validator._check_story_requirements_block = Mock(return_value=True)
    validator._check_story_requirements_timing = Mock()
    validator._calc_status_days = Mock(return_value=days)
    validator._check_stories('REL-1')
    calls = [call.args for call in validator._log_issue.call_args_list if 'тест-статусах' in call.args[2]]
    assert len(calls) == 1
    assert calls[0][1] == expected
    assert '115%' in calls[0][2]
    assert validator.jira_main.search_issues.call_args.kwargs['maxResults'] is False
    validator._check_story_architecture_subtask.assert_called_once_with(story)


@pytest.mark.parametrize('author', ['Метляев Игорь Андреевич', 'Никонов Александр Алексеевич'])
def test_exempt_comment_satisfies_artifacts_without_worklog(validator, author):
    artifact = NS(key='HRM-1', fields=NS(issuetype=NS(name='Bug')))
    validator._get_consist_of_issues = Mock(return_value=['HRM-1'])
    validator.jira_main.issue.return_value = artifact
    validator.jira_main.comments.return_value = [NS(author=NS(displayName=author))]
    validator.jira_main.worklogs.return_value = []
    validator._check_artifacts('REL-1')
    assert all(call.args[1] == 'success' for call in validator._log_issue.call_args_list)
    assert any(author in call.args[2] for call in validator._log_issue.call_args_list)


def test_architecture_second_valid_candidate_after_unavailable_first(validator):
    author = 'Абдуллаев Магомед Русланович'
    story = architecture_story(validator, [history(author)])
    valid = validator.jira_main.issue.return_value
    story.fields.subtasks.append(NS(key='ABC-3', fields=NS(summary='Анализ архитектуры')))
    validator.jira_main.issue.side_effect = [requests.Timeout(), valid]
    validator._check_story_architecture_subtask(story)
    assert validator._log_issue.call_args.args[1] == 'success'


def summary_payload(count, providers=None):
    return {'summary': {'pullrequest': {
        'overall': {'count': count}, 'byInstanceType': providers or {}}}}


def test_positive_summary_finishes_presence_check_in_one_request(validator):
    validator.jira_http.get.return_value = response(200, summary_payload(2))
    assert 'связанных PR — 2' in validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert validator.jira_http.get.call_count == 1
    assert validator.jira_http.get.call_args.args[0].endswith('/summary')


def test_zero_count_and_empty_remote_links_is_confirmed_absence(validator):
    validator.jira_http.get.side_effect = [response(200, summary_payload(0)), response(200, [])]
    assert validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123')) is None
    assert not validator._pr_lookup_failures
    assert validator.jira_http.get.call_count == 2


@pytest.mark.parametrize('payload', [{}, {'summary': {}}, summary_payload(False), summary_payload('0')])
def test_incomplete_or_invalid_summary_is_unknown_not_absence(validator, payload):
    validator.jira_http.get.side_effect = lambda url, **kw: response(200, [] if url.endswith('/remotelink') else payload)
    assert validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123')) is None
    assert validator._pr_lookup_failures


def test_failed_request_snapshot_reused_with_failure_reason(validator):
    validator.jira_http.get.side_effect = requests.Timeout()
    assert validator._pr_get_json('https://jira.example/summary', params={'issueId': '1'}) is None
    validator._pr_lookup_failures = set()
    assert validator._pr_get_json('https://jira.example/summary', params={'issueId': '1'}) is None
    assert validator.jira_http.get.call_count == rc.PR_MAX_ATTEMPTS
    assert validator._pr_lookup_failures


def test_empty_responses_reused_only_within_current_run(validator):
    validator.jira_http.get.return_value = response(200, summary_payload(0))
    url = 'https://jira.example/summary'
    assert validator._pr_get_json(url) == summary_payload(0)
    assert validator._pr_get_json(url) == summary_payload(0)
    assert validator.jira_http.get.call_count == 1
    validator._reset_pr_run_state()
    validator._pr_get_json(url)
    assert validator.jira_http.get.call_count == 2


def test_throttle_stops_retrying_after_recovery(validator):
    validator.jira_http.get.side_effect = [response(429), response(200, {})]
    assert validator._pr_get_json('https://jira.example') == {}
    assert validator.jira_http.get.call_count == 2


def test_api_errors_in_success_http_response_are_unknown(validator):
    validator.jira_http.get.return_value = response(200, {'errors': ['unavailable'], **summary_payload(0)})
    assert validator._pr_get_json('https://jira.example') is None
    assert validator._pr_lookup_failures


def test_many_issues_during_outage_have_bounded_requests_and_no_success(validator, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rc.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(rc.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    def timeout(url, **kwargs):
        now[0] += kwargs['timeout'].total
        raise requests.Timeout()
    validator.jira_http.get.side_effect = timeout
    with validator._pr_lookup_budget(rc.PR_RELEASE_TIMEOUT_SECONDS):
        for number in range(50):
            assert validator._find_issue_pull_request_only_evidence(NS(key=f'ABC-{number}', id=str(number))) is None
            assert validator._pr_lookup_failures
    # Summary, remotelink, direct details and properties still share 120 seconds.
    assert validator.jira_http.get.call_count <= 50
    assert now[0] <= rc.PR_RELEASE_TIMEOUT_SECONDS


def test_global_deadline_stops_even_healthy_but_slow_sources(validator, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rc.time, 'monotonic', lambda: now[0])
    def slow(url, **kwargs):
        now[0] += min(1.0, kwargs['timeout'].total)
        return response(200, summary_payload(1))
    validator.jira_http.get.side_effect = slow
    with validator._pr_lookup_budget(5):
        for number in range(10):
            validator._find_issue_pull_request_only_evidence(NS(key=f'ABC-{number}', id=str(number)))
    assert validator.jira_http.get.call_count == 5
    assert now[0] == 5


def test_detail_queries_only_reported_provider_and_requested_type(validator):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return summary_payload(1, {'custom-git': {'count': 1}})
        assert kwargs['params']['applicationType'] == 'custom-git'
        assert kwargs['params']['dataType'] == 'pullrequest'
        return {'detail': [{'pullRequests': [{'id': 42}]}]}
    validator._pr_get_json = Mock(side_effect=fetch)
    assert validator._get_dev_status_payloads('123', data_types=('pullrequest',))
    assert validator._pr_get_json.call_count == 2
    validator._get_dev_status_payloads('123', data_types=('pullrequest',))
    assert validator._pr_get_json.call_count == 2


def test_empty_detail_with_positive_summary_is_not_success(validator):
    validator._pr_get_json = Mock(side_effect=lambda url, **kw:
        summary_payload(1, {'stash': {'count': 1}}) if url.endswith('/summary') else {'detail': []})
    with pytest.raises(RuntimeError, match='пустой или недоступный detail'):
        validator._get_dev_status_payloads('123', data_types=('pullrequest',))


def test_optional_gigacode_failure_does_not_also_log_success(validator):
    issue = NS(key='ABC-1', fields=NS(labels=[]))
    validator._get_release_pr_targets = Mock(return_value=[(issue, [issue])])
    validator._issue_has_gigacode_pull_request = Mock(side_effect=RuntimeError('timeout'))
    validator._check_gigacode_aifixed_labels('REL-1')
    assert [call.args[1] for call in validator._log_issue.call_args_list] == ['warning']


def stage_validator():
    from collections import defaultdict
    checker = rc.ReleaseValidator.__new__(rc.ReleaseValidator)
    checker.report_data = defaultdict(lambda: {
        'summary': '', 'assignee': 'Не назначен', 'url': '',
        'errors': [], 'warnings': [], 'success': []})
    checker.check_results = []
    checker._active_check = None
    return checker


def test_explicit_tls_policy_survives_requests_ca_bundle(monkeypatch):
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', '/corporate/ca.pem')
    session = rc.ReleaseHttpSession()
    session.verify = False
    session.send = Mock(return_value=response(200, {}))
    session.get('https://jira.example/rest/atm/1.0/testcase/ABC-1')
    assert session.send.call_args.kwargs['verify'] is False
    session.verify = '/configured/ca.pem'
    session.get('https://jira.example/rest/atm/1.0/testcase/ABC-1')
    assert session.send.call_args.kwargs['verify'] == '/configured/ca.pem'


def test_tls_policy_allows_ca_bundle_and_strict_verification(monkeypatch):
    monkeypatch.delenv('RELEASE_CHECKER_CA_BUNDLE', raising=False)
    monkeypatch.setenv('RELEASE_CHECKER_VERIFY_SSL', '1')
    assert rc.release_tls_policy() is True
    monkeypatch.setenv('RELEASE_CHECKER_CA_BUNDLE', '/configured/ca.pem')
    assert rc.release_tls_policy() == '/configured/ca.pem'


def test_zephyr_certificate_failure_is_not_retried_or_reported_as_no_cases(monkeypatch):
    client = rc.ZephyrScaleClient('https://jira.example', 'synthetic-test-token')
    client.session.get = Mock(side_effect=requests.exceptions.SSLError('certificate verify failed'))
    sleep = Mock()
    monkeypatch.setattr(rc.time, 'sleep', sleep)
    for key in ('ABC-1', 'ABC-2'):
        cases, failure = client.get_test_cases_for_issue(key)
        assert cases == []
        assert 'TLS' in failure
    assert client.session.get.call_count == 1
    sleep.assert_not_called()


def test_pr_certificate_failure_explains_circuit_breaker(validator):
    validator.jira_http.get.side_effect = requests.exceptions.SSLError('certificate verify failed')
    for issue_id in ('1', '2'):
        validator._pr_get_json('https://jira.example/rest/dev-status/latest/issue/summary', params={'issueId': issue_id})
    assert validator.jira_http.get.call_count == 1
    assert any('TLS' in failure for failure in validator._pr_lookup_failures)


def test_mandatory_incomplete_stage_blocks_release_and_next_stage_runs(capsys):
    checker = stage_validator()
    def unavailable():
        checker._log_issue('ABC-1', 'warning', 'Zephyr: не удалось получить ТК (TLS)')
    checker._run_check('zephyr', 'Zephyr', unavailable)
    checker._run_check('stories', 'Story', lambda: checker._log_issue('ABC-1', 'success', 'Описание заполнено'))
    assert [r['status'] for r in checker.check_results] == ['INCOMPLETE', 'PASS']
    assert checker.report_data['ABC-1']['errors']
    output = capsys.readouterr().out
    assert '[CHECK zephyr] START' in output
    assert '[CHECK zephyr] INCOMPLETE' in output
    assert '[CHECK stories] PASS' in output


def test_unexpected_stage_exception_isolated_and_optional_warning_preserved():
    checker = stage_validator()
    def crash():
        raise RuntimeError('synthetic failure')
    checker._run_check('bug', 'Bugs', crash)
    checker._run_check('giga', 'GigaChat', crash, required=False)
    checker._run_check('web', 'Web', Mock(), skip_reason='нет лейбла web')
    assert [r['status'] for r in checker.check_results] == ['INCOMPLETE', 'INCOMPLETE', 'SKIP']
    assert len(checker.report_data['GENERAL']['errors']) == 1
    assert len(checker.report_data['GENERAL']['warnings']) == 1
    assert 'нет лейбла web' in checker._checks_wiki_table()


def test_comment_includes_warnings_and_execution_table_on_failure():
    checker = stage_validator()
    checker.my_account_id = 'synthetic-account'
    checker.jira_main = Mock()
    checker.jira_main.comments.return_value = []
    checker._run_check('giga', 'GigaCode', lambda: checker._log_issue('ABC-1', 'warning', 'AIFIXED: не удалось проверить'), required=False)
    checker._run_check('story', 'Story', lambda: checker._log_issue('ABC-1', 'error', 'Нет описания'))
    checker._manage_jira_comment('REL-1', False)
    body = checker.jira_main.add_comment.call_args.args[1]
    assert 'Не удалось определить, использовался ли GigaCode при написании кода. Необходимость лейбла AIFIXED не установлена.' in body
    assert 'Нет описания' in body
    assert 'INCOMPLETE' not in body
    assert rc.COMMENT_TABLE_HEADER in body
    assert '#de350b' in body and '#14892c' not in body
    assert len([line for line in body.splitlines() if line.startswith('| ')]) == 1


def test_comment_keeps_optional_warnings_on_success():
    checker = stage_validator()
    checker.my_account_id = 'synthetic-account'
    checker.jira_main = Mock()
    checker.jira_main.comments.return_value = []
    checker._run_check('giga', 'GigaCode', lambda: checker._log_issue('ABC-1', 'warning', 'AIFIXED: не удалось проверить'), required=False)
    checker._manage_jira_comment('REL-1', True)
    body = checker.jira_main.add_comment.call_args.args[1]
    assert 'Не удалось определить, использовался ли GigaCode при написании кода. Необходимость лейбла AIFIXED не установлена.' in body
    assert rc.COMMENT_TABLE_HEADER in body
    assert '#de350b' in body  # Non-PR remarks are red even when advisory.
    assert 'INCOMPLETE' not in body


def test_cached_jira_reads_cache_only_identical_successful_requests():
    raw = Mock()
    raw.issue.side_effect = [requests.Timeout(), 'plain', 'history']
    client = rc.CachedJiraReads(raw)
    with pytest.raises(requests.Timeout):
        client.issue('ABC-1')
    assert client.issue('ABC-1') == 'plain'
    assert client.issue('ABC-1') == 'plain'
    assert client.issue('ABC-1', expand='changelog') == 'history'
    assert raw.issue.call_count == 3
    client.add_comment('ABC-1', 'test')
    client.add_comment('ABC-1', 'test')
    assert raw.add_comment.call_count == 2
    client.clear()
    assert not client.cache


def test_merged_pr_in_structured_array_without_url_is_detected(validator):
    payload = {'detail': [{'pullRequests': [{'id': 42, 'status': 'MERGED'}]}]}
    assert validator._extract_merged_pull_request_evidence_from_payload(payload) == '42 (merged)'


@pytest.mark.parametrize('failed_method,ready', [
    ('_check_artifacts', False),
    ('_check_required_pull_requests', True),
    ('_check_unlinked_release_items_pull_requests', True),
])
def test_all_applicable_release_stages_execute_despite_one_crash(failed_method, ready, monkeypatch):
    monkeypatch.setattr(rc.ReleaseValidator, '_check_sdd_gate',
                        lambda self, key: self._log_issue(key, 'success', 'SDD: проверка пройдена'))
    checker = stage_validator()
    release = NS(key='REL-1', fields=NS(summary='Cloud release', labels=['back', 'web'],
                                      assignee=None, issuelinks=[]))
    checker.jira_main = Mock()
    checker.jira_main.issue.return_value = release
    checker.zephyr = NS(failed_requests=0, fatal_error=None, last_test_cycle_search_stats=[])
    checker._consist_of_cache = {}
    checker._release_service_infos_cache = {}
    methods = [
        '_check_release_service_consistency', '_check_test_subtask', '_check_artifacts', '_check_release_coverage',
        '_check_zephyr_test_cases', '_check_back_release_service_test_cycles',
        '_check_web_release_service_test_cycles', '_check_bugs', '_check_stories',
        '_check_required_pull_requests', '_check_unlinked_release_items_pull_requests',
        '_check_gigacode_aifixed_labels', '_check_cloud_label',
        '_check_sbrppl_third_party_label', '_check_sbrppl_story_points',
        '_check_summary_description_match']
    for name in methods:
        setattr(checker, name, Mock())
    getattr(checker, failed_method).side_effect = RuntimeError('synthetic outage')
    assert checker.check_release('REL-1') is ready
    for name in methods:
        getattr(checker, name).assert_called_once()
    assert len(checker.check_results) == 18
    assert checker.check_results[0]['id'] == 'sdd'
    assert not any(r['status'] == 'RUNNING' for r in checker.check_results)


def test_dry_run_does_not_write_comment_or_aifixed_label(validator):
    checker = stage_validator()
    checker.dry_run = True
    checker.jira_main = Mock()
    checker._manage_jira_comment('REL-1', False)
    checker.jira_main.add_comment.assert_not_called()
    checker.jira_main.comments.assert_not_called()
    validator.dry_run = True
    issue = NS(key='ABC-1', fields=NS(labels=[]))
    validator._get_release_pr_targets = Mock(return_value=[(issue, [issue])])
    validator._issue_has_gigacode_pull_request = Mock(return_value='#GigaCode Agent')
    validator._add_label_if_missing = Mock()
    validator._check_gigacode_aifixed_labels('REL-1')
    validator._add_label_if_missing.assert_not_called()
    assert 'dry-run' in validator._log_issue.call_args.args[2]


def test_failed_zephyr_service_does_not_trigger_direct_cycle_scan(validator):
    validator._zephyr_release_cycles_cache = {}
    validator.zephyr = Mock(last_test_cycle_search_stats=[])
    validator.zephyr.lookup_budget.return_value = nullcontext()
    validator.zephyr.get_test_cycles_for_issue.return_value = []
    validator.zephyr.is_failed_request_limit_reached.return_value = True
    validator._get_test_cycles_from_jira_release_metadata = Mock()
    validator._get_test_cycles_from_direct_cache_or_scan = Mock()
    validator._zephyr_last_cycle_search_had_technical_failure = Mock(return_value=True)
    validator._log_test_cycle_search_debug = Mock()
    assert validator._get_release_test_cycles('REL-1') == []
    validator._get_test_cycles_from_jira_release_metadata.assert_not_called()
    validator._get_test_cycles_from_direct_cache_or_scan.assert_not_called()


def test_separate_stage_budgets_prevent_previous_stage_starvation(monkeypatch):
    checker = stage_validator()
    now = [0.0]
    monkeypatch.setattr(rc.time, 'monotonic', lambda: now[0])
    deadlines = []
    def first():
        deadlines.append(checker._pr_request_deadline)
        now[0] += 20
    def second():
        deadlines.append(checker._pr_request_deadline)
    checker._run_check('pr', 'PR', first, budget=20)
    checker._run_check('merged', 'Merged', second, budget=20)
    assert deadlines == [20, 40]
    assert checker._pr_request_deadline is None


def test_http_trace_does_not_expose_query_headers_or_arbitrary_params(monkeypatch, capsys):
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '1')
    rc.trace_http('PR', 'https://jira.example/path?token=secret', 200, rc.time.monotonic(),
                  params={'issueId': '123', 'token': 'secret', 'password': 'secret'})
    output = capsys.readouterr().out
    assert '123' in output
    assert 'secret' not in output
    assert 'token' not in output


@pytest.fixture
def fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(rc.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(rc.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    return now


def test_pr_recovers_on_third_attempt_with_short_backoff(validator, fake_clock):
    validator.jira_http.get.side_effect = [requests.Timeout(), response(503), response(200, summary_payload(1))]
    assert validator._pr_get_json('https://jira.example/summary') == summary_payload(1)
    assert validator.jira_http.get.call_count == 3
    assert fake_clock[0] == 1.5
    assert not validator._pr_lookup_failures


def test_retry_after_honored_inside_budget(validator, fake_clock):
    validator.jira_http.get.side_effect = [response(429, headers={'Retry-After': '2'}), response(200, {})]
    with validator._pr_lookup_budget(5):
        assert validator._pr_get_json('https://jira.example/summary') == {}
    assert fake_clock[0] == 2


def test_retry_after_cannot_extend_budget(validator, fake_clock):
    validator.jira_http.get.return_value = response(429, headers={'Retry-After': '60'})
    with validator._pr_lookup_budget(5):
        assert validator._pr_get_json('https://jira.example/summary') is None
    assert validator.jira_http.get.call_count == 1
    assert fake_clock[0] == 0
    assert any('лимит' in reason for reason in validator._pr_lookup_failures)


@pytest.mark.parametrize('header,expected', [('3', 3), ('-1', 0), ('NaN', 0), ('invalid', 0)])
def test_retry_after_numeric_and_invalid(header, expected):
    assert rc.retry_after_seconds(response(429, headers={'Retry-After': header})) == expected


def test_retry_after_http_date():
    from datetime import datetime, timezone, timedelta
    from email.utils import format_datetime
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    assert 3 < rc.retry_after_seconds(response(429, headers={'Retry-After': date})) <= 5


def test_zero_deadline_prevents_request(validator, fake_clock):
    with validator._pr_lookup_budget(0):
        assert validator._pr_get_json('https://jira.example/summary') is None
    validator.jira_http.get.assert_not_called()


@pytest.fixture
def zephyr(monkeypatch):
    monkeypatch.setattr(rc.time, 'sleep', lambda _: None)
    client = rc.ZephyrScaleClient('https://jira.example', 'synthetic-test-token')
    client.session.get = Mock()
    return client


def case_payload(key='HRPQA-T1', **overrides):
    return dict({'key': key, 'name': 'Synthetic case', 'status': 'Approved',
                 'customFields': {'Вид тестирования': 'Регресс'}, 'issueLinks': ['ABC-1']}, **overrides)


def test_zephyr_restores_legacy_full_response_without_projection(zephyr):
    zephyr.session.get.return_value = response(200, [case_payload()])
    assert zephyr.get_test_cases_for_issue('ABC-1') == ([case_payload()], None)
    assert zephyr.session.get.call_count == 1
    assert 'fields' not in zephyr.session.get.call_args.kwargs.get('params', {})


def test_zephyr_fallback_uses_documented_search_and_all_pages(zephyr):
    first = [case_payload(f'HRPQA-T{i}') for i in range(1, 101)]
    zephyr.session.get.side_effect = [response(404), response(200, first), response(200, [case_payload('HRPQA-T101')])]
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert error is None and len(cases) == 101
    calls = zephyr.session.get.call_args_list
    assert calls[1].args[0].endswith('/testcase/search')
    assert calls[1].kwargs['params']['query'] == 'projectKey = "HRPQA" AND issueKeys IN ("ABC-1")'
    assert [c.kwargs['params']['startAt'] for c in calls[1:]] == [0, 100]


def test_zephyr_explicit_pagination_overrides_short_page(zephyr):
    zephyr.session.get.side_effect = [response(404),
        response(200, {'values': [case_payload()], 'total': 2}),
        response(200, {'values': [case_payload('HRPQA-T2')], 'total': 2})]
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert error is None and len(cases) == 2


def test_zephyr_unknown_schema_is_retried_then_searched(zephyr):
    zephyr.session.get.side_effect = [response(200, {'unexpected': []})] * 3 + [response(200, [case_payload()])]
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert error is None and cases == [case_payload()]
    assert zephyr.session.get.call_count == 4


@pytest.mark.parametrize('payload', [{}, {'unexpected': []}, {'errors': ['unavailable']}, [{'name': 'no key'}]])
def test_zephyr_bad_schema_is_never_zero_coverage(zephyr, payload):
    zephyr.session.get.return_value = response(200, payload)
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert error and cases == []
    assert zephyr.session.get.call_count <= 6


def test_zephyr_repeated_final_page_is_incomplete(zephyr):
    zephyr.session.get.side_effect = [response(404),
        response(200, {'values': [case_payload()], 'total': 2}),
        response(200, {'values': [case_payload()], 'total': 2})]
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert cases == [case_payload()]
    assert 'пагинация' in error


def test_zephyr_primary_and_fallback_share_deadline(zephyr, fake_clock):
    def timeout(url, **kwargs):
        fake_clock[0] += kwargs['timeout'].total
        raise requests.Timeout('synthetic outage')
    zephyr.session.get.side_effect = timeout
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert error and cases == []
    assert fake_clock[0] <= rc.ZEPHYR_LOOKUP_TIMEOUT_SECONDS
    assert zephyr.session.get.call_count <= 6


@pytest.mark.parametrize('status', [401, 403])
def test_zephyr_auth_failure_stops_without_fallback(zephyr, status):
    zephyr.session.get.return_value = response(status)
    for key in ['ABC-1', 'ABC-2']:
        cases, error = zephyr.get_test_cases_for_issue(key)
        assert not cases and str(status) in error
    assert zephyr.session.get.call_count == 1


def test_zephyr_detail_key_mismatch_is_retried(zephyr):
    zephyr.session.get.side_effect = [response(200, case_payload('HRPQA-T2')), response(200, case_payload())]
    assert zephyr.get_test_case_details('HRPQA-T1') == case_payload()
    assert zephyr.session.get.call_count == 2


def zephyr_checker(zephyr):
    checker = stage_validator()
    checker.zephyr = zephyr
    checker.jira_main = Mock()
    checker._zephyr_issue_test_cases_cache = {}
    checker._zephyr_cycle_approved_checked = set()
    checker._zephyr_cycle_testing_type_checked = set()
    checker._zephyr_test_case_details_cache = {}
    checker._get_consist_of_issues = Mock(return_value=['ABC-1'])
    checker._build_expected_testing_type_map = Mock(return_value={'ABC-1': 'Регресс'})
    checker._get_issue_type_map = Mock(return_value={'ABC-1': 'story'})
    return checker


def test_coverage_and_case_policy_reuse_full_zephyr_response_without_xray(zephyr):
    checker = zephyr_checker(zephyr)
    zephyr.session.get.return_value = response(200, [case_payload()])
    checker._run_check('coverage', 'Покрытие', lambda: checker._check_release_coverage('REL-1'))
    checker._run_check('cases', 'ТК', lambda: checker._check_zephyr_test_cases('REL-1'))
    assert [r['status'] for r in checker.check_results] == ['PASS', 'PASS']
    assert zephyr.session.get.call_count == 1
    checker.jira_main.search_issues.assert_not_called()


@pytest.mark.parametrize('overrides,expected', [
    ({'status': 'Draft'}, 'FAIL'),
    ({'customFields': {'Вид тестирования': 'Новый функционал'}}, 'FAIL'),
    ({'customFields': {}}, 'INCOMPLETE'),
    ({'status': ''}, 'INCOMPLETE'),
])
def test_zephyr_actual_policy_errors_and_missing_data_block_release(zephyr, overrides, expected):
    checker = zephyr_checker(zephyr)
    case = case_payload(**overrides)
    zephyr.session.get.side_effect = lambda url, **kw: response(200, [case] if url.endswith('/testcases') else case)
    checker._run_check('cases', 'ТК', lambda: checker._check_zephyr_test_cases('REL-1'))
    assert checker.check_results[0]['status'] == expected
    assert checker.report_data['ABC-1']['errors']


@pytest.mark.parametrize('payload', [[], [case_payload(issueLinks=['ABC-10'])]])
def test_no_direct_coverage_is_a_failure(zephyr, payload):
    checker = zephyr_checker(zephyr)
    zephyr.session.get.return_value = response(200, payload)
    checker._run_check('coverage', 'Покрытие', lambda: checker._check_release_coverage('REL-1'))
    assert checker.check_results[0]['status'] == 'FAIL'
    assert not checker.report_data['ABC-1']['success']


def test_zephyr_failed_list_not_cached_and_can_recover_next_stage(zephyr):
    checker = zephyr_checker(zephyr)
    zephyr.get_test_cases_for_issue = Mock(side_effect=[([], 'timeout'), ([case_payload()], None)])
    checker._run_check('coverage', 'Покрытие', lambda: checker._check_release_coverage('REL-1'))
    checker._run_check('cases', 'ТК', lambda: checker._check_zephyr_test_cases('REL-1'))
    assert [r['status'] for r in checker.check_results] == ['INCOMPLETE', 'PASS']
    assert zephyr.get_test_cases_for_issue.call_count == 2
    zephyr.session.get.assert_not_called()


def test_zephyr_failed_detail_not_cached(zephyr):
    checker = zephyr_checker(zephyr)
    zephyr.get_test_case_details = Mock(side_effect=[None, case_payload()])
    assert checker._get_test_case_details_cached('HRPQA-T1') is None
    assert checker._get_test_case_details_cached('HRPQA-T1') == case_payload()


@pytest.mark.parametrize('custom_fields', [
    {'Вид тестирования': {'name': 'Регресс'}},
    [{'name': 'Вид тестирования', 'value': {'name': 'Регресс'}}],
])
def test_zephyr_testing_type_supports_structured_fields(zephyr, custom_fields):
    assert zephyr.get_test_case_custom_field({'customFields': custom_fields}, 'Вид тестирования') == 'Регресс'


@pytest.mark.parametrize('detail', [None, case_payload(status='')])
def test_cycle_missing_case_data_never_logs_all_approved(zephyr, detail):
    checker = zephyr_checker(zephyr)
    checker._get_test_cycle_details_cached = Mock(return_value={'items': [{'testCaseKey': 'HRPQA-T1'}]})
    checker._get_test_case_details_cached = Mock(return_value=detail)
    checker._run_check('cycle', 'ТЦ', lambda: checker._check_test_cycle_cases_approved('REL-1', 'HRPQA-R1', 'Cycle'))
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    assert not checker.report_data['REL-1']['success']


def test_field_metadata_retried_and_failed_load_not_cached(validator):
    validator._jira_field_name_to_id_cache = None
    fields = [{'id': 'customfield_1', 'name': 'КЭ сервиса'}]
    validator.jira_http.get.side_effect = [requests.Timeout()] * 3 + [response(200, fields)]
    assert validator._get_jira_field_id_by_names(('КЭ сервиса',)) is None
    assert validator._jira_field_name_to_id_cache is None
    assert validator._get_jira_field_id_by_names(('КЭ сервиса',)) == 'customfield_1'
    assert validator._get_jira_field_id_by_names(('КЭ сервиса',)) == 'customfield_1'
    assert validator.jira_http.get.call_count == 4


def test_field_metadata_failure_does_not_poison_service_cache(validator):
    validator._release_service_infos_cache = {}
    validator._get_consist_of_issues = Mock(return_value=['ABC-1'])
    validator._get_jira_field_id_by_names = Mock(return_value=None)
    for check in ('Back', 'Web'):
        assert validator._collect_release_service_infos('REL-1', check) is None
    assert validator._get_jira_field_id_by_names.call_count == 2


def test_comment_escapes_markup_and_encoded_newlines_without_extra_rows():
    import re
    checker = stage_validator()
    checker._run_check('case', 'Zephyr | ТК', lambda: checker._log_issue('ABC-1', 'error',
        'Draft | wrong\n{panel} &#10;|| injected || [text|url]'))
    checker._log_issue('GENERAL', 'warning', 'Внешнее предупреждение')
    body = checker._checks_wiki_table()
    assert len(body.splitlines()) == 3
    assert '{panel}' not in body and '|| injected ||' not in body
    assert '&#124;' in body
    for row in body.splitlines()[1:]:
        assert re.sub(r'\[ABC-1\|[^]]+\]', 'ABC-1', row).count('|') == 7
    assert 'Внешнее предупреждение' in body


def test_empty_checks_comment_is_explicit_incomplete_table():
    body = stage_validator()._checks_wiki_table()
    assert body.startswith(rc.CHECK_TABLE_HEADER)
    assert len(body.splitlines()) == 2
    assert 'INCOMPLETE' in body


def test_existing_bot_table_comment_is_recognized():
    checker = stage_validator()
    checker.my_account_id = 'bot'
    checker.jira_main = Mock()
    own = Mock(author=NS(accountId='bot'), body=rc.CHECK_TABLE_HEADER + '\n| old |')
    other = Mock(author=NS(accountId='human'), body=rc.CHECK_TABLE_HEADER + '\n| other |')
    checker.jira_main.comments.return_value = [own, other]
    checker._manage_jira_comment('REL-1', False)
    own.delete.assert_called_once()
    other.delete.assert_not_called()


@pytest.mark.parametrize('provider', rc.PR_LEGACY_PROVIDERS)
def test_pr_presence_survives_missing_summary_through_legacy_detail(validator, provider):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return response(404)
        if url.endswith('/remotelink'):
            return response(200, [])
        if url.endswith('/detail') and kwargs['params']['applicationType'] == provider:
            return response(200, {'detail': [{'pullRequests': [{'id': 42, 'status': 'OPEN'}]}]})
        return response(200, {'detail': []})
    validator.jira_http.get.side_effect = fetch
    assert 'PR 42' in validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert not validator._pr_lookup_failures
    detail_calls = [c for c in validator.jira_http.get.call_args_list if c.args[0].endswith('/detail')]
    assert all(c.kwargs['params']['dataType'] == 'pullrequest' for c in detail_calls)


def test_merged_pr_details_work_when_summary_is_unavailable(validator):
    validator._merged_pull_request_evidence_cache = {}
    validator._pr_get_json = Mock(side_effect=lambda url, **kw:
        None if url.endswith('/summary') else {'detail': [{'pullRequests': [{'id': 42, 'status': 'MERGED'}]}]})
    assert '42 (merged)' in validator._find_issue_merged_pull_request_evidence(NS(key='ABC-1', id='123'))


def test_gigacode_in_repository_survives_summary_outage(validator):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return None
        data_type = kwargs['params']['dataType']
        if data_type == 'repository':
            return {'detail': [{'repositories': [{'commits': [{'message': 'Co-authored-by: GigaCode Assistant'}]}]}]}
        return {'detail': [{'pullRequests': [], 'commits': []}]}
    validator._pr_get_json = Mock(side_effect=fetch)
    assert 'GigaCode Assistant' in validator._issue_has_gigacode_pull_request(NS(key='ABC-1', id='123'))


def test_gigacode_marker_in_pr_does_not_require_other_sources(validator):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return summary_payload(1, {'stash': {'count': 1}})
        assert kwargs['params']['dataType'] == 'pullrequest'
        return {'detail': [{'pullRequests': [{'id': 42, 'description': 'Co-authored-by: GigaCode Assistant'}]}]}
    validator._pr_get_json = Mock(side_effect=fetch)
    assert 'GigaCode Assistant' in validator._issue_has_gigacode_pull_request(NS(key='ABC-1', id='123'))
    assert validator._pr_get_json.call_count == 2


def test_failed_cycle_search_does_not_poison_following_stage(zephyr):
    zephyr.get_test_cycles_from_frontend = Mock(return_value=([], False))
    checker = zephyr_checker(zephyr)
    checker._zephyr_release_cycles_cache = {}
    cycle = {'key': 'HRPQA-C1', 'name': 'REL-1 API Регресс'}
    zephyr.get_test_cycles_for_issue = Mock(side_effect=[[], [cycle]])
    checker._get_test_cycles_from_direct_cache_or_scan = Mock(return_value=[])
    checker._get_test_cycles_from_jira_release_metadata = Mock(return_value=[])
    checker._zephyr_last_cycle_search_had_technical_failure = Mock(return_value=True)
    checker._log_test_cycle_search_debug = Mock()
    assert checker._get_release_test_cycles('REL-1') == []
    assert checker._get_release_test_cycles('REL-1') == [('HRPQA-C1', 'REL-1 API Регресс')]
    assert zephyr.get_test_cycles_for_issue.call_count == 2


def test_properties_source_removed_today_is_restored(validator):
    def fetch(url, **kwargs):
        if url.endswith('/properties'):
            return response(200, {'keys': [{'key': 'unrelated'}, {'key': 'development/pr links'}]})
        if url.endswith('/properties/development%2Fpr%20links'):
            return response(200, {'value': {'pullRequests': [{'id': 42, 'status': 'OPEN'}]}})
        return response(404)
    validator.jira_http.get.side_effect = fetch
    evidence = validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert 'Jira issue property' in evidence and 'PR 42' in evidence
    assert not any(c.args[0].endswith('/properties/unrelated') for c in validator.jira_http.get.call_args_list)


def test_property_fallback_keeps_time_after_slow_summary_and_details(validator, fake_clock):
    def fetch(url, **kwargs):
        if url.endswith('/properties'):
            return response(200, {'keys': [{'key': 'dev'}]})
        if url.endswith('/properties/dev'):
            return response(200, {'pullRequests': [{'id': 42}]})
        fake_clock[0] += kwargs['timeout'].total
        raise requests.Timeout()
    validator.jira_http.get.side_effect = fetch
    assert 'property dev' in validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert fake_clock[0] < rc.PR_LOOKUP_TIMEOUT_SECONDS


def test_slow_detail_longer_than_old_three_second_limit_recovers(validator, fake_clock):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return response(404)
        if url.endswith('/remotelink'):
            return response(200, [])
        assert kwargs['timeout'].total >= 8
        fake_clock[0] += 8
        return response(200, {'detail': [{'pullRequests': [{'id': 42}]}]})
    validator.jira_http.get.side_effect = fetch
    assert validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert fake_clock[0] == 8


def test_transient_pr_failure_expires_within_run(validator, fake_clock):
    url = 'https://jira.example/rest/dev-status/latest/issue/detail'
    validator.jira_http.get.side_effect = requests.Timeout()
    assert validator._pr_get_json(url) is None
    fake_clock[0] += rc.PR_FAILURE_COOLDOWN_SECONDS
    validator.jira_http.get.side_effect = None
    validator.jira_http.get.return_value = response(200, {'detail': [{'pullRequests': [{'id': 42}]}]})
    assert validator._pr_get_json(url)['detail']


def test_pr_next_stage_can_recover_but_tls_remains_permanent(validator):
    temporary = 'https://jira.example/temporary'
    permanent = 'https://jira.example/permanent'
    validator.jira_http.get.side_effect = lambda url, **kw: response(503 if url == temporary else 403)
    assert validator._pr_get_json(temporary) is None
    assert validator._pr_get_json(permanent) is None
    validator._begin_pr_stage()
    validator.jira_http.get.side_effect = lambda *a, **kw: response(200, {})
    assert validator._pr_get_json(temporary) == {}
    assert validator._pr_get_json(permanent) is None


def test_retry_after_applies_to_other_pr_sources_and_stages(validator, fake_clock):
    validator.jira_http.get.return_value = response(429, headers={'Retry-After': '60'})
    with validator._pr_lookup_budget(5):
        assert validator._pr_get_json('https://jira.example/summary') is None
    validator._begin_pr_stage()
    with validator._pr_lookup_budget(5):
        assert validator._pr_get_json('https://jira.example/detail') is None
    assert validator.jira_http.get.call_count == 1
    assert fake_clock[0] == 0


def test_zephyr_retry_after_cannot_be_bypassed_by_search(zephyr, fake_clock):
    zephyr.session.get.return_value = response(429, headers={'Retry-After': '90'})
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert error and not cases
    assert zephyr.session.get.call_count == 1
    assert fake_clock[0] == 0


def test_jenkins_long_retry_after_does_not_poison_unused_pr_sources(validator, fake_clock, capsys, monkeypatch):
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '1')
    validator.jira_http.get.return_value = response(429, headers={'Retry-After': '120'})
    for number in range(24):
        issue = NS(key=f'ABC-{number + 1}', id=str(number + 1))
        assert validator._find_issue_pull_request_only_evidence(issue) is None
        assert any('Retry-After' in reason for reason in validator._pr_lookup_failures)
    assert validator.jira_http.get.call_count == 1
    assert fake_clock[0] == 0
    assert sum(validator._pr_endpoint_failures.values()) == 1
    assert len(validator._pr_http_cache) == 1
    assert len(validator._pr_endpoint_retry_at) == 1
    validator._begin_pr_stage()
    with pytest.raises(RuntimeError, match='Retry-After'):
        validator._get_dev_status_payloads('25')
    assert validator.jira_http.get.call_count == 1
    output = capsys.readouterr().out
    assert output.count('"result": "rate-limited"') == 1
    assert 'circuit-open' not in output

    # The pause expires naturally; no stage reset is needed to recover.
    fake_clock[0] = 120
    validator.jira_http.get.return_value = response(200, summary_payload(1))
    assert validator._find_issue_pull_request_only_evidence(NS(key='ABC-2', id='2'))
    assert validator.jira_http.get.call_count == 2


def test_zephyr_unsent_cooldown_requests_do_not_exhaust_failure_limit(zephyr, fake_clock):
    zephyr.session.get.return_value = response(429, headers={'Retry-After': '120'})
    for number in range(30):
        cases, error = zephyr.get_test_cases_for_issue(f'ABC-{number + 1}')
        assert not cases and 'Retry-After' in error
    assert zephyr.session.get.call_count == 1
    assert zephyr.failed_requests == 1
    assert not zephyr.is_failed_request_limit_reached()
    assert fake_clock[0] == 0
    fake_clock[0] = 120
    zephyr.session.get.return_value = response(200, [case_payload()])
    cases, error = zephyr.get_test_cases_for_issue('ABC-1')
    assert cases and error is None
    assert zephyr.session.get.call_count == 2


@pytest.mark.parametrize('method', ['_get_once', '_get_with_retries'])
def test_zephyr_unsent_deadline_does_not_count_as_failure(zephyr, fake_clock, method):
    with zephyr.lookup_budget(0):
        with pytest.raises(rc.RequestNotSent):
            getattr(zephyr, method)('https://jira.example/endpoint')
    zephyr.session.get.assert_not_called()
    assert zephyr.failed_requests == 0


def test_cached_positive_pr_survives_server_pause(validator, fake_clock):
    issue = NS(key='ABC-1', id='1')
    validator._pull_request_only_evidence_cache['ABC-1'] = 'PR 42'
    validator._pr_retry_state = {'until': 120}
    assert validator._find_issue_pull_request_only_evidence(issue) == 'PR 42'
    validator.jira_http.get.assert_not_called()


def test_direct_scan_does_not_walk_cycle_numbers_during_retry_after(zephyr, fake_clock):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_cache = {'projects': {}, 'release_map': {}}
    checker._get_cached_or_fetch_cycle_details = Mock()
    zephyr.retry_state['until'] = 120
    assert checker._get_test_cycles_from_direct_cache_or_scan('REL-1') == []
    checker._get_cached_or_fetch_cycle_details.assert_not_called()
    zephyr.session.get.assert_not_called()
    assert zephyr.last_test_cycle_search_stats[-1]['complete'] is False
    assert zephyr.failed_requests == 0


def test_zephyr_legacy_text_id_and_details_without_key(zephyr):
    case = {'id': 'HRPQA-T1', 'name': 'Legacy case'}
    details = {'status': 'Approved', 'customFields': {'Вид тестирования': 'Регресс'}, 'issueLinks': ['ABC-1']}
    zephyr.session.get.side_effect = [response(200, [case]), response(200, details)]
    assert zephyr.get_test_cases_for_issue('ABC-1') == ([case], None)
    assert zephyr.get_test_case_details('HRPQA-T1') == details
    assert all('params' not in c.kwargs for c in zephyr.session.get.call_args_list)


def test_failed_cycle_details_can_recover_in_same_run(zephyr):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_details_cache = {}
    zephyr.get_test_cycle_details = Mock(side_effect=[None, {'key': 'HRPQA-C1', 'name': 'Cycle'}])
    assert checker._get_test_cycle_details_cached('HRPQA-C1') is None
    assert checker._get_test_cycle_details_cached('HRPQA-C1')['name'] == 'Cycle'


def test_metadata_can_finish_after_ten_seconds(validator, fake_clock):
    validator._jira_field_name_to_id_cache = None
    def fetch(url, **kwargs):
        assert kwargs['timeout'].total > 10
        fake_clock[0] += 11
        return response(200, [{'id': 'customfield_1', 'name': 'КЭ сервиса'}])
    validator.jira_http.get.side_effect = fetch
    assert validator._get_jira_field_id_by_names(('КЭ сервиса',)) == 'customfield_1'
    assert validator.jira_http.get.call_count == 1


def test_comment_groups_release_objects_and_hides_technical_details():
    checker = stage_validator()
    checker._log_issue('ABC-1', 'error', 'Story: отсутствует описание')
    checker._log_issue('ABC-1', 'error', 'Pull Request не удалось подтвердить: HTTP 429; /rest/dev-status/latest; timeout=3')
    checker._log_issue('ABC-1', 'error', 'Pull Request не удалось подтвердить: connection error')
    checker._log_issue('GENERAL', 'warning', 'Zephyr debug: status=404; endpoint=internal')
    checker._log_issue('GENERAL', 'error', 'Не удалось загрузить metadata полей Jira: HTTPSConnectionPool(token=secret)')
    body = checker._release_comment('REL-1', False)
    assert rc.COMMENT_TABLE_HEADER in body
    assert 'Не удалось проверить ПР.' in body
    assert 'недоступны данные КЭ' in body
    assert 'отсутствует описание' in body
    assert body.count('[ABC-1|') == 1 and body.count('[REL-1|') == 1
    assert body.count('Не удалось проверить ПР.') == 1
    for forbidden in ('HTTP', 'timeout', 'endpoint', 'secret', 'INCOMPLETE', '/rest/', 'Время, с', 'debug'):
        assert forbidden not in body
    assert '#de350b' in body


def test_success_comment_is_green_with_single_release_row():
    checker = stage_validator()
    checker._log_issue('ABC-1', 'success', 'Approved; elapsed=2.5')
    body = checker._release_comment('REL-1', True)
    assert '#14892c' in body and '#de350b' not in body
    assert 'Ошибок нет.' in body
    assert body.count('| [REL-1|') == 1
    assert 'ABC-1' not in body and 'elapsed' not in body


def test_object_comment_escapes_wiki_and_encoded_line_breaks():
    import re
    checker = stage_validator()
    checker._log_issue('ABC-1', 'error', 'Draft | invalid {panel} &#10; || injected ||')
    body = checker._release_comment('REL-1', False)
    rows = [line for line in body.splitlines() if line.startswith('| ')]
    assert len(rows) == 1
    assert re.sub(r'\[ABC-1\|[^]]+\]', 'ABC-1', rows[0]).count('|') == 4
    assert body.count('{panel}') == 1
    assert '|| injected ||' not in body


@pytest.mark.parametrize('section', [1, {'overall': 1}, {'byInstanceType': []}])
def test_invalid_summary_metadata_still_allows_direct_pr(validator, section):
    validator._pr_get_json = Mock(side_effect=lambda url, **kw:
        {'summary': {'pullrequest': section}} if url.endswith('/summary') else
        {'detail': [{'pullRequests': [{'id': 42, 'status': 'OPEN'}]}]})
    payloads = validator._get_dev_status_payloads('123', data_types=('pullrequest',))
    assert validator._extract_pull_request_evidence(payloads[0][2]) == 'PR 42'


def test_comment_remains_red_if_success_flag_conflicts_with_errors():
    checker = stage_validator()
    checker._log_issue('ABC-1', 'error', 'Нет описания')
    assert '#de350b' in checker._release_comment('REL-1', True)


def test_legacy_requests_and_slow_pr_via_real_local_http(validator, monkeypatch):
    """Transport-level replay; synthetic data, only a local HTTP server is used."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            path = urlsplit(self.path).path
            status, payload = 200, {}
            if path.endswith('/summary'):
                status = 503
            elif path.endswith('/remotelink'):
                payload = []
            elif path.endswith('/detail'):
                threading.Event().wait(3.2)  # Earlier 3-second timeout loses this PR.
                payload = {'detail': [{'pullRequests': [{'id': 42, 'status': 'MERGED'}]}]}
            elif path.endswith('/testcases'):
                status = 400 if 'fields=' in self.path else 200
                payload = [{'id': 'HRPQA-T1', 'name': 'Legacy case'}]
            elif path.endswith('/testcase/HRPQA-T1'):
                status = 400 if 'fields=' in self.path else 200
                payload = {k: v for k, v in case_payload().items() if k != 'key'}
            else:
                status = 404
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    monkeypatch.setitem(rc.config['jira'], 'url', base)
    validator.jira_http = rc.ReleaseHttpSession()
    validator.jira_http.trust_env = False
    zephyr = rc.ZephyrScaleClient(base, 'synthetic-token')
    zephyr.session.trust_env = False
    try:
        assert 'PR 42' in validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
        checker = zephyr_checker(zephyr)
        checker._run_check('coverage', 'Покрытие', lambda: checker._check_release_coverage('REL-1'))
        checker._run_check('cases', 'ТК', lambda: checker._check_zephyr_test_cases('REL-1'))
        assert [r['status'] for r in checker.check_results] == ['PASS', 'PASS']
        assert sum('/issue/detail?' in path for path in calls) == 1
        assert sum(path.endswith('/testcase/HRPQA-T1') for path in calls) == 1
    finally:
        validator.jira_http.close()
        zephyr.session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('lookup', [None, requests.Timeout('unavailable')])
def test_missing_or_unavailable_pr_is_green_with_object_remark(lookup):
    checker = stage_validator()
    issue = NS(key='ABC-1', fields=NS(summary='Release item', assignee=None))
    checker._get_release_pr_targets = Mock(return_value=[(issue, [issue])])
    checker._find_issue_pull_request_only_evidence = Mock()
    if isinstance(lookup, Exception):
        checker._find_issue_pull_request_only_evidence.side_effect = lookup
    else:
        checker._find_issue_pull_request_only_evidence.return_value = lookup
    checker._run_check('required_pr', 'Наличие связи PR',
                       lambda: checker._check_required_pull_requests('REL-1'), required=False)
    assert not any(data['errors'] for data in checker.report_data.values())
    assert len(checker.report_data['ABC-1']['warnings']) == 1
    body = checker._release_comment('REL-1', True)
    assert '#14892c' in body and '#de350b' not in body
    assert '[ABC-1|' in body and 'ПР' in body
    assert rc.COMMENT_TABLE_HEADER == '|| Объект релиза || Статус || Что требует внимания ||'


def test_real_violation_stays_red_alongside_missing_pr():
    checker = stage_validator()
    checker._log_issue('ABC-1', 'warning', 'Pull Request не найден.')
    checker._log_issue('ABC-2', 'error', 'Story: отсутствует описание')
    checker._log_issue('GENERAL', 'error', 'Не удалось проверить тест-циклы')
    body = checker._release_comment('REL-1', False)
    assert '#de350b' in body
    assert '[ABC-1|' in body and '[ABC-2|' in body and '[REL-1|' in body
    assert 'GENERAL' not in body
    assert 'INCOMPLETE' not in body and 'Время, с' not in body


def test_zephyr_direct_links_precede_broad_project_search(zephyr):
    cycle = {'key': 'HRPQA-C1', 'name': 'REL-1 API Регресс'}
    zephyr._get_test_cycles_by_issue_link = Mock(return_value=[cycle])
    zephyr._get_test_cycles_by_issue_query_params = Mock(side_effect=AssertionError('Unnecessary search'))
    assert zephyr.get_test_cycles_for_issue('REL-1') == [cycle]


def test_transient_search_failure_does_not_disable_direct_lookup(zephyr):
    zephyr.get_test_cycles_from_frontend = Mock(return_value=([], False))
    checker = zephyr_checker(zephyr)
    checker._zephyr_release_cycles_cache = {}
    cycle = {'key': 'HRPQA-C1', 'name': 'REL-1 API Регресс'}
    zephyr.get_test_cycles_for_issue = Mock(return_value=[])
    checker._get_test_cycles_from_jira_release_metadata = Mock(return_value=[])
    checker._zephyr_last_cycle_search_had_technical_failure = Mock(return_value=True)
    checker._get_test_cycles_from_direct_cache_or_scan = Mock(return_value=[cycle])
    assert checker._get_release_test_cycles('REL-1') == [('HRPQA-C1', 'REL-1 API Регресс')]
    checker._get_test_cycles_from_direct_cache_or_scan.assert_called_once()


def test_zephyr_name_search_reads_second_page(zephyr):
    first = [{'key': f'HRPQA-C{i}', 'name': f'REL-1 Cycle {i}'} for i in range(50)]
    last = {'key': 'HRPQA-C50', 'name': 'REL-1 API Регресс'}
    zephyr._get_with_retries = Mock(side_effect=[response(200, first), response(200, [last])])
    cycles = zephyr._search_test_cycles_by_name_query('HRPQA', 'REL-1')
    assert len(cycles) == 51 and cycles[-1] == last
    assert [c.kwargs['params']['startAt'] for c in zephyr._get_with_retries.call_args_list] == [0, 50]


def test_zephyr_repeated_page_not_cached_as_complete(zephyr):
    page = [{'key': f'HRPQA-C{i}', 'name': f'REL-1 Cycle {i}'} for i in range(50)]
    zephyr._get_with_retries = Mock(return_value=response(200, page))
    assert zephyr._search_test_cycles_by_name_query('HRPQA', 'REL-1') == []
    assert zephyr._get_with_retries.call_count == 2
    assert zephyr.last_test_cycle_search_stats[-1]['status'] == 'exception'


def test_pr_from_repository_survives_missing_summary_and_pullrequest_endpoint(validator):
    def fetch(url, **kwargs):
        if url.endswith('/detail') and kwargs['params']['dataType'] == 'repository':
            return {'detail': [{'repositories': [{'pullRequests': [{'id': 42}]}]}]}
        return None
    validator._pr_get_json = Mock(side_effect=fetch)
    assert 'PR 42' in validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))


def test_direct_cycle_scan_stops_at_its_budget(zephyr, fake_clock):
    checker = zephyr_checker(zephyr)
    def get(key):
        fake_clock[0] += 5
        return None
    zephyr.get_test_cycle_details = Mock(side_effect=get)
    with zephyr.lookup_budget(10):
        assert checker._refresh_direct_scan_high_watermark('HRPQA', 100) == 100
    assert zephyr.get_test_cycle_details.call_count == 2


@pytest.mark.parametrize('merged_result,blocking', [('PR 42 (merged)', True), (requests.Timeout(), False)])
def test_unlinked_merged_violation_blocks_but_unavailable_pr_does_not(merged_result, blocking):
    checker = stage_validator()
    checker._check_unlinked_other_releases = Mock(return_value=set())
    issue = NS(key='ABC-2', id='2', fields=NS(summary='Removed task', assignee=None,
               issuetype=NS(name='Story'), labels=[], issuelinks=[]))
    checker._get_unlinked_release_issue_keys = Mock(return_value={'ABC-2'})
    checker._get_release_story_bug_keys = Mock(return_value={'ABC-1'})
    checker.jira_main = Mock()
    checker.jira_main.search_issues.return_value = [issue]
    checker._find_issue_merged_pull_request_evidence = Mock()
    if isinstance(merged_result, Exception):
        checker._find_issue_merged_pull_request_evidence.side_effect = merged_result
    else:
        checker._find_issue_merged_pull_request_evidence.return_value = merged_result
    checker._run_check('unlinked_pr', 'Merged PR',
                       lambda: checker._check_unlinked_release_items_pull_requests('REL-1'), required=False)
    assert bool(checker.report_data['ABC-2']['errors']) is blocking
    body = checker._release_comment('REL-1', not blocking)
    assert ('#de350b' if blocking else '#14892c') in body
    assert '[ABC-2|' in body


def test_no_bugs_is_a_success_without_a_comment_remark():
    checker = stage_validator()
    checker.jira_main = Mock()
    checker.jira_main.search_issues.return_value = []
    checker._get_consist_of_issues = Mock(return_value=[])
    checker._run_check('bugs', 'Баги', lambda: checker._check_bugs('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    body = checker._release_comment('REL-1', True)
    assert '#14892c' in body and 'Баги не найдены' not in body


def test_job_1468_comment_has_numbered_facts_and_no_diagnostics():
    checker = stage_validator()
    checker._log_issue('REL-1', 'error',
        'Web release: Zephyr временно не отдал ТЦ по ключу релиза REL-1; '
        'проверка ТЦ пропущена, отсутствие ТЦ не зафиксировано как ошибка. '
        'Диагностика: issuelink/testruns status=404 found=0; '
        'testrun/search issueKey status=exception; search-name status=400')
    checker._log_issue('ABC-1', 'error', 'Отсутствует тестовое покрытие (нет прилинкованных ТК)')
    checker._log_issue('ABC-1', 'error', 'Zephyr: задача есть в Jira, но нет прилинкованных ТК в пространстве HRPQA')
    checker._log_issue('ABC-1', 'error', 'Нет комментариев от тестировщика')
    checker._log_issue('ABC-2', 'error', 'Нет затреканного времени')
    body = checker._release_comment('REL-1', False)
    rows = [line for line in body.splitlines() if line.startswith('| ')]
    assert len(rows) == 3 and '[REL-1|' in rows[0]
    assert '*1.* Не удалось проверить тест-циклы в Zephyr.' in rows[0]
    assert '*1.* Нет связанных тест-кейсов.' in rows[1]
    assert '*2.* Нет комментариев от тестировщика' in rows[1]
    assert '*1.* Нет затреканного времени' in rows[2]
    assert body.count('Нет связанных тест-кейсов.') == 1
    for forbidden in ('Диагностика', 'status=', 'exception', 'testruns', '{color:'):
        assert forbidden not in body
    # Detailed evidence is still available to the console report.
    assert 'status=404' in checker.report_data['REL-1']['errors'][0]


def test_cycle_index_uses_supported_query_and_finds_links_on_server_capped_pages(zephyr):
    pages = [
        [{'key': 'HRPQA-C1', 'name': 'REL-10 regression'}],
        [{'key': 'HRPQA-C2', 'name': 'Regression', 'issueLinks': [{'issueKey': 'REL-1'}]}],
        [{'key': 'HRPQA-C3', 'name': 'Other', 'issueKey': 'REL-1'}],
        [],
    ]
    def fetch(url, **kwargs):
        if '/issuelink/' in url:
            return response(404)
        params = kwargs['params']
        assert params['query'] == 'projectKey = "HRPQA"'
        assert params['fields'] == 'key,name,issueKey,issueLinks'
        assert 'issueKey' not in params and params['maxResults'] == rc.ZEPHYR_CYCLE_INDEX_PAGE_SIZE
        return response(200, pages[params['startAt']])
    zephyr.session.get.side_effect = fetch
    found = zephyr.get_test_cycles_for_issue('REL-1')
    assert [c['key'] for c in found] == ['HRPQA-C2', 'HRPQA-C3']
    assert zephyr.last_test_cycle_search_stats[-1]['complete'] is True
    assert zephyr.session.get.call_count == 5


@pytest.mark.parametrize('payload', [
    {'results': [], 'total': 10}, {'results': [], 'isLast': False},
    {'unknown': []}, [{'name': 'REL-1 without a key'}],
])
def test_invalid_cycle_index_is_not_proof_of_absence(zephyr, payload):
    zephyr.session.get.return_value = response(200, payload)
    assert zephyr._search_test_cycles_by_name('HRPQA', 'REL-1') == []
    assert zephyr.last_test_cycle_search_stats[-1]['complete'] is False
    assert zephyr.last_test_cycle_search_stats[-1]['status'] == 'exception'


@pytest.mark.parametrize('termination', ['repeat', 'timeout'])
def test_partial_cycle_index_keeps_matches_but_marks_check_incomplete(zephyr, termination):
    zephyr.get_test_cycles_from_frontend = Mock(return_value=([], False))
    checker = zephyr_checker(zephyr)
    checker._zephyr_release_cycles_cache = {}
    page = [{'key': 'HRPQA-C1', 'name': 'REL-1 regression'}]
    zephyr._get_test_cycles_by_issue_link = Mock(return_value=[])
    zephyr._get_with_retries = Mock(side_effect=[response(200, page),
        response(200, page) if termination == 'repeat' else requests.Timeout()])
    result = []
    checker._run_check('web', 'ТЦ', lambda: result.extend(checker._get_release_test_cycles('REL-1')))
    assert result == [('HRPQA-C1', 'REL-1 regression')]
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    assert 'REL-1' not in checker._zephyr_release_cycles_cache
    assert 'Не удалось проверить все тест-циклы релиза.' in checker._release_comment('REL-1', False)


def test_cycle_index_obeys_shared_budget(zephyr, fake_clock):
    def fetch(url, **kwargs):
        fake_clock[0] += kwargs['timeout'].total
        raise requests.Timeout()
    zephyr.session.get.side_effect = fetch
    assert zephyr._search_test_cycles_by_name('HRPQA', 'REL-1') == []
    assert fake_clock[0] <= 30
    assert zephyr.session.get.call_count <= 3


def test_cycle_index_retries_transient_errors(zephyr):
    cycle = {'key': 'HRPQA-C1', 'name': 'REL-1 regression'}
    zephyr.session.get.side_effect = [requests.Timeout(),
        response(200, {'results': [cycle], 'total': 1})]
    assert zephyr._search_test_cycles_by_name('HRPQA', 'REL-1') == [cycle]
    assert zephyr.last_test_cycle_search_stats[-1]['complete'] is True
    assert zephyr.session.get.call_count == 2


def test_direct_scan_checks_known_cycles_before_slow_forward_reads(zephyr, fake_clock, monkeypatch):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_cache = {'projects': {}, 'release_map': {}}
    checker._save_zephyr_cycle_cache = Mock()
    monkeypatch.setattr(rc, 'ZEPHYR_DIRECT_CYCLE_SCAN_START', '100')
    monkeypatch.setattr(rc, 'ZEPHYR_DIRECT_CYCLE_SCAN_LIMIT', 1)
    def fetch(key):
        fake_clock[0] += 1 if key == 'HRPQA-C100' else 10
        return {'key': key, 'name': 'REL-1 regression'} if key == 'HRPQA-C100' else None
    zephyr.get_test_cycle_details = Mock(side_effect=fetch)
    found = checker._get_test_cycles_from_direct_cache_or_scan('REL-1')
    assert [c['key'] for c in found] == ['HRPQA-C100']
    assert zephyr.get_test_cycle_details.call_args_list[0].args == ('HRPQA-C100',)


def test_direct_scan_progress_and_new_issue_links_survive_cache(zephyr, monkeypatch, tmp_path):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_cache = {'projects': {}, 'release_map': {}}
    monkeypatch.setattr(rc, 'ZEPHYR_CYCLE_CACHE_PATH', tmp_path / 'cache' / 'cycles.json')
    monkeypatch.setattr(rc, 'ZEPHYR_DIRECT_CYCLE_SCAN_START', '100')
    cycle = {'key': 'HRPQA-C110', 'name': 'Regression', 'issueLinks': [{'issueKey': 'REL-1'}]}
    checker._cache_cycle_details('HRPQA', cycle)
    checker._save_zephyr_cycle_cache()
    checker._zephyr_cycle_cache = checker._load_zephyr_cycle_cache()
    assert checker._get_direct_scan_high_watermark('HRPQA') == 110
    assert checker._get_cached_test_cycles_for_release('REL-1') == [cycle]
    zephyr.session.get.assert_not_called()


def test_slow_pr_endpoint_leaves_time_for_repository(validator, fake_clock):
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return response(404)
        if url.endswith('/remotelink'):
            return response(200, [])
        if kwargs['params']['dataType'] == 'pullrequest':
            fake_clock[0] += kwargs['timeout'].total
            raise requests.ReadTimeout()
        assert kwargs['params']['dataType'] == 'repository'
        fake_clock[0] += 2
        return response(200, {'detail': [{'repositories': [{'pullRequests': [{'id': 42}]}]}]})
    validator.jira_http.get.side_effect = fetch
    evidence = validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert 'repository' in evidence
    assert fake_clock[0] <= 16


def test_four_second_summary_succeeds_within_existing_budget(validator, fake_clock):
    def fetch(url, **kwargs):
        assert kwargs['timeout'].total >= 4
        fake_clock[0] += 4
        return response(200, summary_payload(1))
    validator.jira_http.get.side_effect = fetch
    assert validator._find_issue_pull_request_only_evidence(NS(key='ABC-1', id='123'))
    assert fake_clock[0] == 4
    assert validator.jira_http.get.call_count == 1


def test_cycle_index_and_metadata_timeouts_leave_direct_fallback_time(zephyr, fake_clock):
    checker = zephyr_checker(zephyr)
    checker._zephyr_release_cycles_cache = {}
    checker._zephyr_cycle_details_cache = {}
    checker.jira_http = Mock()
    cycle = {'key': 'HRPQA-C1', 'name': 'REL-1 regression'}
    def fetch(url, **kwargs):
        if '/issuelink/' in url:
            return response(404)
        if url.endswith('/HRPQA-C1'):
            fake_clock[0] += 2
            return response(200, cycle)
        fake_clock[0] += kwargs['timeout'].total
        raise requests.ReadTimeout()
    zephyr.session.get.side_effect = fetch
    checker.jira_http.get.side_effect = fetch
    checker._get_test_cycles_from_direct_cache_or_scan = Mock(
        side_effect=lambda _: [zephyr.get_test_cycle_details('HRPQA-C1')])
    assert checker._get_release_test_cycles('REL-1') == [('HRPQA-C1', 'REL-1 regression')]
    assert fake_clock[0] == 43
    assert fake_clock[0] <= rc.ZEPHYR_LOOKUP_TIMEOUT_SECONDS
    assert checker.jira_http.get.call_count == 1


def test_cycle_link_retries_without_increasing_its_budget(zephyr, fake_clock):
    cycle = {'key': 'HRPQA-C1', 'name': 'REL-1 regression'}
    zephyr.session.get.side_effect = [requests.ConnectionError(), response(200, [cycle])]
    assert zephyr._get_test_cycles_by_issue_link('REL-1') == [cycle]
    assert fake_clock[0] == .5
    assert zephyr.session.get.call_count == 2
    zephyr.session.get.reset_mock()
    def slow(url, **kwargs):
        fake_clock[0] += kwargs['timeout'].total
        raise requests.Timeout()
    zephyr.session.get.side_effect = slow
    assert zephyr._get_test_cycles_by_issue_link('REL-1') == []
    assert fake_clock[0] == 10.5
    assert zephyr.session.get.call_count == 1


def test_unattempted_pr_source_does_not_poison_next_lookup(validator, fake_clock):
    url = 'https://jira.example/rest/dev-status/latest/issue/detail'
    with validator._pr_lookup_budget(0):
        assert validator._pr_get_json(url) is None
    validator.jira_http.get.return_value = response(200, {'detail': [{'pullRequests': [{'id': 42}]}]})
    assert validator._pr_get_json(url)['detail']
    assert validator.jira_http.get.call_count == 1


def test_structured_debug_records_retries_budget_and_safe_response_shape(monkeypatch, capsys, fake_clock):
    import json
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '1')
    session = Mock()
    session.get.side_effect = [requests.ReadTimeout('secret-exception'), response(200, {
        'results': [{'key': 'SECRET-1', 'name': 'secret-name', 'password': 'secret-value'}],
        'total': 1, 'private-secret-field': 'secret-value'})]
    rc.bounded_get(session, 'https://jira.example/path?token=secret-query', source='Zephyr',
                   attempts=3, timeout=3, deadline=10, validate=rc.validate_json_payload,
                   params={'token': 'secret-param', 'startAt': 0}, headers={'Authorization': 'secret-header'})
    output = capsys.readouterr().out
    events = [json.loads(line.removeprefix('[HTTP DEBUG] ')) for line in output.splitlines()]
    assert 'secret' not in output.lower()
    assert {e['result'] for e in events} >= {'request', 'retry', 'timeout', 200, 'json'}
    assert len({e['request_id'] for e in events}) == 1
    shape = next(e['shape'] for e in events if e['result'] == 'json')
    assert shape['known']['results']['count'] == 1
    assert shape['known']['total'] == 1
    assert events[-1]['remaining_s'] == 9.5


@pytest.mark.parametrize('enabled', [True, False])
def test_http_debug_switch_works_without_changing_jenkins_command(monkeypatch, capsys, enabled):
    monkeypatch.delenv('RELEASE_CHECKER_DEBUG_HTTP', raising=False)
    monkeypatch.setattr(rc, 'HTTP_DEBUG_ENABLED', enabled)
    session = Mock()
    session.get.return_value = response(200, {'results': [], 'total': 0})
    rc.bounded_get(session, 'https://jira.example/path', source='Zephyr', attempts=3,
                   timeout=3, deadline=rc.time.monotonic() + 10, validate=rc.validate_json_payload)
    output = capsys.readouterr().out
    assert ('[HTTP DEBUG]' in output) is enabled
    assert ('"shape"' in output) is enabled
    assert session.get.call_count == 1


@pytest.mark.parametrize('override,expected', [('0', False), ('1', True), ('false', False), ('true', True)])
def test_http_debug_environment_overrides_code_switch(monkeypatch, override, expected):
    monkeypatch.setattr(rc, 'HTTP_DEBUG_ENABLED', not expected)
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', override)
    assert rc.http_debug_enabled() is expected


def test_http_debug_off_disables_file_logging_too(monkeypatch, tmp_path):
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '0')
    path = tmp_path / 'disabled.jsonl'
    handlers_before = list(rc.http_debug_logger.handlers)
    try:
        rc.configure_http_debug(path)
        rc.trace_http('PR', 'https://jira.example/path', 200, rc.time.monotonic())
        assert path.read_text() == ''
    finally:
        for handler in list(rc.http_debug_logger.handlers):
            if handler not in handlers_before:
                rc.http_debug_logger.removeHandler(handler)
                handler.close()


def test_jsonl_file_and_dry_run_comment_preview(monkeypatch, tmp_path):
    import json
    path = tmp_path / 'http.jsonl'
    handlers_before = list(rc.http_debug_logger.handlers)
    try:
        rc.configure_http_debug(path)
        rc.trace_http('PR', 'https://jira.example/path', 200, rc.time.monotonic())
        assert json.loads(path.read_text())['result'] == 200
    finally:
        for handler in list(rc.http_debug_logger.handlers):
            if handler not in handlers_before:
                rc.http_debug_logger.removeHandler(handler)
                handler.close()
    checker = stage_validator()
    checker.dry_run = True
    checker.jira_main = Mock()
    preview = tmp_path / 'comment.wiki'
    monkeypatch.setenv('RELEASE_CHECKER_COMMENT_PREVIEW', str(preview))
    checker._manage_jira_comment('REL-1', True)
    assert preview.read_text() == checker._release_comment('REL-1', True)
    checker.jira_main.add_comment.assert_not_called()
    checker.jira_main.comments.assert_not_called()


def test_metadata_html_fallback_is_not_validated_as_json(zephyr):
    checker = zephyr_checker(zephyr)
    checker.jira_http = Mock()
    html_response = response(200)
    html_response.text = '<a>HRPQA-C1</a>'
    html_response.json = Mock(side_effect=ValueError('HTML'))
    checker.jira_http.get.return_value = html_response
    assert checker._collect_test_cycle_keys_from_issue_html('REL-1') == {'HRPQA-C1'}
    html_response.json.assert_not_called()


def test_comment_separates_violations_missing_data_and_advice():
    checker = stage_validator()
    checker._log_issue('ABC-1', 'error', 'Отсутствует тестовое покрытие (нет прилинкованных ТК)')
    checker._log_issue('ABC-2', 'error', 'Zephyr: не удалось получить ТК (timeout)')
    checker._log_issue('ABC-3', 'warning', 'Pull Request не найден.')
    body = checker._release_comment('REL-1', False)
    assert '*Исправить: 1*' in body and '*Нет данных: 1*' in body and '*Проверить: 1*' in body
    rows = [line for line in body.splitlines() if line.startswith('| ')]
    assert '(x) Исправить' in rows[0] and 'Нет связанных тест-кейсов.' in rows[0]
    assert '(?) Нет данных' in rows[1] and 'Не удалось проверить' in rows[1]
    assert '(!) Проверить' in rows[2]


def test_legacy_object_comment_still_recognized():
    checker = stage_validator()
    checker.my_account_id = 'bot'
    checker.jira_main = Mock()
    old = Mock(author=NS(accountId='bot'), body=rc.LEGACY_COMMENT_TABLE_HEADER)
    checker.jira_main.comments.return_value = [old]
    checker._manage_jira_comment('REL-1', True)
    old.delete.assert_called_once()


def test_unwritable_cycle_cache_is_console_only(zephyr, monkeypatch, tmp_path, capsys):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_cache = {'projects': {}, 'release_map': {}}
    not_directory = tmp_path / 'file'
    not_directory.write_text('not a directory')
    monkeypatch.setattr(rc, 'ZEPHYR_CYCLE_CACHE_PATH', not_directory / 'cycles.json')
    checker._run_check('cache', 'Cache', checker._save_zephyr_cycle_cache)
    assert checker.check_results[0]['status'] == 'NO_FINDINGS'
    assert 'не удалось сохранить' in capsys.readouterr().out
    assert '#14892c' in checker._release_comment('REL-1', True)


@pytest.mark.parametrize('failure', ['comments', 'worklogs', 'issue', None])
def test_artifacts_checks_every_story_despite_independent_failures(validator, monkeypatch, failure):
    monkeypatch.setattr(rc, 'is_allowed_tester', lambda name: name == 'Tester')
    stories = {key: NS(key=key, fields=NS(issuetype=NS(name='Story'), issuelinks=[]))
               for key in ['ABC-1', 'ABC-2']}
    validator._get_consist_of_issues = Mock(return_value=list(stories))

    def issue(key, **kwargs):
        if failure == 'issue' and key == 'ABC-1':
            raise requests.Timeout('metadata')
        return stories[key]

    def comments(key):
        if failure == 'comments' and key == 'ABC-1':
            raise requests.Timeout('comments')
        return [NS(author=NS(displayName='Tester'))]

    def worklogs(key):
        if failure == 'worklogs' and key == 'ABC-1':
            raise requests.Timeout('worklogs')
        return [NS(author=NS(displayName='Tester'), timeSpentSeconds=60)]

    validator.jira_main.issue.side_effect = issue
    validator.jira_main.comments.side_effect = comments
    validator.jira_main.worklogs.side_effect = worklogs
    validator._check_artifacts('REL-1')
    assert [c.args[0] for c in validator.jira_main.comments.call_args_list] == list(stories)
    assert [c.args[0] for c in validator.jira_main.worklogs.call_args_list] == list(stories)
    messages = [c.args[2] for c in validator._log_issue.call_args_list]
    assert not any('Нет затреканного' in message for message in messages)
    if failure:
        assert any(c.args[1] == 'warning' for c in validator._log_issue.call_args_list)


def test_missing_comment_still_checks_time_without_vacuous_success(validator):
    story = NS(key='ABC-1', fields=NS(issuetype=NS(name='Story'), issuelinks=[]))
    validator._get_consist_of_issues = Mock(return_value=[story.key])
    validator.jira_main.issue.return_value = story
    validator.jira_main.comments.return_value = []
    validator.jira_main.worklogs.return_value = []
    validator._check_artifacts('REL-1')
    validator.jira_main.worklogs.assert_called_once_with(story.key)
    messages = [c.args[2] for c in validator._log_issue.call_args_list]
    assert 'Нет комментариев от тестировщика' in messages
    assert 'Нет затреканного времени от тестировщика' in messages
    assert not any(c.args[1] == 'success' for c in validator._log_issue.call_args_list)


def test_story_checks_continue_after_architecture_failure(validator):
    story = NS(key='HRC-1', fields=NS(description='', status=NS(name='Done'), issuelinks=[],
                                    customfield_24000='Да', customfield_18400='Нет'))
    validator._get_consist_of_issues = Mock(return_value=[story.key])
    validator.jira_main.search_issues.return_value = [story]
    validator.jira_main.issue.return_value = NS(changelog=NS(histories=[]))
    validator._check_story_architecture_subtask = Mock(side_effect=requests.Timeout())
    validator._check_required_platform_label = Mock()
    validator._check_story_requirements_block = Mock(return_value=False)
    validator._check_story_requirements_timing = Mock(side_effect=requests.Timeout())
    validator._calc_status_days = Mock(return_value=0)
    validator._check_stories('REL-1')
    validator._check_required_platform_label.assert_called_once_with(story, 'Story')
    validator._check_story_requirements_block.assert_called_once_with(story)
    validator._calc_status_days.assert_called_once()
    assert any('отсутствует описание' in c.args[2] for c in validator._log_issue.call_args_list)


@pytest.mark.parametrize('actual,expected_level', [('Новый функционал', 'success'), ('НФ', 'error')])
def test_cycle_testing_type_requires_full_field_value(validator, actual, expected_level):
    validator._zephyr_cycle_testing_type_checked = set()
    validator._get_test_cycle_details_cached = Mock(return_value={'key': 'HRPQA-R1'})
    validator.zephyr = Mock()
    validator.zephyr.get_test_case_custom_field.return_value = actual
    validator._check_test_cycle_testing_type('REL-1', 'HRPQA-R1', 'НФ release', rc.ZEPHYR_TESTING_TYPE_NEW)
    assert validator._log_issue.call_args.args[1] == expected_level


@pytest.mark.parametrize('actual,expected', [('Новый функционал', 'PASS'), ('НФ', 'FAIL')])
def test_new_functionality_test_case_uses_full_testing_type(zephyr, actual, expected):
    checker = zephyr_checker(zephyr)
    checker._build_expected_testing_type_map.return_value = {'ABC-1': rc.ZEPHYR_TESTING_TYPE_NEW}
    case = case_payload(customFields={'Вид тестирования': actual})
    zephyr.session.get.side_effect = lambda url, **kw: response(200, [case] if url.endswith('/testcases') else case)
    checker._run_check('cases', 'ТК', lambda: checker._check_zephyr_test_cases('REL-1'))
    assert checker.check_results[0]['status'] == expected


def test_story_worklog_failure_still_reads_linked_testing_task(validator, monkeypatch):
    monkeypatch.setattr(rc, 'is_allowed_tester', lambda name: name == 'Tester')
    story = NS(key='ABC-1', fields=NS(issuetype=NS(name='Story'),
               issuelinks=[NS(outwardIssue=NS(key='ABC-2'))]))
    task = NS(key='ABC-2', fields=NS(issuetype=NS(name='Task'), summary='Тестирование'))
    validator._get_consist_of_issues = Mock(return_value=[story.key])
    validator.jira_main.issue.side_effect = lambda key, **kw: story if key == story.key else task
    validator.jira_main.comments.return_value = [NS(author=NS(displayName='Tester'))]
    validator.jira_main.worklogs.side_effect = [requests.Timeout(), [NS(author=NS(displayName='Tester'), timeSpentSeconds=60)]]
    validator._check_artifacts('REL-1')
    assert [c.args[0] for c in validator.jira_main.worklogs.call_args_list] == ['ABC-1', 'ABC-2']
    assert not any(c.args[1] == 'error' for c in validator._log_issue.call_args_list)
    assert any('есть списанное время' in c.args[2] for c in validator._log_issue.call_args_list)


def test_cycle_batches_check_small_cycles_before_large_regression(zephyr):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_details_cache = {
        'HRPQA-C1': {'items': [{'testCaseKey': 'HRPQA-T1'}] * 200},
        'HRPQA-C2': {'items': [{'testCaseKey': 'HRPQA-T2'}]},
    }
    checker._check_test_cycle_cases_approved = Mock()
    checker._check_cycle_case_batches('REL-1', [('HRPQA-C1', 'Регресс'), ('HRPQA-C2', 'НФ'), ('HRPQA-C1', 'Регресс')])
    assert [c.args[1] for c in checker._check_test_cycle_cases_approved.call_args_list] == ['HRPQA-C2', 'HRPQA-C1']


def test_web_checks_all_cycle_metadata_before_case_reads(validator):
    validator._collect_release_service_infos = Mock(return_value={'service': {
        'display': 'Service', 'issue_keys': {'ABC-1'}, 'has_nf_story': True, 'project_keys': {'abc'}}})
    validator._get_release_test_cycles = Mock(return_value=[('HRPQA-C1', 'cycle')])
    validator._find_matching_cycle = Mock(return_value=('HRPQA-C1', 'cycle'))
    events = []
    validator._check_test_cycle_testing_type = Mock(side_effect=lambda *args: events.append('metadata'))
    validator._check_cycle_case_batches = Mock(side_effect=lambda *args: events.append('cases'))
    validator._check_web_release_service_test_cycles('REL-1')
    assert events == ['metadata'] * 5 + ['cases']
    assert len(validator._check_cycle_case_batches.call_args.args[1]) == 5


def test_long_unresolved_cycle_list_is_only_in_console(zephyr, capsys):
    checker = zephyr_checker(zephyr)
    keys = [f'HRPQA-T{i:04}' for i in range(160)]
    checker._get_test_cycle_details_cached = Mock(return_value={'items': [{'testCaseKey': key} for key in keys]})
    checker._get_test_case_details_cached = Mock(return_value=None)
    checker._run_check('cycle', 'ТЦ', lambda: checker._check_test_cycle_cases_approved('REL-1', 'HRPQA-C1', 'Регресс'))
    message = checker.report_data['REL-1']['errors'][0]
    assert 'ещё 150' in message
    assert keys[-1] not in message
    assert keys[-1] in capsys.readouterr().out
    assert checker.check_results[0]['status'] == 'INCOMPLETE'


def test_paused_zephyr_detail_reads_use_cache_without_http(zephyr, fake_clock):
    checker = zephyr_checker(zephyr)
    checker._zephyr_cycle_details_cache = {'HRPQA-C1': {'key': 'HRPQA-C1'}}
    checker._zephyr_test_case_details_cache = {'HRPQA-T1': case_payload()}
    zephyr.retry_state['until'] = fake_clock[0] + 120
    assert checker._get_test_cycle_details_cached('HRPQA-C1')
    assert checker._get_test_case_details_cached('HRPQA-T1')
    for i in range(2, 162):
        assert checker._get_test_case_details_cached(f'HRPQA-T{i}') is None
        assert checker._get_test_cycle_details_cached(f'HRPQA-C{i}') is None
    zephyr.session.get.assert_not_called()
    fake_clock[0] += 120
    zephyr.session.get.return_value = response(200, case_payload('HRPQA-T2'))
    assert checker._get_test_case_details_cached('HRPQA-T2')


def test_testing_time_deviation_is_visible_and_nonblocking():
    checker = stage_validator()
    message = 'Story: суммарное время в тест-статусах (7.05 д.) больше 115% норматива (6.9 д. из 6.0 д.)'
    checker._run_check('stories', 'Story', lambda: checker._log_issue('ABC-1', 'warning', message))
    assert not checker.report_data['ABC-1']['errors']
    comment = checker._release_comment('REL-1', True)
    assert '(x) Исправить' not in comment
    assert '(!) Отклонение' in comment
    assert '*Отклонения: 1*' in comment
    assert '#14892c' in comment
    assert 'Отклонение —' in checker._checks_wiki_table()


def matrix_response(status=200, payload=None, headers=None):
    result = response(status, payload, headers or {'Content-Type': 'application/json'})
    result.content = b'{}'
    return result


def matrix_session():
    session = Mock()
    session.headers = {}
    def fetch(url, **kwargs):
        if url.endswith('/issue/REL-1'):
            return matrix_response(payload={'fields': {'issuelinks': [
                {'type': {'name': 'consists of'}, 'outwardIssue': {'key': 'ABC-1'}}]}})
        if url.endswith('/issue/ABC-1'):
            return matrix_response(payload={'id': '123', 'key': 'ABC-1'})
        if url.endswith('/testrun/search'):
            return matrix_response(payload=[{'key': 'HRPQA-C1', 'name': 'REL-1.api.НФ'},
                                            {'key': 'HRPQA-C2', 'name': 'REL-10.api.НФ'}])
        if '/dev-status/' in url:
            return matrix_response(payload={'detail': [{'pullRequests': [{'id': '42'}]}]})
        return matrix_response(payload=[])
    session.get.side_effect = fetch
    return session


def test_lookup_matrix_compares_variants_without_writes(monkeypatch, capsys, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_FOCUS', 'both')
    monkeypatch.delenv('RELEASE_CHECKER_DIAG_ISSUE', raising=False)
    session = matrix_session()
    records = rc._diag_lookup_matrix('REL-1', session)
    assert len([r for r in records if r['method'].startswith('PR detail')]) == 18
    assert all(r['result'] == 'PR_EVIDENCE' for r in records if r['method'].startswith('PR detail'))
    baseline = next(r for r in records if r['method'] == 'cycle index size=1000 offset=0')
    control = next(r for r in records if r['method'] == 'cycle index size=5000 offset=0')
    assert baseline['release_matches'] == 1
    assert baseline['unrelated'] == 1
    assert baseline['keys_fingerprint'] == control['keys_fingerprint']
    assert 'mixed response' in baseline['filter_note']
    session.post.assert_not_called()
    session.put.assert_not_called()
    session.delete.assert_not_called()
    assert '[LOOKUP MATRIX SUMMARY]' in capsys.readouterr().out


def test_lookup_matrix_honors_shared_retry_after_without_waiting(monkeypatch, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_ISSUE', 'ABC-1')
    session = matrix_session()
    session.get.side_effect = lambda *args, **kwargs: matrix_response(429, headers={'Retry-After': '120'})
    records = rc._diag_lookup_matrix('REL-1', session)
    assert session.get.call_count == 1
    assert all(r['reason'] == 'Retry-After' for r in records[1:])
    assert fake_clock[0] == 0


def test_lookup_matrix_bounds_total_time_and_attempts(monkeypatch, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_ISSUE', 'ABC-1')
    session = matrix_session()
    def timeout(url, **kwargs):
        fake_clock[0] += kwargs['timeout'].total
        raise requests.Timeout()
    session.get.side_effect = timeout
    records = rc._diag_lookup_matrix('REL-1', session)
    assert fake_clock[0] <= 130
    assert session.get.call_count < 20
    assert all(r['result'] in {'EXCEPTION', 'SKIP'} for r in records)


def test_lookup_matrix_does_not_log_raw_service_errors(monkeypatch, capsys, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_ISSUE', 'ABC-1')
    session = matrix_session()
    session.get.side_effect = lambda *args, **kw: matrix_response(payload={
        'errors': [{'message': 'Connection timeout token=SUPER_SECRET', 'url': 'https://private/internal'}]})
    records = rc._diag_lookup_matrix('REL-1', session)
    assert records[0]['result'] == 'API_ERROR'
    assert 'timeout' in records[0]['error_categories']
    output = capsys.readouterr().out
    assert 'SUPER_SECRET' not in output
    assert 'https://private' not in output


@pytest.mark.parametrize('focus,excluded', [('cycles', 'PR detail'), ('pr', 'cycle ')])
def test_lookup_matrix_can_isolate_each_service(monkeypatch, focus, excluded, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_FOCUS', focus)
    records = rc._diag_lookup_matrix('REL-1', matrix_session())
    assert not any(r['method'].startswith(excluded) for r in records)


@pytest.mark.parametrize('retry_after', [None, '3'])
def test_matrix_short_throttle_does_not_skip_remaining_methods(monkeypatch, fake_clock, retry_after):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_CYCLE', 'HRPQA-C1')
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_FOCUS', 'both')
    session = matrix_session()
    fetch = session.get.side_effect
    calls = []
    throttled_at = []
    def get(url, **kwargs):
        calls.append(fake_clock[0])
        if len(calls) == 11:
            throttled_at.append(fake_clock[0])
            return matrix_response(429, headers={'Retry-After': retry_after} if retry_after else {})
        return fetch(url, **kwargs)
    session.get.side_effect = get
    records = rc._diag_lookup_matrix('REL-1', session)
    assert len(throttled_at) == 1
    assert all(b-a >= 0.5 for a, b in zip(calls, calls[1:]))
    assert calls[11]-calls[10] >= (3 if retry_after else 2)
    assert all(r['result'] != 'SKIP' for r in records)
    assert any(r['method'] == 'cycle direct key' and r['http'] == 200 for r in records)
    assert fake_clock[0] < 60


def test_matrix_repeated_throttles_remain_bounded(monkeypatch, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_FOCUS', 'cycles')
    session = matrix_session()
    session.get.side_effect = lambda *a, **kw: matrix_response(429, headers={})
    records = rc._diag_lookup_matrix('REL-1', session)
    assert session.get.call_count == len(records)
    assert fake_clock[0] < 60
    assert all(r['result'] == 'HTTP_ERROR' for r in records)


@pytest.mark.parametrize('cycle_release,expected', [('REL-1', 'RELEASE_MATCHES'), ('REL-10', 'NO_RELEASE_MATCHES')])
def test_matrix_direct_cycle_does_not_treat_executions_as_cycles(monkeypatch, fake_clock, cycle_release, expected):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_FOCUS', 'cycles')
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_CYCLE', 'HRPQA-C1')
    session = matrix_session()
    fetch = session.get.side_effect
    def get(url, **kwargs):
        if url.endswith('/testrun/HRPQA-C1'):
            return matrix_response(payload={'key': 'HRPQA-C1', 'name': cycle_release + '.api.Регресс',
                'issueKey': cycle_release, 'items': [{'testCaseKey': 'HRPQA-T1', 'comment': 'REL-1'}] * 183})
        return fetch(url, **kwargs)
    session.get.side_effect = get
    records = rc._diag_lookup_matrix('REL-1', session)
    direct = next(r for r in records if r['method'] == 'cycle direct key')
    assert direct['result'] == expected
    assert direct['count'] == 1
    assert direct['execution_count'] == 183
    assert direct['matching_keys'] == (['HRPQA-C1'] if cycle_release == 'REL-1' else [])


def test_matrix_discovers_cycle_keys_from_metadata_and_properties(monkeypatch, fake_clock):
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_FOCUS', 'cycles')
    monkeypatch.setenv('RELEASE_CHECKER_DIAG_CYCLE', '')
    session = matrix_session()
    fetch = session.get.side_effect
    def get(url, **kwargs):
        if url.endswith('/remotelink'):
            return matrix_response(payload=[{'object': {'url': 'https://jira.example/HRPQA-C2'}}])
        if url.endswith('/properties'):
            return matrix_response(payload={'keys': [{'key': 'zephyr-cycles'}]})
        if url.endswith('/properties/zephyr-cycles'):
            return matrix_response(payload={'value': ['HRPQA-C3']})
        if '/testrun/HRPQA-C' in url:
            return matrix_response(payload={'key': url.rsplit('/', 1)[1], 'issueKey': 'REL-1', 'items': []})
        return fetch(url, **kwargs)
    session.get.side_effect = get
    records = rc._diag_lookup_matrix('REL-1', session)
    hints = next(r for r in records if r['method'] == 'cycle remote links')
    assert hints['cycle_keys'] == ['HRPQA-C2']
    direct = [r for r in records if r.get('cycle_key')]
    assert {r['cycle_key'] for r in direct} == {'HRPQA-C2', 'HRPQA-C3'}
    assert all(r['result'] == 'RELEASE_MATCHES' for r in direct)


def test_compact_cycle_index_handles_server_page_cap(fake_clock):
    rows = [{'key': f'HRPQA-C{i}', 'name': ('REL-1' if i == 3 else 'REL-10') + '.api.НФ'} for i in range(5)]
    offsets = []
    def probe(name, path, params, **kwargs):
        offsets.append(params['startAt'])
        assert params['fields'] == 'key,name,issueKey,issueLinks'
        return rows[params['startAt']:params['startAt']+2]
    result = rc._diag_cycle_index(probe, 'REL-1', 45)
    assert offsets == [0, 0, 2, 4, 5]
    assert result['complete']
    assert result['unique_cycles'] == 5
    assert result['matches'] == ['HRPQA-C3']


def test_compact_index_rejects_ignored_pagination(fake_clock):
    probe = Mock(return_value=[{'key': 'HRPQA-C1', 'name': 'REL-1.api.НФ'}])
    result = rc._diag_cycle_index(probe, 'REL-1', 45)
    assert not result['complete']
    assert result['reason'] == 'pagination_not_advancing'
    assert probe.call_count == 3


def test_compact_index_keeps_matches_when_later_request_fails(fake_clock):
    probe = Mock(side_effect=[
        [{'key': 'HRPQA-C1', 'name': 'REL-1.api.НФ'}],
        [{'key': 'HRPQA-C1', 'name': 'REL-1.api.НФ'}, {'key': 'HRPQA-C2', 'name': 'REL-2.api.НФ'}],
        None])
    result = rc._diag_cycle_index(probe, 'REL-1', 45)
    assert not result['complete']
    assert result['matches'] == ['HRPQA-C1']
    assert result['reason'] == 'request_failed_or_throttled'


def test_compact_index_stops_at_budget(fake_clock):
    def probe(name, path, params, **kwargs):
        fake_clock[0] += min(12, kwargs['deadline'] - fake_clock[0])
        return [{'key': f"HRPQA-C{params['startAt']+1}", 'name': 'REL-1.api.НФ'}]
    result = rc._diag_cycle_index(probe, 'REL-1', 45)
    assert not result['complete']
    assert result['reason'] == 'budget'
    assert result['pages'] == 4


def test_production_index_replays_1570_within_discovery_budget(zephyr, fake_clock, monkeypatch):
    monkeypatch.setattr(rc, 'ZEPHYR_CYCLE_INDEX_PAGE_SIZE', 5000)
    offsets = []
    timings = {0: 7.537, 5000: 8.007, 10000: 5.991, 15000: 0.553, 20000: 0.424, 23059: 0.083}
    def fetch(url, **kwargs):
        if '/issuelink/' in url:
            return response(404)
        params = kwargs['params']
        offset = params['startAt']
        offsets.append(offset)
        assert params['maxResults'] == 5000
        assert params['fields'] == 'key,name,issueKey,issueLinks'
        assert kwargs['timeout'].total >= timings[offset]
        fake_clock[0] += timings[offset]
        return response(200, [{'key': f'HRPQA-C{i+1}',
            'name': ('REL-1' if i < 8 else 'REL-10') + '.api.НФ'}
            for i in range(offset, min(offset+5000, 23059))])
    zephyr.session.get.side_effect = fetch
    with zephyr.lookup_budget(rc.ZEPHYR_CYCLE_DISCOVERY_SECONDS):
        cycles = zephyr.get_test_cycles_for_issue('REL-1')
    assert len(cycles) == 8
    assert offsets == [0, 5000, 10000, 15000, 20000, 23059]
    assert fake_clock[0] < rc.ZEPHYR_CYCLE_DISCOVERY_SECONDS
    stats = zephyr.last_test_cycle_search_stats[-1]
    assert stats['complete'] is True
    assert stats['total_seen'] == 23059
    assert stats['pages'] == 6


USER_CYCLE_SERVICES = [
    ('CoreUI(2298599)', ('Safari', 'WEB', 'PWA', 'iPad')),
    ('HRP.CoreUI-Dates(3304476)', ('WEB', 'PWA', 'iPad', 'Safari')),
    ('HRP.CoreUI-Posts(3304476)', ('Safari', 'WEB', 'PWA', 'iPad')),
    ('HRP-Admin(3589425)', ('Safari', 'iPad', 'PWA', 'WEB')),
    ('HRP.CoreUI-Support(5452083)', ('WEB', 'PWA', 'iPad', 'Safari')),
    ('HRP.Launchpad(5452082)', ('iPad', 'Safari', 'PWA', 'WEB')),
    ('HRP.CoreUI-Configurator(5452084)', ('WEB', 'API')),
    ('NeuroSearchBar(9643401)', ('iPad', 'SberBrowser', 'Safari')),
    ('HRP.CoreUI-Neuro(9644020)', ('SberBrowser', 'Safari', 'iPad')),
]


@pytest.mark.parametrize('service,channel', [(service, channel) for service, channels in USER_CYCLE_SERVICES for channel in channels])
@pytest.mark.parametrize('prefix', ['', 'CI01234567 '])
def test_user_cycle_names_match_service_and_channel(validator, service, channel, prefix):
    aliases = rc.BACK_API_CHANNEL_ALIASES if channel == 'API' else next(
        aliases for _, aliases in rc.WEB_REGRESS_CHANNELS if channel.casefold() in aliases)
    name = f' HRPRELEASE-148167. {service}. {channel}. Regress '
    assert validator._find_matching_cycle([('HRPQA-C1', name)], 'HRPRELEASE-148167',
                                          prefix+service, aliases, rc.BACK_REGRESS_CYCLE_ALIASES)
    russian = name.replace('Regress', 'Регресс').replace('148167', '147712')
    assert validator._find_matching_cycle([('HRPQA-C2', russian)], 'HRPRELEASE-147712',
                                          prefix+service, aliases, rc.BACK_REGRESS_CYCLE_ALIASES)
    assert not validator._find_matching_cycle([('HRPQA-C1', name)], 'HRPRELEASE-148168',
                                              prefix+service, aliases, rc.BACK_REGRESS_CYCLE_ALIASES)


@pytest.mark.parametrize('left,right,expected', [
    ('CI01234567 HRP.CoreUI-Dates', 'HRP. CoreUI-Dates(3304476)', True),
    ('HRP.CoreUI-Dates(3304476)', 'HRP.CoreUI-Posts(3304476)', False),
    ('CoreUI(1)', 'CoreUI(2)', False),
    ('CI01234567 CoreUI', 'CI07654321 CoreUI', False),
    ('CI01234567 CoreUI', 'CI01234567 CoreUI renamed', True),
    ('CoreUI', 'CoreUI-Dates', False),
    ('', '', False),
])
def test_service_identity_does_not_confuse_names_or_ids(left, right, expected):
    assert rc.service_ke_matches(left, right) is expected


def service_issue(key, services, labels=None):
    return NS(key=key, fields=NS(summary=key, assignee=None, labels=labels or [], customfield_ke=services))


def setup_service_check(validator, release_services, task_services):
    release = service_issue('REL-1', release_services)
    tasks = [service_issue(f'ABC-{i+1}', services) for i, services in enumerate(task_services)]
    validator._get_jira_field_id_by_names = Mock(return_value='customfield_ke')
    validator._get_consist_of_issues = Mock(return_value=[task.key for task in tasks])
    validator.jira_main.search_issues.return_value = tasks
    return release, tasks


def test_service_check_all_tasks_continue_after_mismatch_and_empty_field(validator):
    release, tasks = setup_service_check(validator, ['CI01234567 CoreUI', 'HRP.CoreUI-Dates(3304476)'],
        [['CoreUI(2298599)'], ['HRP.CoreUI-Posts(3304476)'], [], ['HRP.CoreUI-Dates(3304476)']])
    validator._check_release_service_consistency(release)
    assert [c.args[1] for c in validator._log_issue.call_args_list] == ['success', 'error', 'error', 'success']
    assert validator.jira_main.search_issues.call_count == 1
    assert validator.jira_main.search_issues.call_args.kwargs['maxResults'] is False


@pytest.mark.parametrize('mobile_service', [
    'CI02257858 HRP-ios', 'CI02935717 HRP-Android', 'CI03521872 HRP.iOS МП.МП-iPAD',
    'CI06355438 HRP. SberTab', 'HRP-ios', 'HRP-Android', 'HRP.iOS МП.МП-iPAD', 'HRP. SberTab',
])
def test_mobile_release_service_is_exempt(validator, mobile_service):
    release, _ = setup_service_check(validator, [mobile_service], [['Different']])
    validator._check_release_service_consistency(release)
    validator.jira_main.search_issues.assert_not_called()
    assert validator._log_issue.call_args.args[1] == 'success'
    assert 'не требуется' in validator._log_issue.call_args.args[2]


def test_mobile_label_exempts_without_field_lookup(validator):
    release = service_issue('REL-1', [], ['mobile'])
    validator._get_jira_field_id_by_names = Mock()
    validator._check_release_service_consistency(release)
    validator._get_jira_field_id_by_names.assert_not_called()


def test_multiple_task_services_must_all_be_in_release(validator):
    release, _ = setup_service_check(validator, ['CoreUI'], [['CoreUI', 'Other']])
    validator._check_release_service_consistency(release)
    assert validator._log_issue.call_args.args[1] == 'error'
    assert 'Other' in validator._log_issue.call_args.args[2]


def test_service_check_reports_unreturned_task(validator):
    release, tasks = setup_service_check(validator, ['CoreUI'], [['CoreUI'], ['CoreUI']])
    validator.jira_main.search_issues.return_value = tasks[:1]
    validator._check_release_service_consistency(release)
    assert validator._log_issue.call_args.args[0] == 'ABC-2'
    assert 'не удалось' in validator._log_issue.call_args.args[2]


def test_service_check_missing_metadata_is_clear_in_comment(validator):
    release, _ = setup_service_check(validator, ['CoreUI'], [['CoreUI']])
    validator._get_jira_field_id_by_names.return_value = None
    validator._check_release_service_consistency(release)
    message = validator._log_issue.call_args.args[2]
    assert rc.ReleaseValidator._comment_message(message) == 'Не удалось проверить совпадение КЭ задач и релиза.'
    validator.jira_main.search_issues.assert_not_called()


def test_service_mismatch_is_blocking_in_release_report():
    checker = stage_validator()
    checker.jira_main = Mock()
    release, _ = setup_service_check(checker, ['CoreUI'], [['Other']])
    checker._run_check('service_consistency', 'КЭ', lambda: checker._check_release_service_consistency(release))
    assert checker.check_results[0]['status'] == 'FAIL'
    assert checker.report_data['ABC-1']['errors']


def test_service_check_missing_release_value_blocks_comparison(validator):
    release, _ = setup_service_check(validator, [], [['CoreUI']])
    validator._check_release_service_consistency(release)
    assert validator._log_issue.call_args.args[1] == 'error'
    assert 'поле релиза не заполнено' in validator._log_issue.call_args.args[2]
    validator.jira_main.search_issues.assert_not_called()


def giga_summary(pr=0, repo=0, commit=0):
    return {'summary': {kind: {'overall': {'count': count}, 'byInstanceType': {'stash': {'count': count}}}
                        for kind, count in [('pullrequest', pr), ('repository', repo), ('commit', commit)]}}


def test_gigacode_all_zero_is_known_absence_without_detail(validator):
    validator._pr_get_json = Mock(return_value=giga_summary())
    result = validator._lookup_gigacode(NS(key='ABC-1', id='1'))
    assert result['state'] == 'NOT_FOUND'
    assert validator._pr_get_json.call_count == 1


@pytest.mark.parametrize('payload', [
    {'detail': []}, {'detail': [{'_instance': {'name': '#GigaCode Agent'}}]},
    {'detail': [{'pullRequests': [{'id': '1'}]}]},
    {'detail': [{'pullRequests': [{'id': '1', 'name': 'ordinary'}], 'isLast': False}]},
])
def test_gigacode_empty_metadata_and_partial_data_are_unknown(validator, payload):
    validator._pr_get_json = Mock(side_effect=lambda url, **kw: giga_summary(pr=1) if url.endswith('/summary') else payload)
    result = validator._lookup_gigacode(NS(key='ABC-1', id='1'))
    assert result['state'] == 'UNKNOWN'
    assert any(r['state'] == 'INCOMPLETE' for r in result['sources'])


def test_gigacode_visible_pr_text_without_marker_is_checked(validator):
    validator._pr_get_json = Mock(side_effect=lambda url, **kw: giga_summary(pr=1) if url.endswith('/summary') else
        {'detail': [{'pullRequests': [{'id': '1', 'name': 'normal PR', 'description': 'normal description'}]}]})
    result = validator._lookup_gigacode(NS(key='ABC-1', id='1'))
    assert result['state'] == 'NOT_FOUND'
    checked = next(row for row in result['sources'] if row['state'] == 'CHECKED')
    assert checked['text_fields']['description'] == 1


def test_gigacode_marker_survives_partial_response_and_is_cached(validator):
    validator._pr_get_json = Mock(side_effect=lambda url, **kw: giga_summary(pr=3) if url.endswith('/summary') else
        {'detail': [{'pullRequests': [{'description': '#GigaCode Agent'}], 'isLast': False}]})
    issue = NS(key='ABC-1', id='1')
    assert validator._lookup_gigacode(issue)['state'] == 'FOUND'
    validator._pr_get_json.reset_mock()
    validator._pr_retry_state = {'until': rc.time.monotonic()+120}
    assert validator._lookup_gigacode(issue)['state'] == 'FOUND'
    validator._pr_get_json.assert_not_called()


def test_gigacode_unknown_is_not_cached_as_absence(validator):
    validator._pr_get_json = Mock(side_effect=lambda url, **kw: giga_summary(pr=1) if url.endswith('/summary') else {'detail': []})
    issue = NS(key='ABC-1', id='1')
    assert validator._lookup_gigacode(issue)['state'] == 'UNKNOWN'
    validator._pr_get_json.side_effect = lambda url, **kw: giga_summary(pr=1) if url.endswith('/summary') else {
        'detail': [{'pullRequests': [{'name': '#GigaCode Agent'}]}]}
    assert validator._lookup_gigacode(issue)['state'] == 'FOUND'


def test_gigacode_log_explains_unknown_but_comment_keeps_only_existing_text(validator, capsys, monkeypatch):
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '1')
    secret = 'PRIVATE_PR_CONTENT'
    validator._pr_get_json = Mock(side_effect=lambda url, **kw: giga_summary(pr=2) if url.endswith('/summary') else {
        'detail': [{'pullRequests': [{'title': secret, 'description': secret}]}]})
    with pytest.raises(RuntimeError) as error:
        validator._issue_has_gigacode_pull_request(NS(key='ABC-1', id='1'))
    output = capsys.readouterr().out
    assert secret not in output
    assert 'INCOMPLETE' in output and 'expected_count' in output and 'text_fields' in output
    comment = validator._comment_message(f'AIFIXED: не удалось определить использование GigaCode: {error.value}')
    assert comment == 'Не удалось определить, использовался ли GigaCode при написании кода. Необходимость лейбла AIFIXED не установлена.'
    assert 'expected_count' not in comment


def test_api_application_error_categories_do_not_expose_messages():
    with pytest.raises(rc.DevelopmentAPIError) as error:
        rc.validate_json_payload({'errors': [{'message': 'connection timed out token=SECRET_VALUE'}]})
    assert 'upstream_timeout' in rc.request_failure_kind(error.value)
    assert 'SECRET_VALUE' not in str(error.value)


def test_gigacode_provider_zero_does_not_override_positive_total(validator):
    payload = giga_summary(pr=1)
    payload['summary']['pullrequest']['byInstanceType']['stash']['count'] = 0
    validator._pr_get_json = Mock(return_value=payload)
    assert validator._lookup_gigacode(NS(key='ABC-1', id='1'))['state'] == 'UNKNOWN'


def test_gigacode_timeouts_stay_within_issue_budget(validator, fake_clock):
    validator.jira_http.get.side_effect = requests.Timeout()
    with validator._pr_lookup_budget(12):
        result = validator._lookup_gigacode(NS(key='ABC-1', id='1'))
    assert result['state'] == 'UNKNOWN'
    assert fake_clock[0] <= 12


def test_repository_commit_objects_avoid_unsupported_commit_alias(validator):
    summary = giga_summary(pr=0, repo=1)
    del summary['summary']['commit']
    def fetch(url, **kwargs):
        if url.endswith('/summary'):
            return summary
        assert kwargs['params']['dataType'] == 'repository'
        return {'detail': [{'repositories': [{'commits': [{'message': 'ordinary commit'}]}]}]}
    validator._pr_get_json = Mock(side_effect=fetch)
    result = validator._lookup_gigacode(NS(key='ABC-1', id='1'))
    assert result['state'] == 'NOT_FOUND'
    assert any(row.get('reason') == 'commits_read_from_repository' for row in result['sources'])
    assert validator._pr_get_json.call_count == 2


def test_gigacode_task_evidence_resolves_previous_task_failure(validator):
    issue = NS(key='ABC-1', fields=NS(labels=[]))
    validator._get_release_pr_targets = Mock(return_value=[(issue, [NS(key='ABC-2'), NS(key='ABC-3')])])
    validator._issue_has_gigacode_pull_request = Mock(side_effect=[RuntimeError('unavailable'), '#GigaCode Agent'])
    validator._add_label_if_missing = Mock(return_value=True)
    validator._check_gigacode_aifixed_labels('REL-1')
    validator._add_label_if_missing.assert_called_once_with(issue, 'AIFIXED')
    assert all(call.args[1] == 'success' for call in validator._log_issue.call_args_list)


@pytest.mark.parametrize('bad', ['offset', 'total', 'repeat', 'shape'])
def test_frontend_rejects_incomplete_pages(zephyr, bad):
    item = {'key': 'HRPQA-C1', 'name': 'REL-1. WEB. Regress'}
    second = {'results': [dict(item, key='HRPQA-C2')], 'total': 2, 'startAt': 1}
    if bad == 'offset': second['startAt'] = 0
    if bad == 'total': second['total'] = 3
    if bad == 'repeat': second['results'] = [item]
    if bad == 'shape': second = {'children': []}
    zephyr.session.get.side_effect = [response(200, {'results': [item], 'total': 2, 'startAt': 0}), response(200, second)]
    matches, complete = zephyr.get_test_cycles_from_frontend('REL-1')
    assert not complete
    assert matches == [item]


def test_frontend_reads_capped_pages_and_exact_release_name(zephyr):
    rows = [{'key': 'HRPQA-C1', 'name': 'REL-10. WEB. Regress'},
            {'key': 'HRPQA-C2', 'name': 'REL-1. WEB. Regress'}]
    def fetch(url, **kwargs):
        assert url.endswith('/rest/tests/1.0/testrun/search')
        assert kwargs['headers'] == {'jira-project-id': '88701'}
        assert 'folderTreeId' not in kwargs['params']['query']
        offset = kwargs['params']['startAt']
        return response(200, {'results': rows[offset:offset+1], 'total': 2, 'startAt': offset})
    zephyr.session.get.side_effect = fetch
    assert zephyr.get_test_cycles_from_frontend('REL-1') == ([rows[1]], True)
    assert zephyr.session.get.call_count == 2


@pytest.mark.parametrize('primary_complete', [True, False])
def test_frontend_fallback_only_after_failed_primary(zephyr, primary_complete):
    checker = zephyr_checker(zephyr)
    checker._zephyr_release_cycles_cache = {}
    checker._get_test_cycles_from_jira_release_metadata = Mock(return_value=[])
    checker._get_test_cycles_from_direct_cache_or_scan = Mock(return_value=[])
    checker._log_test_cycle_search_debug = Mock()
    zephyr.get_test_cycles_for_issue = Mock(return_value=[])
    zephyr.last_test_cycle_search_stats = [{'mode': 'search', 'complete': primary_complete,
                                           'status': 200 if primary_complete else 'exception'}]
    row = {'key': 'HRPQA-C1', 'name': 'REL-1. WEB. Regress'}
    zephyr.session.get.return_value = response(200, {'results': [row], 'total': 1, 'startAt': 0})
    result = checker._get_release_test_cycles('REL-1')
    assert result == ([] if primary_complete else [(row['key'], row['name'])])
    assert zephyr.session.get.call_count == (0 if primary_complete else 1)
    assert checker._zephyr_release_cycles_cache['REL-1'] == result


def test_frontend_respects_cooldown(zephyr, fake_clock):
    zephyr.retry_state['until'] = fake_clock[0] + 60
    assert zephyr.get_test_cycles_from_frontend('REL-1') == ([], False)
    zephyr.session.get.assert_not_called()


@pytest.mark.parametrize('statuses,accepted', [([], False), (['В работе'], False),
    (['Установлен на ПРОМ'], True), (['В работе', 'Установлен на ПРОМ'], True), ([None], False)])
def test_unlinked_requires_installed_other_release(statuses, accepted):
    checker = stage_validator()
    checker.jira_main = Mock()
    links = [NS(outwardIssue=NS(key=f'REL-{i+2}')) for i in range(len(statuses))]
    links.append(NS(inwardIssue=NS(key='REL-1')))
    issue = NS(key='ABC-2', fields=NS(summary='Task', assignee=None, issuelinks=links))
    checker.jira_main.search_issues.return_value = [
        NS(key=f'REL-{i+2}', fields=NS(status=NS(name=status)))
        for i, status in enumerate(statuses) if status is not None]
    assert checker._check_unlinked_other_releases([issue], 'REL-1') == ({'ABC-2'} if accepted else set())
    assert bool(checker.report_data['ABC-2']['errors']) is not accepted


def test_installed_other_release_allows_merged_pr():
    checker = stage_validator()
    issue = NS(key='ABC-2', fields=NS(summary='Task', assignee=None,
        issuetype=NS(name='Story'), labels=[], issuelinks=[NS(inwardIssue=NS(key='REL-2'))]))
    checker._get_unlinked_release_issue_keys = Mock(return_value={'ABC-2'})
    checker._get_release_story_bug_keys = Mock(return_value=set())
    checker.jira_main = Mock()
    checker.jira_main.search_issues.side_effect = [[issue],
        [NS(key='REL-2', fields=NS(status=NS(name='Установлен на ПРОМ')))]]
    checker._find_issue_merged_pull_request_evidence = Mock(return_value='PR 42')
    checker._check_unlinked_release_items_pull_requests('REL-1')
    checker._find_issue_merged_pull_request_evidence.assert_called_once_with(issue)
    assert not checker.report_data['ABC-2']['errors']


@pytest.mark.parametrize('exemption', ['label', 'linked_story'])
def test_pr_exemptions_do_not_skip_other_release_requirement(exemption):
    checker = stage_validator()
    issue = NS(key='ABC-2', fields=NS(summary='Task', assignee=None,
        issuetype=NS(name='Story'), labels=['review_confirmed'] if exemption == 'label' else [],
        issuelinks=[NS(outwardIssue=NS(key='ABC-1'))] if exemption == 'linked_story' else []))
    checker._get_unlinked_release_issue_keys = Mock(return_value={'ABC-2'})
    checker._get_release_story_bug_keys = Mock(return_value={'ABC-1'})
    checker.jira_main = Mock()
    checker.jira_main.search_issues.return_value = [issue]
    checker._find_issue_merged_pull_request_evidence = Mock()
    checker._check_unlinked_release_items_pull_requests('REL-1')
    assert checker.report_data['ABC-2']['errors']
    checker._find_issue_merged_pull_request_evidence.assert_not_called()


def test_other_release_read_failure_blocks():
    checker = stage_validator()
    checker.jira_main = Mock()
    checker.jira_main.search_issues.side_effect = requests.Timeout()
    issue = NS(key='ABC-2', fields=NS(summary='Task', assignee=None,
        issuelinks=[NS(outwardIssue=NS(key='REL-2'))]))
    assert checker._check_unlinked_other_releases([issue], 'REL-1') == set()
    assert 'статус не удалось получить' in str(checker.report_data['ABC-2']['errors'])


@pytest.fixture
def native_sdd(monkeypatch):
    """Native SDD requests never access the network or sleep in tests."""
    checker = stage_validator()
    session = Mock()
    session.headers = {}
    monkeypatch.setattr(rc.requests, 'Session', lambda: nullcontext(session))
    monkeypatch.setattr(rc, 'config', {'jira': {'url': 'https://jira.example', 'token': 'test-token'}})
    monkeypatch.setattr(rc.time, 'sleep', Mock())
    monkeypatch.delenv('BITBUCKET_BASE_URL', raising=False)
    monkeypatch.delenv('BITBUCKET_API_TOKEN', raising=False)
    monkeypatch.setattr(rc, 'sdd_release_artifacts', Mock(return_value=False))
    checker._reset_release_run = Mock()
    checker.jira_main = Mock()
    return checker, session


def sdd_responses(labels=None, detail=None, *, issues=None, issue_id='42', repo_count=1):
    """One linked Story and its API responses in original request order."""
    data = [
        {'issues': [{'key': 'ST-1'}] if issues is None else issues},
        {'fields': {'labels': ['SDD'] if labels is None else labels}},
        {'id': issue_id},
        {'summary': {'repository': {'overall': {'count': repo_count}}}},
        {'detail': [{'repositories': [{'commits': [{'files': [{'path': 'openspec/ST-1/spec.md'}]}]}]}]}
        if detail is None else detail,
    ]
    return [response(200, item) for item in data]


@pytest.mark.parametrize('labels,path,expected,errors', [
    (['SDD'], 'openspec/ST-1/spec.md', 'PASS', 0),
    (['sDd'], 'OPENSPEC/st-1/spec.md', 'PASS', 0),
    (['#SDD'], 'openspec/ST-1/spec.md', 'FAIL', 1),
    ([' SDD '], 'openspec/ST-1/spec.md', 'FAIL', 1),
    ([], 'openspec/ST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'docs/openspec/ST-1/spec.md', 'PASS', 0),
    (['SDD'], 'openspec/ST-10/spec.md', 'PASS', 0),
    (['SDD'], 'openspec/team/ST-1/spec.md', 'PASS', 0),
    (['SDD'], 'docs/openspec/changes/feature-ST-1-new/spec.md', 'PASS', 0),
    (['SDD'], 'src/OPENSPEC/changes/feature-st-1-new.md', 'PASS', 0),
    (['SDD'], 'openspec/prefixST-1suffix.md', 'PASS', 0),
    (['SDD'], 'ST-1/openspec/other/spec.md', 'FAIL', 1),
    (['SDD'], 'docs/openspec-ST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'docs/spec/ST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'openspec/folder\nST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'openspec/folder\rST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'openspec/folder\u2028ST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'openspec/folder\u2029ST-1/spec.md', 'FAIL', 1),
    (['SDD'], 'openspec/ST-2/spec.md', 'FAIL', 1),
    ([], 'openspec/ST-2/spec.md', 'FAIL', 2),
])
def test_native_sdd_original_decision_rules(native_sdd, labels, path, expected, errors):
    checker, session = native_sdd
    detail = {'detail': [{'repositories': [{'commits': [{'files': [{'path': path}]}]}]}]}
    session.get.side_effect = sdd_responses(labels, detail)
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == expected
    assert len(checker.report_data['ST-1']['errors']) == errors
    assert session.headers == {'Authorization': 'Bearer test-token', 'Accept': 'application/json'}
    assert session.get.call_args_list[0].kwargs['params'] == {
        'jql': 'issue IN linkedIssues("REL-1") AND type = Story', 'maxResults': 200, 'fields': 'key'}
    for call in session.get.call_args_list:
        assert call.kwargs['timeout'] == (5, 30)
        assert 'verify' in call.kwargs
    assert rc.time.sleep.call_args_list == [((11,),)]


def test_native_sdd_has_no_linked_stories(native_sdd):
    checker, session = native_sdd
    session.get.side_effect = sdd_responses(issues=[])[:1]
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    assert session.get.call_count == 1
    assert rc.time.sleep.call_count == 0


@pytest.mark.parametrize('kind', ['timeout_errors', 'other_errors', 'http_error'])
def test_native_sdd_unknown_does_not_invent_missing_artifacts(native_sdd, kind):
    checker, session = native_sdd
    retry = response(503) if kind == 'http_error' else response(200, {
        'detail': [], 'errors': [{'error': 'timeout' if kind == 'timeout_errors' else 'other failure'}]})
    session.get.side_effect = sdd_responses(labels=[])[:4] + [retry] * 4
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'WARN'
    assert not checker.report_data['ST-1']['errors']
    assert 'не блокирует гейт' in checker.report_data['ST-1']['warnings'][0]
    assert rc.time.sleep.call_count == 4
    assert 'X-Requested-With' not in session.headers


@pytest.mark.parametrize('summary_response', [response(200, {}), response(403),
    response(200, {'summary': {'repository': {'overall': {'count': 0}}}})])
def test_native_sdd_summary_without_repositories_is_false(native_sdd, summary_response):
    checker, session = native_sdd
    session.get.side_effect = sdd_responses()[:3] + [summary_response]
    assert checker.check_release('REL-1') is False
    checker.jira_main.issue.assert_not_called()
    assert 'openspec/.*ST-1.*' in checker.report_data['ST-1']['errors'][0]
    assert session.get.call_count == 4
    assert rc.time.sleep.call_count == 0


def test_native_sdd_missing_issue_id_is_false(native_sdd):
    checker, session = native_sdd
    session.get.side_effect = sdd_responses(issue_id=None)[:3]
    assert checker.check_release('REL-1') is False
    assert 'openspec/.*ST-1.*' in checker.report_data['ST-1']['errors'][0]
    assert session.get.call_count == 3


def test_native_sdd_retry_success_and_other_instance_error(native_sdd):
    checker, session = native_sdd
    session.get.side_effect = sdd_responses()[:4] + [response(503),
        response(200, {'detail': [], 'errors': [{'error': 'timeout'}]}),
        response(200, {**sdd_responses()[-1].json(), 'errors': [{'error': 'other instance timeout'}]})]
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    assert rc.time.sleep.call_count == 3


@pytest.mark.parametrize('failure,detail', [
    (requests.Timeout('private-secret'), 'таймаут'),
    (requests.exceptions.SSLError('private-secret'), 'TLS'),
    (requests.ConnectionError('private-secret'), 'подключения'),
    (response(401), 'HTTP 401'), (response(403), 'HTTP 403'),
    (response(500), 'HTTP 500'), (response(200, []), 'формат данных'),
])
def test_native_sdd_technical_errors_block_without_secret_leaks(native_sdd, failure, detail):
    checker, session = native_sdd
    session.get.side_effect = failure if isinstance(failure, Exception) else [failure]
    assert checker.check_release('REL-1') is False
    checker.jira_main.issue.assert_not_called()
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    body = checker._release_comment('REL-1', False)
    assert detail in body
    assert 'Остальные проверки не запускались' in body
    assert 'private-secret' not in body


def test_native_sdd_invalid_json_blocks(native_sdd):
    checker, session = native_sdd
    session.get.return_value = NS(status_code=200, json=Mock(side_effect=ValueError('private-secret')))
    assert checker.check_release('REL-1') is False
    body = checker._release_comment('REL-1', False)
    assert 'некорректный JSON' in body and 'private-secret' not in body


def test_native_sdd_no_new_runtime_or_companion_file(native_sdd, monkeypatch, tmp_path):
    checker, session = native_sdd
    monkeypatch.setattr(rc, 'script_dir', tmp_path)
    session.get.side_effect = sdd_responses()
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    assert list(tmp_path.iterdir()) == []
    assert not hasattr(rc, 'SDD_GATE_SOURCE')
    assert not hasattr(rc, 'subprocess')


def test_native_sdd_comment_lists_both_missing_requirements(native_sdd):
    checker, session = native_sdd
    session.get.side_effect = sdd_responses(labels=[], detail={'detail': []})
    assert checker.check_release('REL-1') is False
    body = checker._release_comment('REL-1', False)
    assert '[ST-1|' in body and 'Добавьте в Jira' in body
    assert 'без символа &#35;' in body and 'openspec/changes/archive/2026-10-02-ST-1/spec.md' in body
    assert 'проверьте связь коммита с ST-1' in body


def test_native_sdd_unknown_still_allows_remaining_checks(native_sdd):
    """Do not silently strengthen the supplied unknown=PASS policy."""
    checker, session = native_sdd
    session.get.side_effect = sdd_responses(labels=[])[:4] + [response(503)] * 4
    checker.jira_main.issue.return_value = NS(key='REL-1', fields=NS(
        summary='Release', labels=[], assignee=None, issuelinks=[]))
    checker._extract_consist_of_issues = Mock(return_value=None)
    for name in ('_check_required_platform_label', '_check_release_service_consistency',
                 '_check_test_subtask', '_check_artifacts', '_check_release_coverage',
                 '_check_zephyr_test_cases', '_check_back_release_service_test_cycles',
                 '_check_web_release_service_test_cycles', '_check_bugs', '_check_stories',
                 '_check_required_pull_requests', '_check_unlinked_release_items_pull_requests',
                 '_check_gigacode_aifixed_labels', '_check_cloud_label',
                 '_check_sbrppl_third_party_label', '_check_sbrppl_story_points',
                 '_check_summary_description_match'):
        setattr(checker, name, Mock())
    assert checker.check_release('REL-1') is True
    checker.jira_main.issue.assert_called_once()
    checker._check_artifacts.assert_called_once()
    assert checker.check_results[0]['status'] == 'WARN'


def test_packed_entrypoint_uses_same_native_sdd(monkeypatch, native_sdd):
    """The older packed entrypoint must not retain JavaScript or stale SDD code."""
    import ast
    import base64
    import zlib
    tree = ast.parse((rc.script_dir / 'masked_release_cheker.py').read_text())
    payload = next(n.value.value for n in tree.body if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '_p' for t in n.targets))
    decoded = zlib.decompress(base64.b64decode(payload)).decode()
    assert 'SDD_GATE_SOURCE' not in decoded and 'subprocess' not in decoded
    namespace = dict(vars(rc))
    parsed = ast.parse(decoded)
    helpers = [n for n in parsed.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
               and n.name in {'SddCheckError', 'sdd_get_json', 'sdd_file_evidence', 'sdd_story_result',
                                  'sdd_bitbucket_changes_url', 'sdd_bitbucket_path',
                                  'sdd_bitbucket_commit_files', 'sdd_bitbucket_settings', 'sdd_complete_commit_files',
                                  'sdd_bitbucket_read', 'sdd_commit_story_result',
                                  'sdd_pull_request_api_url', 'sdd_release_artifacts', '_diag_sdd_story'}]
    source_defs = {n.name: n for n in ast.parse((rc.script_dir / 'release_checker.py').read_text()).body
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for helper in helpers:
        assert ast.dump(helper) == ast.dump(source_defs[helper.name]), helper.name
    exec(compile(ast.Module(body=helpers, type_ignores=[]), '<packed-sdd>', 'exec'), namespace)
    cls = next(n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == 'ReleaseValidator')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {'_check_sdd_gate', 'check_release'}]
    exec(compile(ast.Module(body=methods, type_ignores=[]), '<packed-gate>', 'exec'), namespace)
    checker, session = native_sdd
    namespace['sdd_release_artifacts'] = rc.sdd_release_artifacts
    checker._check_sdd_gate = namespace['_check_sdd_gate'].__get__(checker)
    session.get.side_effect = sdd_responses(labels=[], detail={'detail': []})
    assert namespace['check_release'](checker, 'REL-1') is False
    checker.jira_main.issue.assert_not_called()
    assert len(checker.report_data['ST-1']['errors']) == 2


@pytest.mark.parametrize('kind', ['approved', 'testing_type'])
def test_required_cycle_without_key_is_incomplete(kind):
    """A cycle name cannot substitute for the key needed to validate its contents."""
    checker = stage_validator()
    checker._zephyr_cycle_approved_checked = set()
    checker._zephyr_cycle_testing_type_checked = set()
    callback = (lambda: checker._check_test_cycle_cases_approved('REL-1', '', 'Release regress')) if kind == 'approved' else (
        lambda: checker._check_test_cycle_testing_type('REL-1', '', 'Release regress', 'Регресс'))
    checker._run_check('web_cycles', 'Web: ТЦ', callback)
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    assert checker.report_data['REL-1']['errors']
    assert 'отсутствует ключ ТЦ' in checker.report_data['REL-1']['errors'][0]


def test_confluence_unavailable_page_blocks_and_does_not_skip_other_pages(monkeypatch):
    """Unreadable Confluence data is not proof that no production bugs are listed."""
    monkeypatch.setattr(rc, 'config', {'jira': {'url': 'https://jira.example'},
                                     'confluence': {'url': 'https://wiki.example'}})
    checker = stage_validator()
    checker.confluence = Mock()
    checker.confluence_parent_page = 'root'
    checker.confluence.get_child_pages.return_value = [
        {'id': '1', 'title': 'HRC prod 1'}, {'id': '2', 'title': 'HRC prod 2'}]
    checker.confluence.get_page_by_id.side_effect = [requests.Timeout('private-secret'),
        {'body': {'storage': {'value': '<p>HRC-1</p>'}}}]
    bug = NS(key='HRC-1', fields=NS(summary='Prod bug', assignee=None))
    checker._run_check('bugs', 'Баги', lambda: checker._check_confluence([bug]))
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    assert checker.report_data['GENERAL']['errors']
    assert checker.report_data['HRC-1']['errors']
    assert checker.confluence.get_page_by_id.call_count == 2
    assert 'private-secret' not in checker._release_comment('REL-1', False)


@pytest.mark.parametrize('content,found', [
    ('<p>HRC-10 HRC-100 XHRC-1 HRC-1X</p>', False),
    ('<p>HRC-1</p>', True),
    ('<ac:parameter ac:name="key">HRC-1</ac:parameter>', True),
    ('<a href="https://jira.example/browse/HRC-1">Bug</a>', True),
])
def test_confluence_matches_complete_bug_key(validator, monkeypatch, content, found):
    monkeypatch.setattr(rc, 'config', {'confluence': {'url': 'https://wiki.example'}})
    validator.confluence = Mock()
    validator.confluence_parent_page = 'root'
    validator.confluence.get_child_pages.return_value = [{'id': '1', 'title': 'HRC prod'}]
    validator.confluence.get_page_by_id.return_value = {'body': {'storage': {'value': content}}}
    validator._check_confluence([NS(key='HRC-1')])
    assert validator._log_issue.called is found


def status_transition(date, status):
    """A minimal Jira changelog entry for chronology regressions."""
    return NS(created=date, items=[NS(field='status', toString=status)])


@pytest.mark.parametrize('histories,total,start_at', [([], 200, 0),
    ([status_transition('2026-10-01T10:00:00+00:00', 'Done')], 1, 1)])
def test_partial_history_never_produces_precise_duration_or_timing_success(histories, total, start_at):
    checker = stage_validator()
    changelog = NS(histories=histories, total=total, startAt=start_at)
    with pytest.raises(ValueError, match='неполную историю'):
        checker._calc_status_days(changelog, ['IFT'])
    story = NS(key='HRC-1', fields=NS(summary='Story', assignee=None))
    checker._run_check('stories', 'Story', lambda: checker._run_story_check(
        story, 'Порядок привязки требований', lambda: checker._check_story_requirements_timing(story, changelog)))
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    assert checker.report_data['HRC-1']['errors']


@pytest.mark.parametrize('timestamp', ['broken', '', '2026-10-01T10:00:00'])
def test_invalid_history_timestamp_is_not_silently_skipped(timestamp):
    checker = stage_validator()
    changelog = NS(histories=[status_transition(timestamp, 'IFT')])
    with pytest.raises(ValueError, match='Некорректная дата'):
        checker._calc_status_days(changelog, ['IFT'])
    with pytest.raises(ValueError, match='Некорректная дата'):
        checker._check_story_requirements_timing(NS(key='HRC-1'), changelog)


def test_complete_history_duration_still_counts_testing_intervals():
    changelog = NS(histories=[status_transition('2026-10-02T10:00:00+00:00', 'Done'),
                              status_transition('2026-10-01T10:00:00+00:00', 'IFT')], total=2, startAt=0)
    assert rc.ReleaseValidator._calc_status_days(changelog, ['IFT']) == 1.0


def test_requirements_changes_sort_by_real_timestamp_not_timezone_text(validator):
    changelog = NS(histories=[
        status_transition('2026-10-01T12:00:00+03:00', 'UAT'),
        NS(created='2026-10-01T10:00:00+00:00', items=[
            NS(field='customfield_21100', fieldId='customfield_21100')]),
    ], total=2, startAt=0)
    validator._check_story_requirements_timing(NS(key='HRC-1'), changelog)
    assert validator._log_issue.call_count == 1
    assert validator._log_issue.call_args.args[1] == 'error'
    assert 'после перехода' in validator._log_issue.call_args.args[2]


@pytest.mark.parametrize('ready,logged_error,expected', [(False, False, False), (True, True, False), (True, False, True)])
def test_report_honors_validation_verdict_and_errors(monkeypatch, ready, logged_error, expected):
    checker = stage_validator()
    monkeypatch.setattr(rc, 'config', {'jira': {'url': 'https://jira.example'}})
    checker._manage_jira_comment = Mock()
    def validate(key):
        if logged_error:
            checker._log_issue(key, 'error', 'Confirmed failure')
        return ready
    checker.check_release = validate
    assert checker.generate_report('REL-1') is expected
    checker._manage_jira_comment.assert_called_once_with('REL-1', expected)
    if not expected:
        assert checker.report_data['REL-1']['errors']
        assert '#de350b' in checker._release_comment('REL-1', False)


@pytest.mark.parametrize('key,folder', [
    ('SEARCHCS-1718', '2026-08-25-SEARCHCS-1718/specs/issues-upload-rate-limit'),
    ('SEARCHCS-1742', '2026-09-25-SEARCHCS-1742/specs/profile-privacy-settings-indexing'),
    ('SEARCHCS-1831', '2026-10-02-SEARCHCS-1831/specs/issues-for-sign-index'),
])
def test_sdd_accepts_archive_paths_from_reported_screenshot(native_sdd, key, folder, capsys):
    checker, session = native_sdd
    path = f'openspec/changes/archive/{folder}/spec.md'
    data = {'detail': [{'repositories': [{'commits': [{'files': [{'path': path}]}]}]}]}
    session.get.side_effect = sdd_responses(detail=data, issues=[{'key': key}])
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    output = capsys.readouterr().out
    assert path in output and '"matches": 1' in output


@pytest.mark.parametrize('repository,stat', [
    ({'commits': [{'id': 'commit', 'fileCount': 5}]}, 'missing_files'),
    ({'commits': [{'files': [], 'fileCount': 5}]}, 'truncated_files'),
    ({'commits': [{'files': [{'path': {'toString': 'openspec/ST-1/spec.md'}}]}]}, 'invalid_paths'),
    ({'name': 'repository'}, 'missing_commits'),
])
def test_sdd_omitted_file_data_blocks_as_incomplete_not_missing_openspec(native_sdd, repository, stat, capsys):
    checker, session = native_sdd
    session.get.side_effect = sdd_responses(detail={'detail': [{'repositories': [repository]}]})
    assert checker.check_release('REL-1') is False
    checker.jira_main.issue.assert_not_called()
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    body = checker._release_comment('REL-1', False)
    assert 'не передал полный список' in body
    assert 'не найден путь' not in body
    assert f'"{stat}": 1' in capsys.readouterr().out


def test_sdd_positive_match_survives_other_commit_without_files(native_sdd):
    checker, session = native_sdd
    data = sdd_responses()[-1].json()
    data['detail'][0]['repositories'][0]['commits'].append({'id': 'omitted-files'})
    session.get.side_effect = sdd_responses(detail=data)
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'


def test_sdd_diagnostic_samples_respect_debug_switch(native_sdd, monkeypatch, capsys):
    checker, session = native_sdd
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '0')
    session.get.side_effect = sdd_responses()
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert '[SDD DATA]' not in capsys.readouterr().out


def test_sdd_read_only_diagnostic_does_not_create_validator(native_sdd, monkeypatch, capsys):
    _, session = native_sdd
    monkeypatch.setattr(rc, 'ReleaseValidator', Mock(side_effect=AssertionError('must not be initialized')))
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '0')
    session.get.side_effect = sdd_responses()[1:]
    assert rc._diag_sdd_story('ST-1') == 0
    output = capsys.readouterr().out
    assert '[SDD DATA]' in output and '"matches": 1' in output
    assert '"read_only": true' in output
    assert 'test-token' not in output
    assert rc.os.environ['RELEASE_CHECKER_DEBUG_HTTP'] == '0'
    assert session.get.call_count == 4


def test_sdd_diagnostic_invalid_story_does_not_request_api(native_sdd):
    _, session = native_sdd
    assert rc._diag_sdd_story('invalid') == 2
    session.get.assert_not_called()


@pytest.mark.parametrize('repo_url,expected', [
    ('https://git.example/projects/HRP/repos/search/browse', 'https://git.example/rest/api/1.0/projects/HRP/repos/search'),
    ('https://git.example/stash/projects/HRP/repos/search/browse', 'https://git.example/stash/rest/api/1.0/projects/HRP/repos/search'),
    ('https://git.example/rest/api/1.0/projects/HRP/repos/search', 'https://git.example/rest/api/1.0/projects/HRP/repos/search'),
    ('https://user:secret@git.example/projects/HRP/repos/search', None),
    ('http://git.example/projects/HRP/repos/search', None),
])
def test_sdd_builds_only_linked_bitbucket_changes_route(repo_url, expected):
    commit_id = 'a' * 40
    result = rc.sdd_bitbucket_changes_url({'url': repo_url}, {'id': commit_id})
    assert result == (expected + '/commits/' + commit_id + '/changes' if expected else None)


@pytest.mark.parametrize('path,expected', [
    ('openspec/ST-1/spec.md', 'openspec/ST-1/spec.md'),
    ({'toString': 'openspec/ST-1/spec.md'}, 'openspec/ST-1/spec.md'),
    ({'components': ['openspec', 'changes', 'archive', '2026-10-02-SEARCHCS-1831', 'spec.md']},
     'openspec/changes/archive/2026-10-02-SEARCHCS-1831/spec.md'),
    ({'parent': 'openspec/ST-1', 'name': 'spec.md'}, 'openspec/ST-1/spec.md'),
    (None, None), ({}, None),
])
def test_sdd_normalizes_bitbucket_change_paths(path, expected):
    assert rc.sdd_bitbucket_path(path) == expected


def sdd_linked_truncated_detail(file_count=2):
    """A linked commit whose Jira preview omits an archived OpenSpec file."""
    return {'detail': [{'repositories': [{'url': 'https://git.example/projects/HRP/repos/search/browse',
        'commits': [{'id': 'a' * 40, 'fileCount': file_count,
                     'files': [{'path': 'HRP-pipeline/config.yaml'}]}]}]}]}


def bitbucket_test_session(monkeypatch, jira_session=None):
    bb = Mock()
    bb.headers = {}
    contexts = ([nullcontext(jira_session)] if jira_session is not None else []) + [nullcontext(bb)]
    factory = Mock(side_effect=contexts)
    monkeypatch.setattr(rc.requests, 'Session', factory)
    return bb, factory


def test_sdd_reads_second_bitbucket_page_for_screenshot_path(native_sdd, monkeypatch, capsys):
    checker, jira = native_sdd
    key = 'SEARCHCS-1831'
    jira.get.side_effect = sdd_responses(detail=sdd_linked_truncated_detail(), issues=[{'key': key}])
    bb, factory = bitbucket_test_session(monkeypatch, jira)
    bb.get.side_effect = [response(200, {'values': [{'path': {'components': ['HRP-pipeline', 'config.yaml']}}],
                                       'isLastPage': False, 'nextPageStart': 37}),
                            response(200, {'values': [{'path': {'components': ['openspec', 'changes', 'archive',
                               '2026-10-02-SEARCHCS-1831', 'specs', 'issues-for-sign-index', 'spec.md']}}],
                                           'isLastPage': True})]
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    assert [call.kwargs['params']['start'] for call in bb.get.call_args_list] == [0, 37]
    assert 'Authorization' not in bb.headers
    assert all(call.kwargs['allow_redirects'] is False for call in bb.get.call_args_list)
    output = capsys.readouterr().out
    assert '"stage": "expanded_files"' in output and '"matches": 1' in output
    assert factory.call_count == 2


def test_sdd_full_bitbucket_list_without_match_is_fail_not_incomplete(native_sdd, monkeypatch):
    checker, jira = native_sdd
    jira.get.side_effect = sdd_responses(detail=sdd_linked_truncated_detail())
    bb, _ = bitbucket_test_session(monkeypatch, jira)
    bb.get.return_value = response(200, {'values': [{'path': 'config/a.yaml'}, {'path': 'config/b.yaml'}],
                                       'isLastPage': True})
    assert checker.check_release('REL-1') is False
    assert checker.check_results[0]['status'] == 'FAIL'
    assert 'не найден путь' in checker.report_data['ST-1']['errors'][0]


@pytest.mark.parametrize('status', [401, 403, 302])
def test_sdd_bitbucket_access_failure_stays_blocking_and_clear(native_sdd, monkeypatch, status):
    checker, jira = native_sdd
    jira.get.side_effect = sdd_responses(detail=sdd_linked_truncated_detail())
    bb, _ = bitbucket_test_session(monkeypatch, jira)
    bb.get.return_value = response(status)
    assert checker.check_release('REL-1') is False
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    body = checker._release_comment('REL-1', False)
    assert f'Bitbucket HTTP {status}' in body
    assert 'не найден путь' not in body
    if status in {401, 403}:
        assert 'BITBUCKET_API_TOKEN' in rc.html.unescape(body) and 'REPO_READ' in rc.html.unescape(body)
    assert 'Authorization' not in bb.headers


@pytest.mark.parametrize('page', [
    {'values': [], 'isLastPage': False, 'nextPageStart': 0},
    {'values': [], 'isLastPage': False},
    {'values': [], 'isLastPage': True, 'truncated': True},
    {'values': [{'path': None}], 'isLastPage': True},
    {'unexpected': []},
])
def test_sdd_invalid_bitbucket_pages_are_not_absence(native_sdd, monkeypatch, page):
    checker, jira = native_sdd
    jira.get.side_effect = sdd_responses(detail=sdd_linked_truncated_detail())
    bb, _ = bitbucket_test_session(monkeypatch, jira)
    bb.get.return_value = response(200, page)
    assert checker.check_release('REL-1') is False
    assert checker.check_results[0]['status'] == 'INCOMPLETE'
    assert bb.get.call_count == 1


def test_sdd_server_cap_detected_even_if_bitbucket_reports_last_page(native_sdd, monkeypatch):
    checker, jira = native_sdd
    jira.get.side_effect = sdd_responses(detail=sdd_linked_truncated_detail(file_count=5000))
    bb, _ = bitbucket_test_session(monkeypatch, jira)
    bb.get.return_value = response(200, {'values': [{'path': 'config/a.yaml'}], 'isLastPage': True})
    assert checker.check_release('REL-1') is False
    assert checker.check_results[0]['status'] == 'INCOMPLETE'


def test_sdd_bitbucket_token_is_scoped_to_explicit_base(native_sdd, monkeypatch):
    monkeypatch.setenv('BITBUCKET_BASE_URL', 'https://git.example/stash')
    monkeypatch.setenv('BITBUCKET_API_TOKEN', 'bitbucket-test-token')
    bb, _ = bitbucket_test_session(monkeypatch)
    bb.get.return_value = response(200, {'values': [{'path': 'openspec/ST-1/spec.md'}], 'isLastPage': True})
    paths = rc.sdd_bitbucket_commit_files('https://git.example/stash/rest/api/1.0/projects/HRP/repos/search/commits/'
                                        + 'a'*40 + '/changes', 'ST-1')
    assert paths == [{'path': 'openspec/ST-1/spec.md'}]
    assert bb.headers['Authorization'] == 'Bearer bitbucket-test-token'
    assert bb.headers['Authorization'] != 'Bearer test-token'


@pytest.mark.parametrize('configured', ['', 'https://different.example', 'https://git.example/other-context'])
def test_sdd_never_sends_bitbucket_token_to_unconfigured_destination(native_sdd, monkeypatch, configured):
    monkeypatch.setenv('BITBUCKET_BASE_URL', configured)
    monkeypatch.setenv('BITBUCKET_API_TOKEN', 'private-token')
    bb, factory = bitbucket_test_session(monkeypatch)
    with pytest.raises(rc.SddCheckError):
        rc.sdd_bitbucket_commit_files('https://git.example/rest/api/1.0/projects/HRP/repos/search/commits/'
                                     + 'a'*40 + '/changes', 'ST-1')
    factory.assert_not_called()
    bb.get.assert_not_called()


def test_sdd_uses_existing_bitbucket_section_without_env(native_sdd, monkeypatch, capsys):
    checker, jira = native_sdd
    rc.config['bitbucket'] = {'url': 'https://git.example/', 'token': 'configured-bb-token', 'verify_ssl': False}
    jira.get.side_effect = sdd_responses(detail=sdd_linked_truncated_detail())
    bb, _ = bitbucket_test_session(monkeypatch, jira)
    bb.get.return_value = response(200, {'values': [{'path': {'components': ['openspec', 'ST-1', 'spec.md']}}],
                                       'isLastPage': True})
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate('REL-1'))
    assert checker.check_results[0]['status'] == 'PASS'
    assert bb.headers['Authorization'] == 'Bearer configured-bb-token'
    assert bb.get.call_args.kwargs['verify'] is False
    assert jira.headers['Authorization'] == 'Bearer test-token'
    assert 'configured-bb-token' not in capsys.readouterr().out


@pytest.mark.parametrize('verify', [False, True, '/corporate/bitbucket-ca.pem'])
def test_sdd_bitbucket_config_preserves_tls_setting(native_sdd, monkeypatch, verify):
    rc.config['bitbucket'] = {'url': 'https://git.example', 'token': 'bb', 'verify_ssl': verify}
    monkeypatch.delenv('RELEASE_CHECKER_CA_BUNDLE', raising=False)
    monkeypatch.delenv('RELEASE_CHECKER_VERIFY_SSL', raising=False)
    assert rc.sdd_bitbucket_settings() == ('https://git.example', 'bb', verify)


def test_sdd_dedicated_legacy_bitbucket_token_fallback(native_sdd):
    rc.config['jira']['bitbucket_token'] = 'legacy-bb-token'
    rc.config['bitbucket'] = {'url': 'https://git.example', 'token': ''}
    base, token, _ = rc.sdd_bitbucket_settings()
    assert (base, token) == ('https://git.example', 'legacy-bb-token')
    rc.config['bitbucket']['token'] = 'section-bb-token'
    assert rc.sdd_bitbucket_settings()[1] == 'section-bb-token'


def test_sdd_explicit_env_token_and_url_override_config(native_sdd, monkeypatch):
    rc.config['bitbucket'] = {'url': 'https://old.example', 'token': 'old-token', 'verify_ssl': False}
    monkeypatch.setenv('BITBUCKET_BASE_URL', 'https://git.example')
    monkeypatch.setenv('BITBUCKET_API_TOKEN', 'new-token')
    assert rc.sdd_bitbucket_settings()[:2] == ('https://git.example', 'new-token')


def test_sdd_env_host_override_cannot_move_config_token(native_sdd, monkeypatch):
    rc.config['bitbucket'] = {'url': 'https://old.example', 'token': 'private-config-token'}
    monkeypatch.setenv('BITBUCKET_BASE_URL', 'https://new.example')
    with pytest.raises(rc.SddCheckError, match='соответствующий BITBUCKET_API_TOKEN') as error:
        rc.sdd_bitbucket_settings()
    assert 'private-config-token' not in str(error.value)


def test_sdd_explicit_release_ca_override_still_wins(native_sdd, monkeypatch):
    rc.config['bitbucket'] = {'url': 'https://git.example', 'token': 'bb', 'verify_ssl': False}
    monkeypatch.setenv('RELEASE_CHECKER_CA_BUNDLE', '/configured/ca.pem')
    assert rc.sdd_bitbucket_settings()[2] == '/configured/ca.pem'


SDD_RELEASE = 'HRPRELEASE-147210'
SDD_SOURCE_SHA = '5e05e90f8e609c7c4acad5696d1e77f1fb9681bd'
SDD_RELEASE_PR = 'https://stash.sigma.sbrf.ru/projects/HRPLATFORM/repos/spine-search/pull-requests/1381/diff'


def sdd_release_pr_metadata(branch='release/HRPRELEASE-147210', state='OPEN', commit=SDD_SOURCE_SHA):
    return {'id': 1381, 'state': state, 'updatedDate': 2,
            'fromRef': {'id': 'refs/heads/' + branch, 'latestCommit': commit,
                        'repository': {'project': {'key': 'HRPLATFORM'}, 'slug': 'spine-search'}}}


def sdd_release_context(monkeypatch, jira, metadata=None, paths=None, include_jira=True):
    """The user's release PR tree, unrelated to the Story's deployment-only commits."""
    pr_session, tree_session = Mock(), Mock()
    pr_session.headers, tree_session.headers = {}, {}
    pr_session.get.return_value = response(200, metadata or sdd_release_pr_metadata())
    tree_session.get.return_value = response(200, {'values': paths if paths is not None else [
        'openspec/changes/archive/2026-08-25-SEARCHCS-1718/specs/issues-upload-rate-limit/spec.md',
        'openspec/changes/archive/2026-09-25-SEARCHCS-1742/specs/profile-privacy-settings-indexing/spec.md',
        'openspec/changes/archive/2026-10-02-SEARCHCS-1831/specs/issues-for-sign-index/spec.md'],
        'isLastPage': True})
    contexts = ([nullcontext(jira)] if include_jira else []) + [nullcontext(pr_session), nullcontext(tree_session)]
    factory = Mock(side_effect=contexts)
    monkeypatch.setattr(rc.requests, 'Session', factory)
    monkeypatch.setattr(rc, 'sdd_release_artifacts', REAL_SDD_RELEASE_ARTIFACTS)
    rc.config['bitbucket'] = {'url': 'https://stash.sigma.sbrf.ru', 'token': 'test-bb', 'verify_ssl': False}
    return pr_session, tree_session, factory


def test_sdd_release_pr_finds_archived_spec_not_changed_in_story_commits(native_sdd, monkeypatch, capsys):
    checker, jira = native_sdd
    preview = {'detail': [{'repositories': [{'commits': [{'files': [{'path': 'HRP-pipeline/config.yaml'}]}]}]}]}
    jira.get.side_effect = sdd_responses(detail=preview, issues=[{'key': 'SEARCHCS-1831'}]) + [
        response(200, {'id': 'release-id'}),
        response(200, {'detail': [{'pullRequests': [{'url': SDD_RELEASE_PR}]}]})]
    pr_session, tree, _ = sdd_release_context(monkeypatch, jira)
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate(SDD_RELEASE))
    assert checker.check_results[0]['status'] == 'PASS'
    assert tree.get.call_args.kwargs['params']['at'] == SDD_SOURCE_SHA
    assert tree.get.call_args.args[0].endswith('/projects/HRPLATFORM/repos/spine-search/files')
    assert '/pull-requests/1381' in pr_session.get.call_args.args[0]
    output = capsys.readouterr().out
    assert '"stage": "release_source"' in output and SDD_SOURCE_SHA in output
    assert '"stage": "release_artifacts"' in output


def test_sdd_release_tree_is_cached_for_all_stories(native_sdd, monkeypatch):
    _, jira = native_sdd
    jira.get.side_effect = [response(200, {'id': 'release-id'}),
        response(200, {'detail': [{'pullRequests': [{'url': SDD_RELEASE_PR}]}]})]
    _, tree, factory = sdd_release_context(monkeypatch, jira, include_jira=False)
    for story in ('SEARCHCS-1831', 'SEARCHCS-1742', 'SEARCHCS-1718'):
        assert REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, story) is True
    assert jira.get.call_count == 2
    assert tree.get.call_count == 1
    assert factory.call_count == 2


@pytest.mark.parametrize('branch,state', [
    ('release/HRPRELEASE-147211', 'OPEN'), ('feature/SEARCHCS-1831', 'OPEN'),
    ('release/HRPRELEASE-147210', 'DECLINED')])
def test_sdd_does_not_accept_wrong_release_or_declined_pr(native_sdd, monkeypatch, branch, state):
    _, jira = native_sdd
    _, tree, _ = sdd_release_context(monkeypatch, jira,
                                    metadata=sdd_release_pr_metadata(branch=branch, state=state), include_jira=False)
    assert REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831', SDD_RELEASE_PR) is False
    tree.get.assert_not_called()
    jira.get.assert_not_called()


def test_sdd_release_pr_uses_source_repository_for_forks(native_sdd, monkeypatch):
    _, jira = native_sdd
    metadata = sdd_release_pr_metadata()
    metadata['fromRef']['repository'] = {'project': {'key': 'FORK'}, 'slug': 'search-release'}
    _, tree, _ = sdd_release_context(monkeypatch, jira, metadata=metadata, include_jira=False)
    assert REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831', SDD_RELEASE_PR)
    assert '/projects/FORK/repos/search-release/files' in tree.get.call_args.args[0]
    assert tree.get.call_args.kwargs['params']['at'] == SDD_SOURCE_SHA


def test_sdd_release_tree_permission_failure_is_not_absence(native_sdd, monkeypatch):
    _, jira = native_sdd
    _, tree, _ = sdd_release_context(monkeypatch, jira, include_jira=False)
    tree.get.return_value = response(403)
    with pytest.raises(rc.SddCheckError, match='Bitbucket HTTP 403'):
        REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831', SDD_RELEASE_PR)


def test_sdd_release_diagnostic_accepts_exact_pr_without_jira_pr_discovery(native_sdd, monkeypatch):
    _, jira = native_sdd
    jira.get.side_effect = sdd_responses(detail={'detail': []})[1:]
    _, tree, _ = sdd_release_context(monkeypatch, jira)
    assert rc._diag_sdd_story('SEARCHCS-1831', SDD_RELEASE, SDD_RELEASE_PR) == 0
    assert tree.get.call_args.kwargs['params']['at'] == SDD_SOURCE_SHA
    assert jira.get.call_count == 4


def test_sdd_release_tree_artifact_does_not_replace_required_sdd_label(native_sdd, monkeypatch):
    checker, jira = native_sdd
    jira.get.side_effect = sdd_responses(labels=[], detail={'detail': []}, issues=[{'key': 'SEARCHCS-1831'}]) + [
        response(200, {'id': 'release-id'}),
        response(200, {'detail': [{'pullRequests': [{'url': SDD_RELEASE_PR}]}]})]
    sdd_release_context(monkeypatch, jira)
    checker._run_check('sdd', 'SDD', lambda: checker._check_sdd_gate(SDD_RELEASE))
    assert checker.check_results[0]['status'] == 'FAIL'
    assert len(checker.report_data['SEARCHCS-1831']['errors']) == 1
    assert 'лейбл SDD' in checker.report_data['SEARCHCS-1831']['errors'][0]


def test_sdd_release_source_can_be_discovered_from_jira_remote_links(native_sdd, monkeypatch):
    _, jira = native_sdd
    jira.get.side_effect = [response(200, {'id': 'release-id'}), response(200, {'detail': []}),
                           response(200, [{'object': {'url': SDD_RELEASE_PR}}])]
    _, tree, _ = sdd_release_context(monkeypatch, jira, include_jira=False)
    assert REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831')
    assert jira.get.call_args.args[0].endswith('/remotelink')
    assert tree.get.call_count == 1


def test_sdd_old_merged_release_pr_cannot_override_current_open_tree(native_sdd, monkeypatch):
    _, jira = native_sdd
    old_pr = SDD_RELEASE_PR.replace('/1381/', '/1300/')
    jira.get.side_effect = [response(200, {'id': 'release-id'}), response(200, {
        'detail': [{'pullRequests': [{'url': old_pr}, {'url': SDD_RELEASE_PR}]}]})]
    old = sdd_release_pr_metadata(state='MERGED', commit='a'*40)
    old['id'], old['updatedDate'] = 1300, 1
    current = sdd_release_pr_metadata()
    old_session, current_session, tree = Mock(), Mock(), Mock()
    for session in (old_session, current_session, tree):
        session.headers = {}
    old_session.get.return_value = response(200, old)
    current_session.get.return_value = response(200, current)
    tree.get.return_value = response(200, {'values': ['src/current.py'], 'isLastPage': True})
    monkeypatch.setattr(rc.requests, 'Session', Mock(side_effect=[
        nullcontext(old_session), nullcontext(current_session), nullcontext(tree)]))
    rc.config['bitbucket'] = {'url': 'https://stash.sigma.sbrf.ru', 'token': 'test-bb'}
    assert REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831') is False
    assert tree.get.call_count == 1
    assert tree.get.call_args.kwargs['params']['at'] == SDD_SOURCE_SHA


def test_sdd_release_tree_pages_are_pinned_to_same_source_commit(native_sdd, monkeypatch):
    _, jira = native_sdd
    _, tree, _ = sdd_release_context(monkeypatch, jira, include_jira=False)
    tree.get.side_effect = [response(200, {'values': ['src/a.py'], 'isLastPage': False, 'nextPageStart': 21}),
        response(200, {'values': ['openspec/changes/archive/2026-10-02-SEARCHCS-1831/spec.md'], 'isLastPage': True})]
    assert REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831', SDD_RELEASE_PR)
    assert [call.kwargs['params']['start'] for call in tree.get.call_args_list] == [0, 21]
    assert all(call.kwargs['params']['at'] == SDD_SOURCE_SHA for call in tree.get.call_args_list)


def test_sdd_missing_pr_branch_is_unavailable_not_confirmed_absence(native_sdd, monkeypatch):
    _, jira = native_sdd
    metadata = sdd_release_pr_metadata()
    metadata['fromRef'].pop('id')
    _, tree, _ = sdd_release_context(monkeypatch, jira, metadata=metadata, include_jira=False)
    with pytest.raises(rc.SddCheckError, match='исходную ветку'):
        REAL_SDD_RELEASE_ARTIFACTS(jira, 'https://jira.example', SDD_RELEASE, 'SEARCHCS-1831', SDD_RELEASE_PR)
    tree.get.assert_not_called()
