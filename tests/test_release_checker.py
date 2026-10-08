"""Release policy regressions; never connect to Jira or publish comments."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

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
    payloads = validator._get_dev_status_payloads('123')
    assert any(provider == 'custom-git' and validator._extract_pull_request_evidence(payload)
               for provider, _, payload in payloads)


def test_missing_summary_does_not_trigger_detail_scan(validator):
    validator._pr_get_json = Mock(return_value={})
    with pytest.raises(RuntimeError, match='development'):
        validator._get_dev_status_payloads('123')
    assert validator._pr_get_json.call_count == 2
    assert all(call.args[0].endswith('/summary') for call in validator._pr_get_json.call_args_list)


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
    # Two summary versions + remote links, three failed probes per source.
    assert validator.jira_http.get.call_count <= 3 * rc.PR_ENDPOINT_FAILURE_LIMIT * rc.PR_MAX_ATTEMPTS
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
    validator._pr_get_json = Mock(side_effect=[summary_payload(1, {'stash': {'count': 1}}), {'detail': []}])
    with pytest.raises(RuntimeError, match='пустой detail'):
        validator._get_dev_status_payloads('123')


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
    assert 'AIFIXED: не удалось проверить' in body
    assert 'Нет описания' in body
    assert 'INCOMPLETE' in body
    assert body.startswith(rc.CHECK_TABLE_HEADER)
    assert len(body.splitlines()) == 3
    assert all(line.startswith('|') and line.endswith('|') for line in body.splitlines())


def test_comment_keeps_optional_warnings_on_success():
    checker = stage_validator()
    checker.my_account_id = 'synthetic-account'
    checker.jira_main = Mock()
    checker.jira_main.comments.return_value = []
    checker._run_check('giga', 'GigaCode', lambda: checker._log_issue('ABC-1', 'warning', 'AIFIXED: не удалось проверить'), required=False)
    checker._manage_jira_comment('REL-1', True)
    body = checker.jira_main.add_comment.call_args.args[1]
    assert 'Предупреждение — AIFIXED: не удалось проверить' in body
    assert body.startswith(rc.CHECK_TABLE_HEADER)
    assert len(body.splitlines()) == 2
    assert 'INCOMPLETE' in body


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


def test_all_applicable_release_stages_execute_despite_one_crash():
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
    checker._check_artifacts.side_effect = RuntimeError('synthetic outage')
    assert checker.check_release('REL-1') is False
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
    validator.zephyr = Mock()
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


def test_zephyr_projects_fields_for_single_full_response(zephyr):
    zephyr.session.get.return_value = response(200, [case_payload()])
    assert zephyr.get_test_cases_for_issue('ABC-1') == ([case_payload()], None)
    assert zephyr.session.get.call_count == 1
    assert zephyr.session.get.call_args.kwargs['params']['fields'] == rc.ZEPHYR_CASE_FIELDS


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
