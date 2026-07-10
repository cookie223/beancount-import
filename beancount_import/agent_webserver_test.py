import json
import os
import shutil
import tempfile
import time

from tornado.testing import AsyncHTTPTestCase

from . import journal_editor
from . import webserver


testdata_root = os.path.realpath(
    os.path.join(os.path.dirname(__file__), '..', 'testdata'))
mint_data_path = os.path.join(testdata_root, 'source', 'mint', 'mint.csv')
reconcile_initial_path = os.path.join(testdata_root, 'reconcile', 'test_basic',
                                      '0')


class _AgentWebserverHttpTest(AsyncHTTPTestCase):
    read_only = False

    def get_app(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.journal_path = os.path.join(self.temp_directory.name,
                                         'journal.beancount')
        self.ignored_path = os.path.join(self.temp_directory.name,
                                         'ignore.beancount')
        shutil.copyfile(
            os.path.join(reconcile_initial_path, 'journal.beancount'),
            self.journal_path)
        shutil.copyfile(
            os.path.join(reconcile_initial_path, 'ignore.beancount'),
            self.ignored_path)
        args = webserver.parse_arguments(
            argv=[],
            journal_input=self.journal_path,
            ignored_journal=self.ignored_path,
            default_output=self.journal_path,
            read_only=self.read_only,
            data_sources=[{
                'module': 'beancount_import.source.mint',
                'filename': mint_data_path,
            }],
        )
        self.application = webserver.Application(
            args=args, ioloop=self.io_loop)
        self.api_path = self.application.agent_api_base_path
        return self.application

    def tearDown(self):
        observer = getattr(self.application, 'check_modification_observer', None)
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        super().tearDown()
        self.temp_directory.cleanup()

    @staticmethod
    def _decode(response):
        return json.loads(response.body.decode('utf-8'))

    def _get_json(self, path):
        response = self.fetch(path, raise_error=False)
        return response, self._decode(response)

    def _post_json(self, path, payload, idempotency_key=None):
        headers = {'Content-Type': 'application/json'}
        if idempotency_key is not None:
            headers['Idempotency-Key'] = idempotency_key
        response = self.fetch(
            path,
            method='POST',
            headers=headers,
            body=json.dumps(payload),
            raise_error=False)
        return response, self._decode(response)

    def _wait_until_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response, state = self._get_json(self.api_path + '/state')
            assert response.code == 200, state
            if state.get('status') == 'ready':
                return state
            time.sleep(0.01)
        raise AssertionError('Agent API did not become ready')

    def _get_current(self):
        self._wait_until_ready()
        response, current = self._get_json(self.api_path + '/current')
        assert response.code == 200, current
        assert current['status'] == 'review'
        return current

    @staticmethod
    def _select_merged_candidate(current):
        return next(candidate for candidate in current['candidates']
                    if candidate['kind'] == 'merged')

    def _preview_accept(self, current):
        candidate = self._select_merged_candidate(current)
        payload = {
            'dry_run': True,
            'action': 'accept',
            'revision': current['revision'],
            'candidate_id': candidate['id'],
        }
        response, preview = self._post_json(
            self.api_path + '/decision', payload)
        assert response.code == 200, preview
        assert preview['dry_run'] is True
        assert preview['preview_token']
        return payload, preview


class TestAgentWebserverReadOnly(_AgentWebserverHttpTest):
    read_only = True

    def test_commit_is_forbidden_after_a_valid_preview(self):
        current = self._get_current()
        before = open(self.journal_path, 'rb').read()
        preview_payload, preview = self._preview_accept(current)
        assert open(self.journal_path, 'rb').read() == before

        commit_payload = dict(
            preview_payload, dry_run=False,
            preview_token=preview['preview_token'])
        response, result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='read-only-commit')

        assert response.code == 403
        assert result['error']['code'] == 'read_only'
        assert open(self.journal_path, 'rb').read() == before


class TestAgentWebserverReadWrite(_AgentWebserverHttpTest):
    def test_get_info_state_and_current(self):
        response, info = self._get_json(self.api_path)
        assert response.code == 200
        assert info['version'] == 'v1'
        assert info['mode'] == 'read-write'
        assert info['endpoints']['current'] == self.api_path + '/current'
        assert info['endpoints']['retrain'] == self.api_path + '/retrain'

        state = self._wait_until_ready()
        assert state['status'] == 'ready'
        assert state['read_only'] is False
        assert state['pending_count'] == 3
        assert state['revision']['server_epoch']

        response, current = self._get_json(self.api_path + '/current')
        assert response.code == 200
        assert current['status'] == 'review'
        assert current['pending_index'] == 0
        assert current['pending']['id'] == current['revision']['pending_id']
        assert current['candidate_set_hash'] == current['revision'][
            'candidate_set_hash']
        assert current['candidates']
        candidate = self._select_merged_candidate(current)
        assert candidate['details_url'].endswith(candidate['id'])
        response, details = self._get_json(candidate['details_url'])
        assert response.code == 200, details
        assert details['candidate']['id'] == candidate['id']
        assert details['candidate']['diff']

        response, retrain = self._post_json(
            self.api_path + '/retrain', {
                'revision': current['revision'],
                'dry_run': True,
            })
        assert response.code == 200, retrain
        assert retrain['dry_run'] is True
        assert retrain['training_example_count'] >= 0

        response, auto = self._post_json(
            self.api_path + '/auto-accept', {
                'revision': current['revision'],
                'dry_run': True,
                'max_cases': 10,
            })
        assert response.code == 200, auto
        assert auto['dry_run'] is True
        assert auto['would_accept_current'] is False
        assert 'merged_candidate_requires_review' in auto['current'][
            'recommendation']['blockers']

    def test_preview_does_not_write(self):
        current = self._get_current()
        before = open(self.journal_path, 'rb').read()

        _, preview = self._preview_accept(current)

        assert preview['preview']['diff']
        assert preview['preview']['modified_filenames'] == [
            os.path.realpath(self.journal_path)
        ]
        assert open(self.journal_path, 'rb').read() == before
        response, state = self._get_json(self.api_path + '/state')
        assert response.code == 200
        assert state['pending_count'] == 3
        assert state['revision'] == current['revision']

    def test_auto_accept_request_can_only_tighten_server_safety(self):
        current = self._get_current()
        looser_policies = [
            {'account_probability_threshold': 0.0},
            {'account_margin_threshold': 0.0},
            {'account_min_leaf_samples': 1},
            {'max_used_transactions': 3},
            {'max_modified_transactions': 2},
            {'max_output_files': 2},
            {'require_recognized_value_feature': False},
            {'require_existing_account': False},
            {'require_cleared_match': False},
            {'allow_new_accounts': True},
            {'allow_merged_transactions': True},
        ]
        for policy in looser_policies:
            with self.subTest(policy=policy):
                response, result = self._post_json(
                    self.api_path + '/auto-accept', {
                        'revision': current['revision'],
                        'dry_run': True,
                        'policy': policy,
                    })
                assert response.code == 422
                assert result['error'][
                    'code'] == 'invalid_auto_accept_request'

        response, result = self._post_json(
            self.api_path + '/auto-accept', {
                'revision': current['revision'],
                'dry_run': True,
                'allow_errors': True,
            })
        assert response.code == 422
        assert result['error']['code'] == 'invalid_auto_accept_request'

        response, result = self._post_json(
            self.api_path + '/auto-accept', {
                'revision': current['revision'],
                'dry_run': True,
                'policy': {
                    'account_probability_threshold': 0.999,
                    'max_used_transactions': 1,
                },
            })
        assert response.code == 200, result
        assert result['policy']['account_probability_threshold'] == 0.999
        assert result['policy']['max_used_transactions'] == 1

    def test_default_auto_accept_commit_leaves_fuzzy_merge_for_review(self):
        current = self._get_current()
        journal_before = open(self.journal_path, 'rb').read()
        response, result = self._post_json(
            self.api_path + '/auto-accept', {
                'revision': current['revision'],
                'dry_run': False,
                'max_cases': 10,
            }, idempotency_key='default-fuzzy-review')

        assert response.code == 200, result
        assert result['accepted_count'] == 0
        assert result['accepted'] == []
        assert result['stop_reason'] == 'review_required'
        assert 'merged_candidate_requires_review' in result['next'][
            'recommendation']['blockers']
        assert open(self.journal_path, 'rb').read() == journal_before

    def test_ignore_writes_only_raw_pending_transaction_and_replays_once(self):
        current = self._get_current()
        candidate = next(candidate for candidate in current['candidates']
                         if candidate['kind'] == 'unmerged')
        preview_payload = {
            'dry_run': True,
            'action': 'ignore',
            'revision': current['revision'],
            'candidate_id': candidate['id'],
        }
        response, preview = self._post_json(
            self.api_path + '/decision', preview_payload)
        assert response.code == 200, preview
        assert preview['preview']['modified_filenames'] == [
            os.path.realpath(self.ignored_path)
        ]
        assert 'Expenses:FIXME' in preview['preview']['diff']
        journal_before = open(self.journal_path, 'rb').read()
        ignored_before = open(self.ignored_path, 'rb').read()
        commit_payload = dict(
            preview_payload,
            dry_run=False,
            preview_token=preview['preview_token'])

        response, first_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='ignore-raw-pending')
        assert response.code == 200, first_result
        assert first_result['fully_applied'] is True
        assert first_result['modified_filenames'] == [
            os.path.realpath(self.ignored_path)
        ]
        assert open(self.journal_path, 'rb').read() == journal_before
        ignored_after = open(self.ignored_path, 'rb').read()
        assert ignored_after != ignored_before
        assert ignored_after.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1
        assert b'open Expenses:FIXME' not in ignored_after
        assert any(
            posting['account'] == 'Expenses:FIXME'
            for entry in first_result['new_entries']
            for posting in entry.get('postings', []))

        response, replay_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='ignore-raw-pending')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.ignored_path, 'rb').read() == ignored_after

    def test_preview_commit_and_idempotent_replay_write_once(self):
        current = self._get_current()
        before = open(self.journal_path, 'rb').read()
        preview_payload, preview = self._preview_accept(current)
        commit_payload = dict(
            preview_payload, dry_run=False,
            preview_token=preview['preview_token'])

        response, first_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='accept-current')
        assert response.code == 200, first_result
        assert first_result['applied'] is True
        after_first_commit = open(self.journal_path, 'rb').read()
        assert after_first_commit != before
        assert after_first_commit.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1

        response, replay_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='accept-current')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.journal_path, 'rb').read() == after_first_commit
        assert after_first_commit.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1

    def test_written_decision_returns_cached_applied_receipt_when_response_fails(
            self):
        current = self._get_current()
        preview_payload, preview = self._preview_accept(current)
        commit_payload = dict(
            preview_payload, dry_run=False,
            preview_token=preview['preview_token'])
        original_encoder = webserver.json_encode_beancount_entry

        def fail_encoding(_entry):
            raise RuntimeError('response encoding failed after write')

        webserver.json_encode_beancount_entry = fail_encoding
        try:
            response, first_result = self._post_json(
                self.api_path + '/decision',
                commit_payload,
                idempotency_key='postwrite-response-failure')
        finally:
            webserver.json_encode_beancount_entry = original_encoder

        assert response.code == 200, first_result
        assert first_result['applied'] is True
        assert first_result['postprocess_failed'] is True
        assert first_result['error']['code'] == 'postprocess_failed'
        assert first_result['new_entries'] == []
        assert first_result['new_entries_omitted'] is True
        assert first_result['recovery']['status'] == 'loading'
        after_first_commit = open(self.journal_path, 'rb').read()
        assert after_first_commit.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1

        response, replay_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='postwrite-response-failure')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.journal_path, 'rb').read() == after_first_commit
        recovered_state = self._wait_until_ready()
        assert recovered_state['classifier']['trusted'] is False
        assert recovered_state['classifier']['reason'] == (
            'classifier_not_retrained_after_journal_change')

    def test_written_decision_reports_next_case_failure_as_applied(self):
        current = self._get_current()
        preview_payload, preview = self._preview_accept(current)
        commit_payload = dict(
            preview_payload, dry_run=False,
            preview_token=preview['preview_token'])
        original_get_next = self.application.get_next_candidates
        call_count = 0

        def fail_once(new_pending):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError('next case failed after write')
            return original_get_next(new_pending)

        self.application.get_next_candidates = fail_once
        try:
            response, first_result = self._post_json(
                self.api_path + '/decision',
                commit_payload,
                idempotency_key='postwrite-next-case-failure')
        finally:
            self.application.get_next_candidates = original_get_next

        assert response.code == 200, first_result
        assert first_result['applied'] is True
        assert first_result['postprocess_failed'] is True
        assert first_result['error']['details']['message'] == (
            'next case failed after write')
        assert first_result['recovery']['status'] == 'loading'
        after_first_commit = open(self.journal_path, 'rb').read()
        assert after_first_commit.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1

        response, replay_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='postwrite-next-case-failure')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.journal_path, 'rb').read() == after_first_commit
        self._wait_until_ready()

    def test_post_rename_fsync_failure_returns_complete_applied_receipt(self):
        current = self._get_current()
        preview_payload, preview = self._preview_accept(current)
        commit_payload = dict(
            preview_payload, dry_run=False,
            preview_token=preview['preview_token'])
        original_sync = journal_editor.atomicwrites._sync_directory

        def fail_directory_sync(*args, **kwargs):
            raise RuntimeError('directory fsync failed after rename')

        journal_editor.atomicwrites._sync_directory = fail_directory_sync
        try:
            response, first_result = self._post_json(
                self.api_path + '/decision',
                commit_payload,
                idempotency_key='post-rename-fsync-failure')
        finally:
            journal_editor.atomicwrites._sync_directory = original_sync

        assert response.code == 200, first_result
        assert first_result['applied'] is True
        assert first_result['fully_applied'] is True
        assert first_result['write_status'] == 'complete'
        assert first_result['postprocess_failed'] is True
        assert first_result['partial_write'] is False
        assert first_result['error']['code'] == 'postprocess_failed'
        after_first_commit = open(self.journal_path, 'rb').read()
        assert after_first_commit.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1

        response, replay_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='post-rename-fsync-failure')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.journal_path, 'rb').read() == after_first_commit
        self._wait_until_ready()

    def test_partial_multi_file_decision_is_cached_without_claiming_full_apply(
            self):
        current = self._get_current()
        candidate_summary = self._select_merged_candidate(current)
        _, candidate = webserver.agent_protocol.find_candidate(
            self.application.next_candidates, candidate_summary['id'])
        extra_entry = next(
            entry for entry in candidate.staged_changes.get_all_new_entries()
            if entry is not None)
        candidate.staged_changes.add_entry(extra_entry, self.ignored_path)
        response, current = self._get_json(self.api_path + '/current')
        assert response.code == 200, current
        preview_payload, preview = self._preview_accept(current)
        commit_payload = dict(
            preview_payload, dry_run=False,
            preview_token=preview['preview_token'])
        loaded_reconciler = self.application._require_loaded_reconciler()
        original_write = loaded_reconciler.editor._write_file_changes_result

        def fail_ignored_write(filename, result):
            if os.path.realpath(filename) == os.path.realpath(
                    self.ignored_path):
                raise RuntimeError('ignored journal failed before write')
            return original_write(filename, result)

        loaded_reconciler.editor._write_file_changes_result = fail_ignored_write
        journal_before = open(self.journal_path, 'rb').read()
        ignored_before = open(self.ignored_path, 'rb').read()
        try:
            response, first_result = self._post_json(
                self.api_path + '/decision',
                commit_payload,
                idempotency_key='partial-multi-file-write')
        finally:
            loaded_reconciler.editor._write_file_changes_result = original_write

        assert response.code == 200, first_result
        assert first_result['applied'] is True
        assert first_result['fully_applied'] is False
        assert first_result['write_status'] == 'partial'
        assert first_result['partial_write'] is True
        assert first_result['postprocess_failed'] is False
        assert first_result['error']['code'] == 'partial_write'
        assert first_result['recovery']['status'] == 'loading'
        assert first_result['next'] is None
        assert first_result['applied_filenames'] == [
            os.path.realpath(self.journal_path)
        ]
        assert first_result['intended_filenames'] == [
            os.path.realpath(self.journal_path),
            os.path.realpath(self.ignored_path),
        ]
        assert first_result['new_entries']
        assert all(
            entry['meta']['filename'] == os.path.realpath(self.journal_path)
            for entry in first_result['new_entries'])
        journal_after = open(self.journal_path, 'rb').read()
        ignored_after = open(self.ignored_path, 'rb').read()
        assert journal_after != journal_before
        assert ignored_after == ignored_before

        response, replay_result = self._post_json(
            self.api_path + '/decision',
            commit_payload,
            idempotency_key='partial-multi-file-write')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.journal_path, 'rb').read() == journal_after
        assert open(self.ignored_path, 'rb').read() == ignored_after
        self._wait_until_ready()

    def test_auto_accept_reports_postprocess_failure_as_applied(self):
        self.application.agent_default_policy = (
            webserver.agent_protocol.AutoAcceptPolicy(
                allow_merged_transactions=True))
        current = self._get_current()
        loaded_reconciler = self.application._require_loaded_reconciler()

        def fail_postprocess(_entries):
            raise RuntimeError('posting index update failed after write')

        loaded_reconciler._add_uncleared_postings_from = fail_postprocess
        payload = {
            'revision': current['revision'],
            'dry_run': False,
            'max_cases': 1,
        }
        response, first_result = self._post_json(
            self.api_path + '/auto-accept',
            payload,
            idempotency_key='auto-postwrite-failure')

        assert response.code == 200, first_result
        assert first_result['accepted_count'] == 1
        assert first_result['stop_reason'] == 'postprocess_failed'
        assert first_result['postprocess_failed'] is True
        assert first_result['accepted'][0]['applied'] is True
        assert first_result['accepted'][0]['postprocess_failed'] is True
        assert first_result['accepted'][0]['error'][
            'code'] == 'postprocess_failed'
        assert first_result['recovery']['status'] == 'loading'
        assert first_result['next'] is None
        after_first_commit = open(self.journal_path, 'rb').read()
        assert after_first_commit.count(
            b'2013-11-27 * "CR CARD PAYMENT ALEXANDRIA VA"') == 1

        response, replay_result = self._post_json(
            self.api_path + '/auto-accept',
            payload,
            idempotency_key='auto-postwrite-failure')
        assert response.code == 200, replay_result
        assert replay_result == first_result
        assert open(self.journal_path, 'rb').read() == after_first_commit
        recovered_state = self._wait_until_ready()
        assert recovered_state['classifier']['trusted'] is False
        assert recovered_state['classifier']['reason'] == (
            'classifier_not_retrained_after_journal_change')

    def test_stale_revision_returns_conflict(self):
        current = self._get_current()
        candidate = self._select_merged_candidate(current)
        stale_revision = dict(current['revision'])
        stale_revision['candidate_set_hash'] = 'stale-candidate-set'
        before = open(self.journal_path, 'rb').read()

        response, result = self._post_json(
            self.api_path + '/decision', {
                'dry_run': True,
                'action': 'accept',
                'revision': stale_revision,
                'candidate_id': candidate['id'],
            })

        assert response.code == 409
        assert result['error']['code'] == 'stale_state'
        assert result['error']['details']['current_revision'] == current[
            'revision']
        assert open(self.journal_path, 'rb').read() == before

    def test_external_journal_change_returns_structured_conflict(self):
        current = self._get_current()
        candidate = self._select_merged_candidate(current)
        observer = self.application.check_modification_observer
        observer.stop()
        observer.join(timeout=5)
        self.application.check_modification_observer = None
        with open(self.journal_path, 'a', encoding='utf-8') as f:
            f.write('\n; externally modified\n')

        response, result = self._post_json(
            self.api_path + '/decision', {
                'dry_run': True,
                'action': 'accept',
                'revision': current['revision'],
                'candidate_id': candidate['id'],
            })

        assert response.code == 409
        assert result['error']['code'] == 'journal_modified'
        assert os.path.realpath(self.journal_path) in result['error'][
            'details']['modified_filenames']

    def test_unexpected_agent_error_is_json(self):
        self._wait_until_ready()
        original = self.application.get_agent_state

        def fail():
            raise RuntimeError('test failure')

        self.application.get_agent_state = fail
        try:
            response, result = self._get_json(self.api_path + '/state')
        finally:
            self.application.get_agent_state = original

        assert response.code == 500
        assert response.headers['Content-Type'].startswith('application/json')
        assert result['error']['code'] == 'internal_error'
