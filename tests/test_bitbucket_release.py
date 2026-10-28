"""Verified Bitbucket reads used by PR, MERGED, GigaCode and SDD release checks."""
from collections import defaultdict
from contextlib import nullcontext
from types import SimpleNamespace as NS
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests

from scripts import release_checker as rc


PR_URL = 'https://git.example/projects/APP/repos/service/pull-requests/42'
API_PR = 'https://git.example/rest/api/1.0/projects/APP/repos/service/pull-requests/42'
SHA = 'a' * 40
ISSUE = NS(key='ABC-1', id='101', fields=NS(labels=[], summary='Story', assignee=None))


def reply(payload, status=200, headers=None):
    return NS(status_code=status, json=lambda: payload, headers=headers or {})


def page(values, **kwargs):
    return {'values': values, 'isLastPage': True, **kwargs}


@pytest.fixture
def client(monkeypatch):
    """Real policy/client functions with in-memory Jira and Bitbucket transports."""
    monkeypatch.setattr(rc, 'config', {'jira': {'url': 'https://jira.example', 'token': 'jira-secret'},
        'bitbucket': {'url': 'https://git.example', 'token': 'bb-secret', 'verify_ssl': False}})
    for key in ('BITBUCKET_BASE_URL', 'BITBUCKET_API_TOKEN', 'RELEASE_CHECKER_CA_BUNDLE',
                'RELEASE_CHECKER_VERIFY_SSL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('RELEASE_CHECKER_DEBUG_HTTP', '1')
    monkeypatch.setattr(rc.time, 'sleep', Mock())
    checker = rc.ReleaseValidator.__new__(rc.ReleaseValidator)
    checker.jira_http, checker.jira_main = Mock(), Mock()
    checker.jira_http.headers = {'Authorization': 'Bearer jira-secret'}
    checker._reset_pr_run_state()
    checker.report_data = defaultdict(lambda: {'summary': '', 'assignee': 'Не назначен',
        'url': '', 'errors': [], 'warnings': [], 'success': []})
    checker.check_results, checker._active_check = [], None
    checker.dry_run = True
    jira_detail = {'detail': [{'pullRequests': [{'id': 42, 'url': PR_URL}]}], 'errors': []}
    remote = []
    bb = Mock()
    bb.headers = {}
    metadata = {'id': 42, 'state': 'OPEN', 'title': 'Ordinary title', 'description': None,
                'fromRef': {'id': 'refs/heads/feature/ABC-1', 'repository': {'project': {'key': 'APP'}, 'slug': 'service'}}}
    routes = {
        '/rest/api/1.0/projects/APP/repos/service/pull-requests/42': reply(metadata),
        '/rest/api/1.0/projects/APP/repos/service/pull-requests/42/commits': reply(page([{'id': SHA, 'message': 'normal'}])),
        '/rest/jira/1.0/issues/ABC-1/commits': reply(page([{'toCommit': {'id': SHA, 'message': 'normal'},
                    'repository': {'project': {'key': 'APP'}, 'slug': 'service'}}])),
        '/rest/api/1.0/projects/APP/repos/service/commits/' + SHA + '/pull-requests': reply(page([])),
    }
    def jira_get(url, **kwargs):
        if url.endswith('/detail'):
            return reply(jira_detail)
        if url.endswith('/remotelink'):
            return reply(remote)
        if '/issue/ABC-1' in url:
            return reply({'id': '101', 'fields': {'subtasks': []}})
        raise AssertionError('Unexpected Jira request')
    def bb_get(url, **kwargs):
        route = routes[urlsplit(url).path]
        return route(url, **kwargs) if callable(route) else route
    checker.jira_http.get.side_effect = jira_get
    bb.get.side_effect = bb_get
    factory = Mock(side_effect=lambda: nullcontext(bb))
    monkeypatch.setattr(rc.requests, 'Session', factory)
    return NS(checker=checker, bb=bb, factory=factory, detail=jira_detail, remote=remote,
              routes=routes, metadata=metadata, issue=ISSUE)


def test_presence_requires_verified_bitbucket_pr_not_jira_count(client):
    evidence = client.checker._find_issue_pull_request_only_evidence(ISSUE)
    assert 'Bitbucket PR 42' in evidence
    assert client.bb.get.call_count == 1
    assert client.bb.headers['Authorization'] == 'Bearer bb-secret'
    assert client.bb.get.call_args.kwargs['verify'] is False
    assert client.checker.jira_http.headers['Authorization'] == 'Bearer jira-secret'
    assert all(not c.args[0].endswith('/summary') for c in client.checker.jira_http.get.call_args_list)


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_presence_http_failure_does_not_confirm_pr_or_cache_absence(client, status):
    client.routes[urlsplit(API_PR).path] = reply({}, status)
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE) is None
    assert any(f'HTTP {status}' in error for error in client.checker._pr_lookup_failures)
    client.routes[urlsplit(API_PR).path] = reply(client.metadata)
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE)


def test_remote_link_is_discovery_then_verified_in_bitbucket(client):
    client.detail['detail'] = []
    client.remote.append({'object': {'url': PR_URL}})
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE)
    assert client.bb.get.call_args.args[0] == API_PR
    assert client.checker.jira_http.get.call_args.args[0].endswith('/remotelink')


def test_pr_on_child_task_is_not_story_evidence(client):
    client.detail['detail'] = []
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply(page([]))
    client.checker.jira_main.issue.return_value = NS(fields=NS(subtasks=[NS(key='ABC-2')]))
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE) is None
    assert not client.checker._pr_lookup_failures
    assert not any('ABC-2' in str(c) for c in client.checker.jira_http.get.call_args_list)


def test_commit_to_pr_fallback_still_stays_scoped_to_same_issue(client):
    client.detail['detail'] = []
    client.routes['/rest/api/1.0/projects/APP/repos/service/commits/' + SHA + '/pull-requests'] = reply(page([{'id': 42}]))
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE)
    paths = [urlsplit(c.args[0]).path for c in client.bb.get.call_args_list]
    assert '/rest/jira/1.0/issues/ABC-1/commits' in paths
    assert any(path.endswith('/commits/' + SHA + '/pull-requests') for path in paths)


@pytest.mark.parametrize('state,merged', [('OPEN', False), ('DECLINED', False), ('MERGED', True)])
def test_merged_state_comes_from_bitbucket_not_jira(client, state, merged):
    client.detail['detail'][0]['pullRequests'][0]['status'] = 'MERGED' if not merged else 'OPEN'
    client.metadata['state'] = state
    result = client.checker._find_issue_merged_pull_request_evidence(ISSUE)
    assert bool(result) is merged
    if result:
        assert 'Bitbucket PR 42 (merged)' in result


def test_unavailable_merged_state_is_unknown_not_no_merged_pr(client):
    client.routes[urlsplit(API_PR).path] = reply({}, 403)
    with pytest.raises(RuntimeError, match='HTTP 403'):
        client.checker._find_issue_merged_pull_request_evidence(ISSUE)


@pytest.mark.parametrize('field,marker', [('title', '#GigaCode Agent'),
    ('description', 'Co-authored-by: GigaCode Assistant'), ('description', 'Co-authorer-by: GigaCode')])
def test_full_pr_caption_marker_is_authoritative_and_logs_hide_content(client, capsys, field, marker):
    client.metadata[field] = 'PRIVATE_CAPTION\n' + marker
    result = client.checker._lookup_gigacode(ISSUE)
    assert result['state'] == 'FOUND' and 'GigaCode' in result['marker']
    assert client.bb.get.call_count == 1
    output = capsys.readouterr().out
    assert 'PRIVATE_CAPTION' not in output and 'bb-secret' not in output and 'jira-secret' not in output


def test_gigacode_in_second_pr_commit_page_is_found(client):
    def commits(url, **kwargs):
        if kwargs['params']['start'] == 0:
            return reply(page([{'id': SHA, 'message': 'normal'}], isLastPage=False, nextPageStart=9))
        return reply(page([{'id': 'b'*40, 'message': 'Co-authored-by: GigaCode Assistant'}]))
    client.routes[urlsplit(API_PR).path + '/commits'] = commits
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'
    calls = [c for c in client.bb.get.call_args_list if c.args[0].endswith('/42/commits')]
    assert [c.kwargs['params']['start'] for c in calls] == [0, 9]


def test_positive_marker_on_partial_page_does_not_need_missing_next_page(client):
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(page(
        [{'id': SHA, 'message': 'Co-authored-by: GigaCode Assistant'}], isLastPage=False, nextPageStart=99))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'
    assert sum(c.args[0].endswith('/42/commits') for c in client.bb.get.call_args_list) == 1


def test_issue_changeset_marker_survives_pr_access_failure(client):
    client.routes[urlsplit(API_PR).path] = reply({}, 403)
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply(page([
        {'toCommit': {'id': SHA, 'message': 'Co-authored-by: GigaCode Assistant'},
         'repository': {'project': {'key': 'APP'}, 'slug': 'service'}}]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'


def test_issue_commits_are_fully_paged(client):
    def commits(url, **kwargs):
        if kwargs['params']['start'] == 0:
            return reply(page([{'toCommit': {'id': SHA, 'message': 'normal'}}], isLastPage=False, nextPageStart=2))
        return reply(page([{'toCommit': {'id': 'b'*40, 'message': '#GigaCode Agent'}}]))
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = commits
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'


def test_missing_commit_message_is_read_from_commit_metadata(client):
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(page([{'id': SHA}]))
    client.routes['/rest/api/1.0/projects/APP/repos/service/commits/' + SHA] = reply(
        {'id': SHA, 'message': 'Co-authored-by: GigaCode Assistant'})
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'


def test_complete_bitbucket_data_without_marker_is_known_absence(client):
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'NOT_FOUND'
    assert len(client.bb.get.call_args_list) == 3


def test_two_commits_in_one_repo_are_not_an_incomplete_repository_count(client):
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply(page([
        {'toCommit': {'id': SHA, 'message': 'first'}, 'repository': {'slug': 'service'}},
        {'toCommit': {'id': 'b'*40, 'message': 'second'}, 'repository': {'slug': 'service'}},
    ]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'NOT_FOUND'


@pytest.mark.parametrize('bad_page', [
    {'values': [], 'isLastPage': False, 'nextPageStart': 0},
    {'values': [{'id': SHA, 'message': 'normal'}], 'isLastPage': False},
    {'values': [], 'isLastPage': True, 'truncated': True},
    {'values': ['not-a-commit'], 'isLastPage': True},
    {'errorMessages': ['Co-authored-by: GigaCode Assistant']},
])
def test_bad_or_incomplete_commit_pages_are_unknown_not_absence(client, bad_page):
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(bad_page)
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'


def test_unparseable_changeset_is_unknown_not_exception_or_marker(client):
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply(page([{'toCommit': 'Co-authored-by: GigaCode'}]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'


def test_unknown_is_not_cached_as_no_gigacode(client):
    client.routes[urlsplit(API_PR).path + '/commits'] = reply({}, 403)
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(page([{'message': '#GigaCode Agent'}]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'


def test_jira_marker_in_preview_or_metadata_does_not_prove_gigacode(client):
    client.detail['detail'][0]['pullRequests'][0]['description'] = '#GigaCode Agent'
    client.detail['detail'][0]['_instance'] = {'name': 'Co-authored-by: GigaCode Assistant'}
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'NOT_FOUND'


def test_partial_jira_links_without_positive_marker_stay_unknown(client):
    client.detail['detail'][0]['isLast'] = False
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'


def test_shared_cache_reuses_verified_pr_across_stages_and_resets_per_run(client):
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE)
    assert client.checker._find_issue_merged_pull_request_evidence(ISSUE) is None
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'NOT_FOUND'
    assert sum(c.args[0] == API_PR for c in client.bb.get.call_args_list) == 1
    assert client.checker.jira_http.get.call_count == 3
    client.checker._reset_pr_run_state()
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE)
    assert sum(c.args[0] == API_PR for c in client.bb.get.call_args_list) == 2


def test_gigacode_dry_run_does_not_update_labels(client):
    client.metadata['description'] = '#GigaCode Agent'
    client.checker._get_release_pr_targets = Mock(return_value=[(ISSUE, [ISSUE])])
    client.checker._check_gigacode_aifixed_labels('REL-1')
    client.checker.jira_http.put.assert_not_called()
    assert client.checker.report_data['ABC-1']['warnings']


def test_confirmed_gigacode_adds_label_to_same_target_issue_only(client):
    issue = NS(key='ABC-1', id='101', fields=NS(labels=[], summary='Story', assignee=None))
    client.metadata['description'] = '#GigaCode Agent'
    client.checker.dry_run = False
    client.checker.jira_http.put.return_value = reply({}, 204)
    client.checker._get_release_pr_targets = Mock(return_value=[(issue, [issue])])
    client.checker._check_gigacode_aifixed_labels('REL-1')
    assert client.checker.jira_http.put.call_args.args[0].endswith('/issue/ABC-1')
    assert client.checker.jira_http.put.call_args.kwargs['json'] == {'update': {'labels': [{'add': 'AIFIXED'}]}}
    assert 'AIFIXED' in issue.fields.labels


def test_long_retry_after_does_not_fan_out_to_other_issues_or_sleep(client, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(rc.time, 'monotonic', lambda: clock[0])
    client.checker.jira_http.get.return_value = reply({}, 429, {'Retry-After': '120'})
    client.checker.jira_http.get.side_effect = None
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply(page([]))
    for key in ('ABC-1', 'ABC-2', 'ABC-3'):
        client.routes[f'/rest/jira/1.0/issues/{key}/commits'] = reply(page([]))
        assert client.checker._find_issue_pull_request_only_evidence(NS(key=key, id='1')) is None
    assert client.checker.jira_http.get.call_count == 1
    rc.time.sleep.assert_not_called()
    clock[0] = 120
    client.checker.jira_http.get.side_effect = lambda *a, **kw: reply(client.detail)
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE)


def test_deadline_stops_before_any_bitbucket_request(client, monkeypatch):
    monkeypatch.setattr(rc.time, 'monotonic', lambda: 10)
    client.checker._pr_request_deadline = 9
    with pytest.raises(rc.SddCheckError, match='лимит времени'):
        client.checker._bitbucket_api(API_PR, 'ABC-1')
    client.bb.get.assert_not_called()


def test_wrong_destination_never_receives_configured_token(client):
    client.detail['detail'][0]['pullRequests'][0]['url'] = PR_URL.replace('git.example', 'attacker.example')
    assert client.checker._find_issue_pull_request_only_evidence(ISSUE) is None
    assert not client.bb.get.called
    assert 'bb-secret' not in str(client.checker._pr_lookup_failures)


def test_pr_and_gigacode_diagnostics_use_same_client_without_writes(client, capsys):
    client.checker.jira_main.issue.return_value = ISSUE
    rc._diag_pr(client.checker, 'ABC-1')
    rc._diag_gigacode(client.checker, 'ABC-1')
    output = capsys.readouterr().out
    assert '[BITBUCKET PR DIAG]' in output and 'NOT_FOUND' in output
    assert 'bb-secret' not in output and 'jira-secret' not in output
    client.checker.jira_http.put.assert_not_called()
    client.checker.jira_http.post.assert_not_called()


def test_restricted_integration_uses_sdd_style_jira_links_and_full_commit_metadata(client):
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply({}, 404)
    fetch = client.checker.jira_http.get.side_effect
    def jira(url, **kwargs):
        if url.endswith('/summary'):
            return reply({'summary': {'repository': {'overall': {'count': 2}}}})
        if url.endswith('/detail') and kwargs['params']['dataType'] == 'repository':
            return reply({'detail': [{'repositories': [{'url': 'https://git.example/projects/APP/repos/service',
                'commits': [{'id': SHA, 'message': 'truncated'}, {'id': 'b'*40, 'message': 'truncated'}]}]}]})
        return fetch(url, **kwargs)
    client.checker.jira_http.get.side_effect = jira
    client.routes['/rest/api/1.0/projects/APP/repos/service/commits/' + SHA] = reply({'id': SHA, 'message': 'normal'})
    client.routes['/rest/api/1.0/projects/APP/repos/service/commits/' + 'b'*40] = reply(
        {'id': 'b'*40, 'message': 'Co-authored-by: GigaCode Assistant'})
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'


def test_complete_sdd_style_fallback_counts_commits_not_repositories(client):
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply({}, 404)
    fetch = client.checker.jira_http.get.side_effect
    def jira(url, **kwargs):
        if url.endswith('/summary'):
            return reply({'summary': {'repository': {'overall': {'count': 2}}}})
        if url.endswith('/detail') and kwargs['params']['dataType'] == 'repository':
            return reply({'detail': [{'repositories': [{'url': 'https://git.example/projects/APP/repos/service',
                'commits': [{'id': SHA}, {'id': 'b'*40}]}]}]})
        return fetch(url, **kwargs)
    client.checker.jira_http.get.side_effect = jira
    for sha in (SHA, 'b'*40):
        client.routes['/rest/api/1.0/projects/APP/repos/service/commits/' + sha] = reply({'id': sha, 'message': 'normal'})
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'NOT_FOUND'


def test_object_pagination_repetition_is_unknown_not_absence(client):
    client.routes[urlsplit(API_PR).path + '/commits'] = lambda url, **kw: reply(page(
        [{'id': SHA, 'message': 'normal'}], isLastPage=False, nextPageStart=kw['params']['start']+1))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'
    assert sum(c.args[0].endswith('/42/commits') for c in client.bb.get.call_args_list) == 2


def test_http_downgrade_never_receives_bitbucket_token(client):
    with pytest.raises(rc.SddCheckError, match='HTTPS'):
        client.checker._bitbucket_api(API_PR.replace('https://', 'http://'), 'ABC-1')
    client.bb.get.assert_not_called()


def test_gigacode_from_other_story_in_shared_release_pr_does_not_label_this_story(client):
    client.metadata['fromRef']['id'] = 'refs/heads/release/REL-1'
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(page([
        {'id': SHA, 'message': 'ABC-2 implementation\nCo-authored-by: GigaCode Assistant'}]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'NOT_FOUND'
    client.checker.jira_http.put.assert_not_called()


def test_gigacode_in_shared_release_pr_is_scoped_by_exact_issue_key(client):
    client.metadata['fromRef']['id'] = 'refs/heads/release/REL-1'
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(page([
        {'id': SHA, 'message': 'ABC-1 implementation\nCo-authored-by: GigaCode Assistant'}]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'


def test_gigacode_with_no_issue_scope_in_shared_pr_stays_unknown(client):
    client.metadata['fromRef']['id'] = 'refs/heads/release/REL-1'
    client.routes[urlsplit(API_PR).path + '/commits'] = reply(page([
        {'id': SHA, 'message': 'Implementation\nCo-authored-by: GigaCode Assistant'}]))
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'


def test_early_marker_cache_does_not_hide_later_commits_of_another_story(client):
    client.metadata['fromRef']['id'] = 'refs/heads/release/REL-1'
    def commits(url, **kwargs):
        if kwargs['params']['start'] == 0:
            return reply(page([{'id': SHA, 'message': 'ABC-1\nCo-authored-by: GigaCode Assistant'}],
                              isLastPage=False, nextPageStart=1))
        return reply(page([{'id': 'b'*40, 'message': 'ABC-2\nCo-authored-by: GigaCode Assistant'}]))
    client.routes[urlsplit(API_PR).path + '/commits'] = commits
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'FOUND'
    assert client.checker._lookup_gigacode(NS(key='ABC-2', id='102'))['state'] == 'FOUND'
    assert sum(c.args[0].endswith('/42/commits') for c in client.bb.get.call_args_list) == 3


@pytest.mark.parametrize('groups', [[None], [{'repositories': ['invalid']}], [{'repositories': [{'commits': 'invalid'}]}]])
def test_malformed_sdd_style_commit_links_remain_unknown(client, groups):
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply({}, 404)
    fetch = client.checker.jira_http.get.side_effect
    def jira(url, **kwargs):
        if url.endswith('/summary'):
            return reply({'summary': {'repository': {'overall': {'count': 1}}}})
        if url.endswith('/detail') and kwargs['params']['dataType'] == 'repository':
            return reply({'detail': groups})
        return fetch(url, **kwargs)
    client.checker.jira_http.get.side_effect = jira
    assert client.checker._lookup_gigacode(ISSUE)['state'] == 'UNKNOWN'



def test_open_pr_does_not_hide_merged_remote_link(client):
    client.remote.append({'object': {'url': PR_URL.replace('/42', '/43')}})
    client.routes[urlsplit(API_PR).path.replace('/42', '/43')] = reply({**client.metadata, 'id': 43, 'state': 'MERGED'})
    assert 'PR 43' in client.checker._find_issue_merged_pull_request_evidence(ISSUE)
    assert any(c.args[0].endswith('/remotelink') for c in client.checker.jira_http.get.call_args_list)


def test_open_pr_does_not_hide_merged_commit_pr(client):
    client.routes['/rest/api/1.0/projects/APP/repos/service/commits/' + SHA + '/pull-requests'] = reply(page([{'id': 43}]))
    client.routes[urlsplit(API_PR).path.replace('/42', '/43')] = reply({**client.metadata, 'id': 43, 'state': 'MERGED'})
    assert 'PR 43' in client.checker._find_issue_merged_pull_request_evidence(ISSUE)


def test_merged_discovery_does_not_claim_complete_when_commit_source_unavailable(client):
    client.routes['/rest/jira/1.0/issues/ABC-1/commits'] = reply({}, 503)
    client.checker._bitbucket_changesets_from_jira = Mock(side_effect=rc.SddCheckError('unavailable'))
    with pytest.raises(RuntimeError):
        client.checker._find_issue_merged_pull_request_evidence(ISSUE)


def test_add_aifixed_preserves_labels_added_since_issue_snapshot(client):
    issue = NS(key='ABC-1', fields=NS(labels=['sdd']))
    server_labels = ['sdd', 'added-during-run']
    def put(url, **kwargs):
        payload = kwargs['json']
        assert 'fields' not in payload
        for operation in payload['update']['labels']:
            server_labels.append(operation['add'])
        return reply({}, 204)
    client.checker.jira_http.put.side_effect = put
    assert client.checker._add_label_if_missing(issue, 'AIFIXED') is True
    assert server_labels == ['sdd', 'added-during-run', 'AIFIXED']
