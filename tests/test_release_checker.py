"""Release policy regressions; never connect to Jira or publish comments."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
from contextlib import nullcontext

import pytest
import requests

from scripts import release_checker as rc


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


@pytest.mark.parametrize('days,expected', [(0, 'success'), (1, 'success'), (6.9, 'success'), (6.9001, 'error')])
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
    assert 'Не удалось проверить необходимость лейбла AIFIXED.' in body
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
    assert 'Не удалось проверить необходимость лейбла AIFIXED.' in body
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
def test_all_applicable_release_stages_execute_despite_one_crash(failed_method, ready):
    checker = stage_validator()
    release = NS(key='REL-1', fields=NS(summary='Cloud release', labels=['back', 'web'],
                                      assignee=None, issuelinks=[]))
    checker.jira_main = Mock()
    checker.jira_main.issue.return_value = release
    checker.zephyr = NS(failed_requests=0, fatal_error=None, last_test_cycle_search_stats=[])
    checker._consist_of_cache = {}
    checker._release_service_infos_cache = {}
    methods = [
        '_check_test_subtask', '_check_artifacts', '_check_release_coverage',
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
    assert len(checker.check_results) == 16
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
    assert fake_clock[0] == 35
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
