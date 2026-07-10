#!/usr/bin/env python3

from typing import Tuple, Optional, List, Dict, Any, Mapping
import argparse
import binascii
import datetime
import hashlib
import hmac
import time
import io
import collections
import sys
import logging
import traceback
import pdb
import importlib.resources
import json
import os
import tempfile
import uuid
import webbrowser

import atomicwrites
import tornado.ioloop
import tornado.web
import tornado.httpserver
import tornado.netutil
import tornado.websocket

from beancount.core.data import Transaction, Posting
from beancount.core.number import MISSING, Decimal, D
import beancount.parser.printer

import watchdog.events
import watchdog.observers

from . import reconcile
from . import agent as agent_protocol

from . import training
from . import matching
from .source import Source, InvalidSourceReference


class AgentApiError(Exception):
    def __init__(self,
                 status: int,
                 code: str,
                 message: str,
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def init_tornado_asyncio():
    '''
    Python 3.8+ on Windows requires this patch for Tornado loop to work, confirmed they won't fix this internally
    '''
    if sys.platform == 'win32':
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def json_encode_beancount_entry(x):
    if x is None:
        return None
    if isinstance(x, Transaction):
        x = x._replace(postings=[y._asdict() for y in x.postings])
    result = x._asdict()
    if result['meta'] is not None:
        result['meta'] = result['meta'].copy()
        result['meta'].pop('__tolerances__', None)
    return result


def format_transaction(transaction: Transaction) -> str:
    printer = beancount.parser.printer.EntryPrinter()
    return printer(transaction)


def format_posting(posting: Posting, indent: str = '  ') -> str:
    printer = beancount.parser.printer.EntryPrinter()
    flag_account, position_str, weight_str = printer.render_posting_strings(
        posting)
    oss = io.StringIO()
    oss.write(('%s%s  %s' % (indent, flag_account, position_str)).rstrip() +
              '\n')
    if posting.meta:
        printer.write_metadata(posting.meta, oss, '  ' + indent)
    return oss.getvalue()


def convert_uncleared(p: Tuple[Transaction, Posting]):
    return {
        'transaction': json_encode_beancount_entry(p[0]),
        'posting': json_encode_beancount_entry(p[1]),
        'transaction_formatted': format_transaction(p[0]),
    }


def convert_uncleared_list(
        uncleared_entries: List[Tuple[Transaction, Posting]]) -> List[Any]:
    return [convert_uncleared(p) for p in uncleared_entries]


def convert_invalid_reference(ref: Tuple[Source, InvalidSourceReference]):
    def convert_transaction_posting_pair(
            pair: Tuple[Transaction, Optional[Posting]]) -> dict:
        transaction, posting = pair
        result = {
            'transaction': json_encode_beancount_entry(transaction),
            'posting': json_encode_beancount_entry(posting)
        }
        result['transaction_formatted'] = format_transaction(transaction)
        if posting is not None:
            result['posting_formatted'] = format_posting(posting, indent='')
        return result

    return {
        'num_extras':
        ref[1].num_extras,
        'source':
        ref[0].name,
        'transaction_posting_pairs': [
            convert_transaction_posting_pair(p)
            for p in ref[1].transaction_posting_pairs
        ],
    }


def convert_invalid_references(entries):
    return [convert_invalid_reference(ref) for ref in entries]


def json_encode_candidates(candidates: reconcile.Candidates):
    result = {}  # type: Dict[str, Any]

    def encode_used_transaction(transaction: Transaction,
                                index: Optional[int]) -> dict:
        if index is None:
            pending = None
            info = None
            source = None
        else:
            pending = candidates.pending_data[index]
            info = pending.info
            source = None if pending.source is None else pending.source.name
        return {
            'formatted': format_transaction(transaction),
            'entry': json_encode_beancount_entry(transaction),
            'pending_index': index,
            'info': info,
            'source': source,
        }

    result['used_transactions'] = [
        encode_used_transaction(transaction, index)
        for transaction, index in candidates.used_transactions
    ]
    result['candidates'] = candidates.candidates
    result['date'] = candidates.date
    result['number'] = candidates.number
    return result


def json_encode_candidate(obj: reconcile.Candidate):
    change_sets, _, _ = obj.staged_changes_with_unique_account_names.get_diff()
    _, _, new_entries = obj.staged_changes.get_diff()
    return dict(
        change_sets=change_sets,
        used_transaction_ids=obj.used_transaction_ids,
        substituted_accounts=obj.substituted_accounts or [],
        original_transaction_properties=obj.original_transaction_properties,
        new_entries=[json_encode_beancount_entry(x) for x in new_entries],
        associated_data=[x.__dict__ for x in obj.associated_data],
    )


def json_encode_pending_candidate(pending: reconcile.PendingEntry):
    return {
        'date': pending.date,
        'formatted': pending.formatted,
        'entries': [json_encode_beancount_entry(x) for x in pending.entries],
        'info': pending.info,
        'source': None if pending.source is None else pending.source.name,
        'id': pending.id
    }


def json_convert_pending_list(pending_data: List[reconcile.PendingEntry]):
    return [json_encode_pending_candidate(x) for x in pending_data]


def convert_errors(x):
    return x


def json_encode_state(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (frozenset, set)):
        return list(obj)
    if isinstance(obj, datetime.date):
        return obj.strftime('%Y-%m-%d')
    if isinstance(obj, reconcile.Candidate):
        return json_encode_candidate(obj)
    if isinstance(obj, reconcile.Candidates):
        return json_encode_candidates(obj)


class StaticHandler(tornado.web.RequestHandler):
    def get(self, name: str):
        if name == '':
            name = 'index.html'
        if name.endswith('.html'):
            content_type = 'text/html'
        elif name.endswith('.js'):
            content_type = 'application/javascript'
        elif name.endswith('.css'):
            content_type = 'text/css'
        elif name.endswith('.map'):
            content_type = 'application/json'
        else:
            content_type = 'application/octet-stream'
        self.set_header('Content-Type', content_type)
        contents = (importlib.resources.files(__package__)
                    / 'frontend_dist' / name).read_bytes()
        if name == 'app.js':
            contents = contents.replace(
                self.application.secret_key_pattern.encode(),  # type: ignore
                self.application.secret_key.encode())  # type: ignore
        self.write(contents)


data_convert_functions = {
    'errors': convert_errors,
    'uncleared': convert_uncleared_list,
    'invalid': convert_invalid_references,
    'pending': json_convert_pending_list,
}


class GetDataHandler(tornado.web.RequestHandler):
    def get(self, data_type, generation, begin_index, end_index):
        begin_index = int(begin_index)
        end_index = int(end_index)
        info = self.application.current_state.get(data_type)
        if info is None or str(info[0]) != generation:
            self.set_status(404)
            return self.finish('Current generation not specified.')
        if begin_index < 0 or begin_index > end_index or end_index > info[1]:
            self.set_status(400)
            return self.finish('Invalid index specified.')
        try:
            value = getattr(self.application,
                            'current_%s' % data_type)[begin_index:end_index]
            converted_value = data_convert_functions[data_type](value)
            json_encoding = json.dumps(
                converted_value, default=json_encode_state)
            self.set_header('Content-Type', 'application/json')
            self.write(json_encoding.encode())
        except:
            self.set_status(500)
            import traceback
            traceback.print_exc()
            return self.finish('Error writing data')


class ChangeCandidateHandler(tornado.web.RequestHandler):
    def post(self):
        msg = json.loads(self.request.body)
        self.application.handle_change_candidate(msg)
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(None).encode())


class SelectCandidateHandler(tornado.web.RequestHandler):
    def post(self):
        if self.application.read_only:
            self.set_status(403)
            self.set_header('Content-Type', 'application/json')
            return self.finish(
                json.dumps({
                    'error': {
                        'code': 'read_only',
                        'message': 'Candidate writes are disabled in read-only mode.'
                    }
                }).encode())
        msg = json.loads(self.request.body)
        new_entries = self.application.handle_select_candidate(msg) or []
        self.set_header('Content-Type', 'application/json')
        self.write(
            json.dumps(
                [json_encode_beancount_entry(x) for x in new_entries],
                default=json_encode_state).encode())


class SkipHandler(tornado.web.RequestHandler):
    def post(self):
        msg = json.loads(self.request.body)
        self.application.handle_skip(msg)
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(None).encode())


class RetrainHandler(tornado.web.RequestHandler):
    def post(self):
        self.application.retrain()
        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(None).encode())


class AgentApiHandler(tornado.web.RequestHandler):
    def write_json(self, value: Any, status: int = 200) -> None:
        self.set_status(status)
        self.set_header('Content-Type', 'application/json')
        self.write(
            json.dumps(value, default=json_encode_state,
                       sort_keys=True).encode())

    def read_json(self) -> Dict[str, Any]:
        if not self.request.body:
            return {}
        try:
            value = json.loads(self.request.body)
        except json.JSONDecodeError as e:
            raise AgentApiError(400, 'invalid_json', str(e))
        if not isinstance(value, dict):
            raise AgentApiError(400, 'invalid_request',
                                'Expected a JSON object request body.')
        return value

    def handle_api_error(self, error: AgentApiError) -> None:
        self.write_json(
            {
                'error': {
                    'code': error.code,
                    'message': error.message,
                    'details': error.details,
                }
            }, status=error.status)

    def write_error(self, status_code: int, **kwargs) -> None:
        exception = None
        exc_info = kwargs.get('exc_info')
        if exc_info is not None:
            exception = exc_info[1]
        if isinstance(exception, AgentApiError):
            self.handle_api_error(exception)
            return
        self.write_json({
            'error': {
                'code': 'internal_error',
                'message': 'The agent API request failed unexpectedly.',
                'details': {},
            }
        }, status=status_code)


class AgentApiInfoHandler(AgentApiHandler):
    def get(self):
        self.write_json(self.application.get_agent_api_info())


class AgentStateHandler(AgentApiHandler):
    def get(self):
        try:
            self.write_json(self.application.get_agent_state())
        except AgentApiError as e:
            self.handle_api_error(e)


class AgentPendingHandler(AgentApiHandler):
    def get(self):
        try:
            start = int(self.get_query_argument('start', '0'))
            limit = int(self.get_query_argument('limit', '50'))
            self.write_json(self.application.get_agent_pending(start, limit))
        except ValueError as e:
            self.handle_api_error(
                AgentApiError(400, 'invalid_pagination', str(e)))
        except AgentApiError as e:
            self.handle_api_error(e)


class AgentCurrentHandler(AgentApiHandler):
    def get(self):
        try:
            self.write_json(self.application.get_agent_current_case())
        except AgentApiError as e:
            self.handle_api_error(e)


class AgentCandidateHandler(AgentApiHandler):
    def get(self, candidate_id: str):
        try:
            self.write_json(
                self.application.get_agent_candidate(candidate_id))
        except KeyError:
            self.handle_api_error(
                AgentApiError(404, 'candidate_not_found',
                              'The candidate is not in the current revision.'))
        except AgentApiError as e:
            self.handle_api_error(e)


class AgentDecisionHandler(AgentApiHandler):
    def post(self):
        try:
            payload = self.read_json()
            idempotency_key = self.request.headers.get(
                'Idempotency-Key', payload.get('idempotency_key'))
            self.write_json(
                self.application.handle_agent_decision(
                    payload, idempotency_key=idempotency_key))
        except reconcile.ReadOnlyError as e:
            self.handle_api_error(
                AgentApiError(403, 'read_only', str(e)))
        except (IndexError, TypeError, ValueError) as e:
            self.handle_api_error(
                AgentApiError(422, 'invalid_decision', str(e)))
        except AgentApiError as e:
            self.handle_api_error(e)


class AgentAutoAcceptHandler(AgentApiHandler):
    def post(self):
        try:
            payload = self.read_json()
            idempotency_key = self.request.headers.get(
                'Idempotency-Key', payload.get('idempotency_key'))
            self.write_json(
                self.application.handle_agent_auto_accept(
                    payload, idempotency_key=idempotency_key))
        except reconcile.ReadOnlyError as e:
            self.handle_api_error(
                AgentApiError(403, 'read_only', str(e)))
        except (IndexError, TypeError, ValueError) as e:
            self.handle_api_error(
                AgentApiError(422, 'invalid_auto_accept_request', str(e)))
        except AgentApiError as e:
            self.handle_api_error(e)


class AgentRetrainHandler(AgentApiHandler):
    def post(self):
        try:
            payload = self.read_json()
            idempotency_key = self.request.headers.get(
                'Idempotency-Key', payload.get('idempotency_key'))
            self.write_json(
                self.application.handle_agent_retrain(
                    payload, idempotency_key=idempotency_key))
        except (IndexError, TypeError, ValueError) as e:
            self.handle_api_error(
                AgentApiError(422, 'invalid_retrain_request', str(e)))
        except AgentApiError as e:
            self.handle_api_error(e)


class WebSocketHandler(tornado.websocket.WebSocketHandler):
    def open(self, *args):
        self.application.socket_clients.add(self)
        try:
            self.set_nodelay(True)
        except:
            # This results in an assertion error in Tornado 6.0.  Simply ignore
            # it since the nodelay option isn't critical.
            pass
        self.prev_state = dict()
        self.prev_state_generation = dict()
        self.watched_files = set()
        try:
            self.send_state_update()
        except:
            traceback.print_exc()

    def on_message(self, message):
        try:
            message = json.loads(message)
            f = getattr(self, 'on_message_%s' % message['type'], None)
            if f is None:
                raise TypeError('Invalid message type: %r' % message)
            self.application.ioloop.add_callback(f, message['value'])
        except:
            traceback.print_exc()
            pdb.pm()

    def on_close(self):
        print('closed, code = %r, reason = %r' % (self.close_code,
                                                  self.close_reason))
        self.application.socket_clients.remove(self)

    def send_state_update(self):
        try:
            update = dict()
            new_state = self.application.current_state
            new_state_generation = self.application.current_state_generation
            prev_state = self.prev_state
            prev_state_generation = self.prev_state_generation
            for k, v in new_state.items():
                generation = new_state_generation[k]
                if prev_state_generation.get(k) != generation:
                    prev_state_generation[k] = generation
                    update[k] = v
            for k, v in prev_state.items():
                if k not in new_state:
                    update[k] = None
            if len(update) > 0:
                self.write_message(
                    json.dumps(
                        dict(type='state_update', state=update),
                        default=json_encode_state))
                prev_state.update(update)
        except:
            traceback.print_exc()
            pdb.post_mortem()

    def send_file_update(self, filename, contents):
        try:
            self.write_message(
                json.dumps(
                    dict(
                        type='file_contents', path=filename,
                        contents=contents)))
        except:
            traceback.print_exc()

    def on_message_watch_file(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8', newline='\n') as f:
                contents = f.read()
            self.send_file_update(filename, contents)
            if filename in self.watched_files:
                return
            self.application.watched_files.setdefault(filename, set()).add(self)
        except:
            traceback.print_exc()

    def on_message_unwatch_file(self, filename):
        try:
            if filename not in self.watched_files:
                return
            filename_watchers = self.application.watched_files[filename]
            del filename_watchers[self]
            if not filename_watchers:
                del self.application.watched_files[filename]
        except:
            traceback.print_exc()

    def on_message_get_file_contents(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8', newline='\n') as f:
                contents = f.read()
            self.write_message(
                json.dumps(
                    dict(
                        type='file_contents', path=filename,
                        contents=contents)))
        except:
            traceback.print_exc()

    def on_message_set_file_contents(self, msg):
        try:
            if self.application.read_only:
                raise reconcile.ReadOnlyError(
                    'Journal editing is disabled in read-only mode.')
            filename = msg['filename']
            contents = msg['contents']
            with atomicwrites.atomic_write(filename, overwrite=True) as f:
                f.write(contents)
        except:
            traceback.print_exc()


class GetFileHandler(tornado.web.RequestHandler):
    def get(self):
        path = self.get_argument('path')
        content_type = self.get_argument('content_type')
        try:
            with open(path, 'rb') as f:
                contents = f.read()
            self.set_header('Content-Type', content_type)
            self.write(contents)
        except:
            self.set_status(404)
            self.finish('File not found')


class JournalModificationHandler(watchdog.events.FileSystemEventHandler):
    def __init__(self, application):
        super(JournalModificationHandler, self).__init__()
        self.application = application

    def on_any_event(self, event):
        self.application.ioloop.add_callback(self.application.check_modification)


class Application(tornado.web.Application):
    def __init__(self, args, ioloop, **kwargs):

        # Secret key that prevents cross-origin access to the websocket.
        # The key is contained in the html response.
        secret_key = 'BEANCOUNT_IMPORT_SECRET_KEY_%s' % binascii.hexlify(
            os.urandom(20)).decode()
        self.secret_key_pattern = 'BEANCOUNT_IMPORT_SECRET_KEY_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX'
        self.secret_key = secret_key
        self.ioloop = ioloop
        self.read_only = bool(args.read_only)
        self.agent_allow_errors = bool(args.agent_auto_accept_with_errors)
        self.agent_allow_invalid_references = bool(
            args.agent_auto_accept_with_invalid_references)
        self.agent_api_base_path = '/%s/api/v1' % secret_key
        self.agent_server_epoch = uuid.uuid4().hex
        self.agent_preview_key = os.urandom(32)
        self.agent_idempotency_results = collections.OrderedDict()
        self.agent_idempotency_limit = 1000
        self.agent_default_policy = agent_protocol.AutoAcceptPolicy.from_mapping(
            {
                'account_probability_threshold':
                args.agent_account_probability_threshold,
                'account_margin_threshold':
                args.agent_account_margin_threshold,
                'account_min_leaf_samples':
                args.agent_account_min_leaf_samples,
            })
        super().__init__([
            (r'/(|index\.html|app\.js|app\.js\.map|app\.css|app\.css\.map)',
             StaticHandler),
            (r'/%s/(errors|pending|invalid|uncleared)/([^/]*)/(\d+)-(\d+)' %
             secret_key, GetDataHandler),
            (r'/%s/websocket' % secret_key, WebSocketHandler),
            (r'/%s/get_file' % secret_key, GetFileHandler),
            (r'/%s/change_candidate' % secret_key, ChangeCandidateHandler),
            (r'/%s/select_candidate' % secret_key, SelectCandidateHandler),
            (r'/%s/skip' % secret_key, SkipHandler),
            (r'/%s/retrain' % secret_key, RetrainHandler),
            (r'/%s/api/v1' % secret_key, AgentApiInfoHandler),
            (r'/%s/api/v1/state' % secret_key, AgentStateHandler),
            (r'/%s/api/v1/pending' % secret_key, AgentPendingHandler),
            (r'/%s/api/v1/current' % secret_key, AgentCurrentHandler),
            (r'/%s/api/v1/current/candidates/([0-9a-f]+)' % secret_key,
             AgentCandidateHandler),
            (r'/%s/api/v1/decision' % secret_key, AgentDecisionHandler),
            (r'/%s/api/v1/auto-accept' % secret_key,
             AgentAutoAcceptHandler),
            (r'/%s/api/v1/retrain' % secret_key, AgentRetrainHandler),
        ], **kwargs)
        self.socket_clients = set()
        self.watched_files = dict()
        self.current_state = dict()
        self.current_state_generation = dict()
        self.generation = 0
        self.skip_ids = None

        self.log_status('Initializing')

        self.check_modification_observer = None
        self.reconciler = reconcile.Reconciler(
            journal_path=args.journal_input,
            ignore_path=args.ignored_journal,
            log_status=self.log_status,
            options=vars(args))
        self.reset()

    def next_generation(self):
        generation = self.generation
        self.generation += 1
        return generation

    def _require_loaded_reconciler(self) -> reconcile.LoadedReconciler:
        if not self.reconciler.loaded_future.done():
            raise AgentApiError(
                503, 'loading', 'The journal and data sources are still loading.',
                {'retry_after_seconds': 1})
        try:
            return self.reconciler.loaded_future.result()
        except Exception as e:
            raise AgentApiError(500, 'load_failed', str(e))

    def get_agent_api_info(self) -> Dict[str, Any]:
        base = self.agent_api_base_path
        return {
            'name': 'beancount-import agent reconciliation API',
            'version': 'v1',
            'mode': 'read-only' if self.read_only else 'read-write',
            'workflow': [
                'GET current',
                'POST decision with dry_run=true',
                'POST the exact decision with preview_token and Idempotency-Key',
            ],
            'endpoints': {
                'state': base + '/state',
                'pending': base + '/pending?start=0&limit=50',
                'current': base + '/current',
                'candidate_details': base +
                '/current/candidates/{candidate_id}',
                'decision': base + '/decision',
                'auto_accept': base + '/auto-accept',
                'retrain': base + '/retrain',
            },
            'default_policy': self.agent_default_policy.to_dict(),
            'safety': {
                'journal_writes_require_preview': True,
                'state_changes_require_idempotency_key': True,
                'idempotency_scope': 'current server process',
                'post_write_failures_report_applied': True,
                'partial_multi_file_writes_are_explicit': True,
                'fuzzy_merged_auto_accept_default': False,
                'request_policy_can_only_tighten': True,
                'auto_accept_with_errors': self.agent_allow_errors,
                'auto_accept_with_invalid_references':
                self.agent_allow_invalid_references,
                'durable_write_ahead_log': False,
                'multi_file_commits_are_globally_atomic': False,
            },
        }

    def get_agent_state(self) -> Dict[str, Any]:
        if not self.reconciler.loaded_future.done():
            return {
                'status': 'loading',
                'message': self.current_state.get('message'),
                'read_only': self.read_only,
            }
        loaded_reconciler = self._require_loaded_reconciler()
        return {
            'status': ('complete' if self.next_candidates is None else 'ready'),
            'read_only': self.read_only,
            'pending_count': len(loaded_reconciler.pending_data),
            'error_count': len(loaded_reconciler.errors),
            'blocking_error_count': sum(
                error[0] == 'error' for error in loaded_reconciler.errors),
            'invalid_reference_count': len(
                loaded_reconciler.invalid_references),
            'uncleared_posting_count': len(
                loaded_reconciler.uncleared_postings),
            'classifier': {
                'available': loaded_reconciler.classifier is not None,
                'trusted': loaded_reconciler.classifier_model_trusted,
                'reason': loaded_reconciler.classifier_model_reason,
                'training_fingerprint':
                loaded_reconciler.classifier_training_fingerprint,
            },
            'revision': self.get_agent_revision(),
        }

    def get_agent_pending(self, start: int, limit: int) -> Dict[str, Any]:
        loaded_reconciler = self._require_loaded_reconciler()
        if start < 0:
            raise ValueError('start cannot be negative')
        if limit < 1 or limit > 200:
            raise ValueError('limit must be between 1 and 200')
        end = min(len(loaded_reconciler.pending_data), start + limit)
        return {
            'start': start,
            'end': end,
            'total': len(loaded_reconciler.pending_data),
            'items': [
                agent_protocol.encode_pending(pending)
                for pending in loaded_reconciler.pending_data[start:end]
            ],
        }

    def get_agent_revision(self) -> Dict[str, Any]:
        pending_generation = self.current_state.get('pending')
        pending_index = self.current_state.get('pending_index')
        pending_id = None
        candidate_set_hash = None
        candidates_generation = self.current_state.get(
            'candidates_generation')
        if self.next_candidates is not None and pending_index is not None:
            pending_id = self.next_candidates.pending_data[pending_index].id
            candidate_set_hash = agent_protocol.get_candidate_set_hash(
                self.next_candidates)
        return {
            'server_epoch': self.agent_server_epoch,
            'pending_generation': (None if pending_generation is None else
                                   pending_generation[0]),
            'candidates_generation': candidates_generation,
            'pending_index': pending_index,
            'pending_id': pending_id,
            'candidate_set_hash': candidate_set_hash,
        }

    def _agent_global_issues(self,
                             loaded_reconciler: reconcile.LoadedReconciler
                             ) -> Dict[str, Any]:
        blocking_errors = [
            error for error in loaded_reconciler.errors if error[0] == 'error'
        ]
        return {
            'blocking_error_count': len(blocking_errors),
            'invalid_reference_count': len(
                loaded_reconciler.invalid_references),
            'errors': loaded_reconciler.errors[:20],
            'errors_truncated': len(loaded_reconciler.errors) > 20,
        }

    def _ensure_agent_journal_unmodified(
            self, loaded_reconciler: reconcile.LoadedReconciler) -> None:
        modified_filenames = sorted(
            loaded_reconciler.editor.check_any_journal_modification())
        if not modified_filenames:
            return
        self.reconciler.reload_journal()
        self.reset()
        raise AgentApiError(
            409, 'journal_modified',
            'The journal changed after this reconciliation state was loaded.', {
                'modified_filenames': modified_filenames,
                'retry_from': self.agent_api_base_path + '/state',
            })

    def get_agent_current_case(
            self,
            policy: Optional[
                agent_protocol.AutoAcceptPolicy] = None) -> Dict[str, Any]:
        loaded_reconciler = self._require_loaded_reconciler()
        self._ensure_agent_journal_unmodified(loaded_reconciler)
        revision = self.get_agent_revision()
        if self.next_candidates is None:
            return {
                'status': 'complete',
                'revision': revision,
                'global_issues': self._agent_global_issues(loaded_reconciler),
            }
        pending_index = self.current_state.get('pending_index')
        if pending_index is None:
            raise AgentApiError(503, 'case_not_ready',
                                'The next candidate set is not ready.')
        if policy is None:
            policy = self.agent_default_policy
        result = agent_protocol.encode_case(
            self.next_candidates,
            pending_index,
            sorted(loaded_reconciler.editor.accounts.keys()),
            policy)
        for candidate in result['candidates']:
            candidate['details_url'] = (
                self.agent_api_base_path + '/current/candidates/' +
                candidate['id'])
        result.update({
            'status': 'review',
            'revision': revision,
            'global_issues': self._agent_global_issues(loaded_reconciler),
        })
        return result

    def get_agent_candidate(self, candidate_id: str) -> Dict[str, Any]:
        loaded_reconciler = self._require_loaded_reconciler()
        if self.next_candidates is None:
            raise AgentApiError(409, 'complete',
                                'There is no current pending transaction.')
        self._ensure_agent_journal_unmodified(loaded_reconciler)
        index, candidate = agent_protocol.find_candidate(
            self.next_candidates, candidate_id)
        return {
            'revision': self.get_agent_revision(),
            'candidate': agent_protocol.encode_candidate(
                candidate,
                index,
                sorted(loaded_reconciler.editor.accounts.keys()),
                include_diff=True),
        }

    def _validate_agent_revision(self, revision: Any) -> Dict[str, Any]:
        if not isinstance(revision, dict):
            raise AgentApiError(400, 'revision_required',
                                'The exact revision from GET current is required.')
        current = self.get_agent_revision()
        required_keys = (
            'server_epoch', 'pending_generation', 'candidates_generation',
            'pending_index', 'pending_id', 'candidate_set_hash')
        if any(revision.get(key) != current.get(key) for key in required_keys):
            raise AgentApiError(
                409, 'stale_state',
                'The pending transaction or candidate set has changed.', {
                    'current_revision': current,
                    'retry_from': self.agent_api_base_path + '/current',
                })
        return current

    def _agent_request_hash(self, payload: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload,
                       default=json_encode_state,
                       sort_keys=True,
                       separators=(',', ':')).encode('utf-8')).hexdigest()

    def _get_idempotent_response(
            self, idempotency_key: Optional[str], request_hash: str
    ) -> Optional[Dict[str, Any]]:
        if (not isinstance(idempotency_key, str) or not idempotency_key or
                len(idempotency_key) > 200):
            raise AgentApiError(
                400, 'idempotency_key_required',
                'A 1-200 character Idempotency-Key header is required for commits.'
            )
        existing = self.agent_idempotency_results.get(idempotency_key)
        if existing is None:
            return None
        existing_hash, response = existing
        if existing_hash != request_hash:
            raise AgentApiError(
                409, 'idempotency_conflict',
                'The Idempotency-Key was already used for another request.')
        return response

    def _store_idempotent_response(self, idempotency_key: str,
                                   request_hash: str,
                                   response: Dict[str, Any]) -> None:
        self.agent_idempotency_results[idempotency_key] = (request_hash,
                                                           response)
        self.agent_idempotency_results.move_to_end(idempotency_key)
        while len(self.agent_idempotency_results) > self.agent_idempotency_limit:
            self.agent_idempotency_results.popitem(last=False)

    def _validate_agent_response(self,
                                 response: Dict[str, Any]) -> Dict[str, Any]:
        """Preflights serialization before an idempotency receipt is stored."""
        json.dumps(response, default=json_encode_state, sort_keys=True)
        return response

    @staticmethod
    def _describe_agent_exception(error: Exception) -> Dict[str, str]:
        try:
            message = str(error)
        except Exception:
            message = '<exception message unavailable>'
        return {
            'type': type(error).__name__,
            'message': message,
        }

    @staticmethod
    def _encode_agent_entries_best_effort(entries):
        try:
            return ([json_encode_beancount_entry(x) for x in entries], False)
        except Exception:
            traceback.print_exc()
            return ([], True)

    def _recover_after_agent_write(
            self, modified_filenames: List[str]) -> Dict[str, Any]:
        """Reloads disk state after a write whose in-memory follow-up failed."""
        self._notify_modified_files(modified_filenames)
        try:
            self.reconciler.reload_journal(
                classifier_untrusted_reason=
                'classifier_not_retrained_after_journal_change')
            self.reset()
            return {
                'status': 'loading',
                'poll': self.agent_api_base_path + '/state',
            }
        except Exception as e:
            traceback.print_exc()
            self.next_candidates = None
            self.set_state(candidates=None, pending_index=None)
            return {
                'status': 'reload_failed',
                'poll': self.agent_api_base_path + '/state',
                'error': self._describe_agent_exception(e),
            }

    def _make_agent_decision_postprocess_failure(
            self, action: str, candidate_id: str,
            result: reconcile.AcceptCandidateResult,
            error: Exception) -> Dict[str, Any]:
        new_entries, entries_omitted = self._encode_agent_entries_best_effort(
            result.new_entries)
        fully_applied = result.fully_written
        error_code = ('postprocess_failed' if fully_applied else
                      'partial_write')
        if fully_applied:
            message = (
                'Journal files were written, but server post-processing '
                'failed. Do not repeat this decision with a new key.')
        else:
            message = (
                'Only part of the multi-file journal change was written. '
                'Manual journal repair is required; do not retry with a new '
                'key.')
        response = {
            'dry_run': False,
            'action': action,
            'applied': bool(result.modified_filenames),
            'fully_applied': fully_applied,
            'write_status': 'complete' if fully_applied else 'partial',
            'postprocess_failed': fully_applied,
            'partial_write': not fully_applied,
            'candidate_id': candidate_id,
            'modified_filenames': result.modified_filenames,
            'applied_filenames': result.modified_filenames,
            'intended_filenames': result.intended_filenames,
            'new_entries': new_entries,
            'new_entries_omitted': entries_omitted,
            'next': None,
            'error': {
                'code': error_code,
                'message': message,
                'details': self._describe_agent_exception(error),
            },
            'recovery': self._recover_after_agent_write(
                result.modified_filenames),
        }
        return self._validate_agent_response(response)

    def _make_preview_token(self, revision: Mapping[str, Any],
                            candidate_id: str, action: str,
                            changes: Mapping[str, Any],
                            preview: Mapping[str, Any]) -> str:
        payload = {
            'revision': revision,
            'candidate_id': candidate_id,
            'action': action,
            'changes': changes,
            'preview': preview,
        }
        message = json.dumps(
            payload, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        return hmac.new(self.agent_preview_key, message,
                        hashlib.sha256).hexdigest()

    def _get_agent_policy(
            self, values: Optional[Mapping[str, Any]]) -> agent_protocol.AutoAcceptPolicy:
        merged = self.agent_default_policy.to_dict()
        if values is not None:
            merged.update(values)
        policy = agent_protocol.AutoAcceptPolicy.from_mapping(merged)
        base = self.agent_default_policy
        looser = []
        for name in ('account_probability_threshold',
                     'account_margin_threshold',
                     'account_min_leaf_samples'):
            if getattr(policy, name) < getattr(base, name):
                looser.append(name)
        for name in ('max_used_transactions', 'max_modified_transactions',
                     'max_output_files'):
            if getattr(policy, name) > getattr(base, name):
                looser.append(name)
        for name in ('require_recognized_value_feature',
                     'require_existing_account', 'require_cleared_match'):
            if getattr(base, name) and not getattr(policy, name):
                looser.append(name)
        for name in ('allow_new_accounts', 'allow_merged_transactions'):
            if not getattr(base, name) and getattr(policy, name):
                looser.append(name)
        if looser:
            raise ValueError(
                'Per-request policy may only tighten server defaults; '
                'looser settings: %s' % ', '.join(sorted(looser)))
        return policy

    def handle_agent_decision(
            self, payload: Dict[str, Any],
            idempotency_key: Optional[str]) -> Dict[str, Any]:
        dry_run = payload.get('dry_run', True)
        if not isinstance(dry_run, bool):
            raise ValueError('dry_run must be a boolean')
        action = payload.get('action')
        if action not in ('accept', 'ignore', 'defer'):
            raise ValueError('action must be accept, ignore, or defer')

        request_hash = self._agent_request_hash(payload)
        if not dry_run:
            cached = self._get_idempotent_response(idempotency_key,
                                                   request_hash)
            if cached is not None:
                return cached

        revision = self._validate_agent_revision(payload.get('revision'))
        loaded_reconciler = self._require_loaded_reconciler()
        if self.next_candidates is None:
            raise AgentApiError(409, 'complete',
                                'There is no current pending transaction.')
        self._ensure_agent_journal_unmodified(loaded_reconciler)

        if action == 'defer':
            current_index = revision['pending_index']
            assert current_index is not None
            if current_index + 1 >= len(loaded_reconciler.pending_data):
                raise AgentApiError(
                    409, 'last_case',
                    'There is no later pending transaction in this session.')
            if dry_run:
                return {
                    'dry_run': True,
                    'action': 'defer',
                    'current_revision': revision,
                    'next_pending_index': current_index + 1,
                }
            self.skip_ids = loaded_reconciler.get_skip_ids_by_index(
                current_index + 1)
            self.get_next_candidates(new_pending=False)
            response = {
                'dry_run': False,
                'action': 'defer',
                'applied': True,
                'next': self.get_agent_current_case(),
            }
            assert idempotency_key is not None
            self._store_idempotent_response(idempotency_key, request_hash,
                                            response)
            return response

        candidate_id = payload.get('candidate_id')
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError('candidate_id is required')
        try:
            _, candidate = agent_protocol.find_candidate(
                self.next_candidates, candidate_id)
        except KeyError:
            raise AgentApiError(
                404, 'candidate_not_found',
                'The candidate is not in the current revision.')
        changes = payload.get('changes') or {}
        if not isinstance(changes, dict):
            raise ValueError('changes must be an object')
        unknown_changes = sorted(
            set(changes) - {'accounts', 'links', 'tags', 'narration', 'payee'})
        if unknown_changes:
            raise ValueError('Unsupported changes: %s' %
                             ', '.join(unknown_changes))
        if 'accounts' in changes:
            accounts = changes['accounts']
            substitutions = candidate.substituted_accounts or []
            if (not isinstance(accounts, list) or
                    not all(isinstance(account, str)
                            for account in accounts) or
                    len(accounts) != len(substitutions)):
                raise ValueError(
                    'accounts must contain one string per unknown posting')
        for name in ('links', 'tags'):
            value = changes.get(name)
            if (value is not None and
                    (not isinstance(value, list) or
                     not all(isinstance(item, str) for item in value))):
                raise ValueError('%s must be null or a list of strings' % name)
        for name in ('narration', 'payee'):
            value = changes.get(name)
            if value is not None and not isinstance(value, str):
                raise ValueError('%s must be null or a string' % name)
        if action == 'ignore' and changes:
            raise ValueError('ignore always records the raw pending transaction')
        candidate = agent_protocol.derive_candidate(candidate, changes)
        action_candidate = agent_protocol.prepare_candidate_for_action(
            candidate, action)
        preview = agent_protocol.make_preview(
            candidate,
            action,
            loaded_reconciler.editor.ignored_path,
            input_filenames=loaded_reconciler.editor.journal_filenames)
        preview_token = self._make_preview_token(revision, candidate_id, action,
                                                 changes, preview)

        if dry_run:
            return {
                'dry_run': True,
                'action': action,
                'revision': revision,
                'candidate_id': candidate_id,
                'changes': changes,
                'preview': preview,
                'preview_token': preview_token,
            }

        if self.read_only:
            raise reconcile.ReadOnlyError(
                'Candidate writes are disabled in read-only mode.')
        supplied_preview_token = payload.get('preview_token')
        if (not isinstance(supplied_preview_token, str) or
                not hmac.compare_digest(supplied_preview_token,
                                        preview_token)):
            raise AgentApiError(
                409, 'preview_stale',
                'Preview the exact decision again before committing it.', {
                    'retry_from': self.agent_api_base_path + '/decision',
                    'current_preview': preview,
                })

        result = None  # type: Optional[reconcile.AcceptCandidateResult]
        try:
            result = loaded_reconciler.accept_candidate(
                action_candidate, ignore=(action == 'ignore'))
            self._notify_modified_files(result.modified_filenames)
            self.get_next_candidates(new_pending=True)
            response = self._validate_agent_response({
                'dry_run': False,
                'action': action,
                'applied': True,
                'fully_applied': True,
                'write_status': 'complete',
                'candidate_id': candidate_id,
                'modified_filenames': result.modified_filenames,
                'applied_filenames': result.modified_filenames,
                'intended_filenames': result.intended_filenames,
                'new_entries': [
                    json_encode_beancount_entry(x)
                    for x in result.new_entries
                ],
                'next': self.get_agent_current_case(),
            })
        except reconcile.CandidateWriteAppliedError as e:
            response = self._make_agent_decision_postprocess_failure(
                action, candidate_id, e.result, e.cause)
        except Exception as e:
            if result is None:
                raise
            response = self._make_agent_decision_postprocess_failure(
                action, candidate_id, result, e)
        assert idempotency_key is not None
        self._store_idempotent_response(idempotency_key, request_hash, response)
        return response

    def handle_agent_auto_accept(
            self, payload: Dict[str, Any],
            idempotency_key: Optional[str]) -> Dict[str, Any]:
        dry_run = payload.get('dry_run', True)
        if not isinstance(dry_run, bool):
            raise ValueError('dry_run must be a boolean')
        max_cases = payload.get('max_cases', 100)
        if isinstance(max_cases, bool) or not isinstance(max_cases, int):
            raise ValueError('max_cases must be an integer')
        if max_cases < 1 or max_cases > 1000:
            raise ValueError('max_cases must be between 1 and 1000')
        for name in ('allow_errors', 'allow_invalid_references'):
            if name in payload and not isinstance(payload[name], bool):
                raise ValueError('%s must be a boolean' % name)
            if payload.get(name, False):
                raise ValueError(
                    '%s cannot bypass automatic-acceptance safety checks' %
                    name)
        policy_values = payload.get('policy')
        if policy_values is not None and not isinstance(policy_values, dict):
            raise ValueError('policy must be an object')
        policy = self._get_agent_policy(policy_values)

        request_hash = self._agent_request_hash(payload)
        if not dry_run:
            cached = self._get_idempotent_response(idempotency_key,
                                                   request_hash)
            if cached is not None:
                return cached
        self._validate_agent_revision(payload.get('revision'))
        loaded_reconciler = self._require_loaded_reconciler()
        issues = self._agent_global_issues(loaded_reconciler)
        global_blockers = []
        if (issues['blocking_error_count'] and
                not self.agent_allow_errors):
            global_blockers.append('journal_or_source_errors')
        if (issues['invalid_reference_count'] and
                not self.agent_allow_invalid_references):
            global_blockers.append('invalid_source_references')

        current = self.get_agent_current_case(policy=policy)
        if dry_run:
            recommendation = current.get('recommendation')
            return {
                'dry_run': True,
                'policy': policy.to_dict(),
                'global_blockers': global_blockers,
                'would_accept_current': bool(
                    not global_blockers and recommendation and
                    recommendation['auto_accept_eligible']),
                'current': current,
                'note': ('Later cases are intentionally not simulated because '
                         'each accepted transaction changes subsequent matching.'),
            }

        if self.read_only:
            raise reconcile.ReadOnlyError(
                'Automatic writes are disabled in read-only mode.')

        accepted = []
        stop_reason = None
        recovery = None
        batch_modified_filenames = []  # type: List[str]
        if global_blockers:
            stop_reason = 'global_blocker'
        while stop_reason is None and len(accepted) < max_cases:
            if self.next_candidates is None:
                stop_reason = 'complete'
                break
            pending_index = self.current_state.get('pending_index')
            assert pending_index is not None
            recommendation = agent_protocol.assess_candidates(
                self.next_candidates,
                sorted(loaded_reconciler.editor.accounts.keys()),
                policy)
            if not recommendation['auto_accept_eligible']:
                stop_reason = 'review_required'
                break
            candidate_id = recommendation['candidate_id']
            assert isinstance(candidate_id, str)
            _, candidate = agent_protocol.find_candidate(
                self.next_candidates, candidate_id)
            pending = self.next_candidates.pending_data[pending_index]
            preview = agent_protocol.make_preview(
                candidate,
                'accept',
                loaded_reconciler.editor.ignored_path,
                input_filenames=loaded_reconciler.editor.journal_filenames)
            try:
                self._ensure_agent_journal_unmodified(loaded_reconciler)
            except AgentApiError as e:
                if e.code != 'journal_modified':
                    raise
                stop_reason = 'journal_modified'
                break
            postprocess_error = None  # type: Optional[Exception]
            try:
                result = loaded_reconciler.accept_candidate(candidate)
            except reconcile.CandidateWriteAppliedError as e:
                result = e.result
                postprocess_error = e.cause
            except Exception as e:
                stop_reason = 'apply_failed'
                accepted.append({
                    'pending_id': pending.id,
                    'candidate_id': candidate_id,
                    'applied': False,
                    'error': str(e),
                    'preview': preview,
                })
                break
            batch_modified_filenames.extend(result.modified_filenames)
            accepted_result = {
                'pending_id': pending.id,
                'candidate_id': candidate_id,
                'applied': bool(result.modified_filenames),
                'fully_applied': result.fully_written,
                'write_status': ('complete' if result.fully_written else
                                 'partial'),
                'confidence': recommendation['confidence'],
                'modified_filenames': result.modified_filenames,
                'applied_filenames': result.modified_filenames,
                'intended_filenames': result.intended_filenames,
                'preview': preview,
            }
            if postprocess_error is None:
                try:
                    self._notify_modified_files(result.modified_filenames)
                    self.get_next_candidates(new_pending=True)
                except Exception as e:
                    postprocess_error = e
            if postprocess_error is not None:
                fully_applied = result.fully_written
                failure_code = ('postprocess_failed' if fully_applied else
                                'partial_write')
                accepted_result.update({
                    'postprocess_failed': fully_applied,
                    'partial_write': not fully_applied,
                    'error': {
                        'code': failure_code,
                        'message': (
                            'Journal files were written, but server '
                            'post-processing failed.' if fully_applied else
                            'Only part of the multi-file journal change was '
                            'written; manual repair is required.'),
                        'details': self._describe_agent_exception(
                            postprocess_error),
                    },
                })
                accepted.append(accepted_result)
                stop_reason = failure_code
                recovery = self._recover_after_agent_write(
                    result.modified_filenames)
                break
            accepted.append(accepted_result)

        if stop_reason is None:
            stop_reason = 'max_cases_reached'
        try:
            response = {
                'dry_run': False,
                'policy': policy.to_dict(),
                'accepted_count': sum(
                    x.get('fully_applied', x['applied']) for x in accepted),
                'accepted': accepted,
                'stop_reason': stop_reason,
                'global_blockers': global_blockers,
                'next': (None if recovery is not None else
                         self.get_agent_current_case(policy=policy)),
                'idempotency_scope': 'current server process',
            }
            if recovery is not None:
                response.update({
                    'postprocess_failed':
                    stop_reason == 'postprocess_failed',
                    'partial_write': stop_reason == 'partial_write',
                    'recovery': recovery,
                })
            response = self._validate_agent_response(response)
        except Exception as e:
            if not batch_modified_filenames:
                raise
            if recovery is None:
                recovery = self._recover_after_agent_write(
                    sorted(set(batch_modified_filenames)))
            safe_accepted = [{
                key: value
                for key, value in item.items()
                if key != 'preview'
            } for item in accepted]
            partial_write = any(
                item.get('partial_write', False) for item in safe_accepted)
            failure_code = ('partial_write' if partial_write else
                            'postprocess_failed')
            response = self._validate_agent_response({
                'dry_run': False,
                'policy': policy.to_dict(),
                'accepted_count': sum(
                    item.get('fully_applied', item['applied'])
                    for item in safe_accepted),
                'accepted': safe_accepted,
                'stop_reason': failure_code,
                'global_blockers': global_blockers,
                'next': None,
                'postprocess_failed': not partial_write,
                'partial_write': partial_write,
                'error': {
                    'code': failure_code,
                    'message': (
                        'Journal files were written, but the automatic '
                        'accept response could not be finalized.'),
                    'details': self._describe_agent_exception(e),
                },
                'recovery': recovery,
                'idempotency_scope': 'current server process',
            })
        assert idempotency_key is not None
        self._store_idempotent_response(idempotency_key, request_hash, response)
        return response

    def handle_agent_retrain(
            self, payload: Dict[str, Any],
            idempotency_key: Optional[str]) -> Dict[str, Any]:
        dry_run = payload.get('dry_run', True)
        if not isinstance(dry_run, bool):
            raise ValueError('dry_run must be a boolean')
        request_hash = self._agent_request_hash(payload)
        if not dry_run:
            cached = self._get_idempotent_response(idempotency_key,
                                                   request_hash)
            if cached is not None:
                return cached
        revision = self._validate_agent_revision(payload.get('revision'))
        loaded_reconciler = self._require_loaded_reconciler()
        self._ensure_agent_journal_unmodified(loaded_reconciler)
        if dry_run:
            return {
                'dry_run': True,
                'revision': revision,
                'training_example_count': len(
                    loaded_reconciler.training_examples.training_examples),
                'classifier_trusted':
                loaded_reconciler.classifier_model_trusted,
                'classifier_reason':
                loaded_reconciler.classifier_model_reason,
                'cache_write_enabled': not self.read_only,
            }

        self.reconciler.retrain()
        self.reset()
        response = {
            'dry_run': False,
            'accepted': True,
            'status': 'loading',
            'cache_write_enabled': not self.read_only,
            'poll': self.agent_api_base_path + '/state',
        }
        assert idempotency_key is not None
        self._store_idempotent_response(idempotency_key, request_hash,
                                        response)
        return response

    def _notify_modified_files(self, modified_filenames: List[str]):
        for filename in modified_filenames:
            watchers = self.watched_files.get(filename, None)
            if watchers:
                try:
                    with open(
                            filename, 'r', encoding='utf-8', newline='\n') as f:
                        contents = f.read()
                    for watcher in watchers:
                        watcher.send_file_update(filename, contents)
                except:
                    traceback.print_exc()

    def check_modification(self):
        if self.reconciler.loaded_future.done():
            loaded_reconciler = self.reconciler.loaded_future.result()
            modified_filenames = loaded_reconciler.editor.check_any_journal_modification(
            )
            if modified_filenames:
                self._notify_modified_files(list(modified_filenames))
                self.reconciler.reload_journal()
                self.reset()

    def reset(self):
        self.next_candidates = None
        self.current_errors = None
        self.current_invalid = None
        self.current_uncleared = None
        self.current_pending = None
        self.set_state(
            pending=None,
            errors=None,
            invalid=None,
            uncleared=None,
            main_journal_path=os.path.realpath(self.reconciler.journal_path),
            candidates=None)
        self.ioloop.add_future(self.reconciler.loaded_future,
                               self._handle_reconciler_loaded)

    def retrain(self):
        if self.reconciler.loaded_future.done():
            self.reconciler.retrain()
            self.reset()

    def _handle_reconciler_loaded(self, loaded_future):
        try:
            loaded_reconciler = loaded_future.result()
            generation = self.next_generation()
            self.set_state(
                errors=(generation, len(loaded_reconciler.errors)),
                invalid=(generation, len(loaded_reconciler.invalid_references)),
                accounts=sorted(loaded_reconciler.editor.accounts.keys()),
                journal_filenames=sorted(
                    list(loaded_reconciler.editor.journal_filenames)))
            self.current_errors = loaded_reconciler.errors
            self.current_invalid = loaded_reconciler.invalid_references
            self.start_check_modification_observer(loaded_reconciler)
            self.get_next_candidates(new_pending=True)
        except:
            traceback.print_exc()
            pdb.post_mortem()

    def start_check_modification_observer(self, loaded_reconciler):
        if self.check_modification_observer is not None:
            self.check_modification_observer.unschedule_all()

        self.check_modification_observer = watchdog.observers.Observer()
        handler = JournalModificationHandler(self)
        journal_paths = set(
            os.path.dirname(filename) for filename in loaded_reconciler.editor.journal_filenames)

        for path in journal_paths:
            self.check_modification_observer.schedule(handler, path)

        self.check_modification_observer.start()

    def get_next_candidates(self, new_pending):
        loaded_reconciler = self.reconciler.loaded_future.result()
        start_time = time.time()
        self.next_candidates, index, self.skip_ids = loaded_reconciler.get_next_candidates(
            self.skip_ids)
        end_time = time.time()
        print('Got next candidates in %.4f seconds' % (end_time - start_time))
        generation = self.next_generation()
        kwargs = dict()
        if new_pending:
            kwargs.update(
                pending=(generation, len(loaded_reconciler.pending_data)),
                uncleared=(generation,
                           len(loaded_reconciler.uncleared_postings)),
            )

        accounts = sorted(loaded_reconciler.editor.accounts.keys())
        if accounts != self.current_state['accounts']:
            kwargs.update(accounts=accounts)

        self.current_pending = loaded_reconciler.pending_data
        self.current_uncleared = loaded_reconciler.uncleared_postings
        if self.next_candidates is None:
            self.set_state(candidates=None, pending_index=None, **kwargs)
        else:
            self.set_state(
                candidates=self.next_candidates,
                candidates_generation=generation,
                pending_index=index,
                **kwargs)

    def _broadcast_state_changed(self):
        try:
            for client in self.socket_clients:
                client.send_state_update()
        except:
            traceback.print_exc()

    # Forces a state update message to be sent to clients even for state objects that have not
    # changed.
    def set_state_force(self, **kwargs):
        for k, v in kwargs.items():
            self.current_state[k] = v
            self.current_state_generation[k] = self.next_generation()
        self.set_state()

    def set_state(self, **kwargs):
        for k, v in kwargs.items():
            if v is not self.current_state.get(k):
                self.current_state[k] = v
                self.current_state_generation[k] = self.next_generation()
        self.ioloop.add_callback(self._broadcast_state_changed)

    def log_status(self, message):
        logging.info(message)
        self.set_state(message=message)

    def handle_change_candidate(self, msg):
        try:
            if (self.next_candidates is not None and
                    self.current_state['candidates_generation'] ==
                    msg['generation']):
                self.next_candidates.change_transaction(msg['candidate_index'],
                                                        msg['changes'])
                self.set_state_force(candidates=self.next_candidates)
        except:
            traceback.print_exc()

    def handle_select_candidate(self, msg):
        if self.read_only:
            raise reconcile.ReadOnlyError(
                'Candidate writes are disabled in read-only mode.')
        result = None  # type: Optional[reconcile.AcceptCandidateResult]
        try:
            if (self.next_candidates is not None and msg['generation'] ==
                    self.current_state['candidates_generation']):
                index = msg['index']
                if index >= 0 and index < len(self.next_candidates.candidates):
                    candidate = self.next_candidates.candidates[index]
                    ignore = msg.get('ignore', None) is True
                    result = self.reconciler.loaded_future.result(
                    ).accept_candidate(
                        candidate,
                        ignore=ignore,
                    )
                    self._notify_modified_files(result.modified_filenames)
                    self.get_next_candidates(new_pending=True)
                    return result.new_entries
        except reconcile.CandidateWriteAppliedError as e:
            traceback.print_exc()
            self._recover_after_agent_write(e.result.modified_filenames)
            return e.result.new_entries
        except:
            traceback.print_exc()
            if result is not None:
                self._recover_after_agent_write(result.modified_filenames)
                return result.new_entries
            print('got error')
            pdb.post_mortem()

    def handle_skip(self, msg):
        pending_generation = int(msg['generation'])
        pending_index = int(msg['index'])
        pending_state = self.current_state['pending']
        if pending_state is None:
            return
        if pending_state[0] != pending_generation:
            return
        loaded_reconciler = self.reconciler.loaded_future.result()
        if pending_index < 0:
            pending_index = 0
        if pending_index >= pending_state[1]:
            pending_index = pending_state[1] - 1
        self.skip_ids = loaded_reconciler.get_skip_ids_by_index(pending_index)
        self.get_next_candidates(new_pending=False)

    def handle_retrain(self, _):
        self.retrain()


def parse_arguments(argv, **kwargs):
    argparser = argparse.ArgumentParser(
        parents=[reconcile.get_entry_file_selector_argparser(kwargs)])
    argparser.add_argument(
        '--journal_input',
        help='Top-level Beancount input file',
        required=kwargs.get('journal_input') is None)
    argparser.add_argument(
        '--ignored_journal',
        help='Beancount input file containing ignored entries',
        required=kwargs.get('ignored_journal') is None)
    argparser.add_argument(
        '--data_sources',
        help='Data sources JSON specification',
        type=json.loads,
        default=[])
    argparser.add_argument(
        '--ignore_account_for_classification_pattern',
        help=
        'Regular expression matching account names that should be ignored for the purpose of automatic classification.  Only transactions with exactly two non-ignored postings are used.',
        default=training.DEFAULT_IGNORE_ACCOUNT_FOR_CLASSIFICATION_PATTERN)
    argparser.add_argument(
        '--log-output',
        type=str,
        help='Filename to which log output will be written.')
    argparser.add_argument(
        '--account_pattern',
        type=str,
        help='Regular expression for limiting accounts to reconcile.')
    argparser.add_argument(
        '-p',
        '--port',
        type=int,
        default=8101,
        help='Port on which webserver listens.')
    argparser.add_argument(
        '-a',
        '--address',
        type=str,
        default='127.0.0.1',
        help='Address on which webserver listens.')
    argparser.add_argument(
        '--browser',
        action='store_true',
        help='Open a web browser automatically.')
    argparser.add_argument(
        '-d',
        '--debug',
        help='Set log verbosity to DEBUG.',
        action='store_const',
        dest='loglevel',
        const=logging.DEBUG,
        default=logging.WARNING)
    argparser.add_argument(
        '-v',
        '--verbose',
        help='Set log verbosity to INFO.',
        action='store_const',
        dest='loglevel',
        const=logging.DEBUG)
    argparser.add_argument(
        '--fuzzy_match_days',
        type=int,
        default=5,
        help=
        'Maximum number of days by which the dates of two matching entries may differ.'
    )
    argparser.add_argument(
        '--fuzzy_match_amount',
        type=Decimal,
        default=D('0.01'),
        help=
        'Maximum amount by which the weights of two matching entries may differ.'
    )
    argparser.add_argument(
        '--max-matches',
        '--max_matches',
        dest='max_matches',
        type=int,
        default=0,
        help=('Maximum matching transactions/results explored at each step. '
              'The compatibility default 0 keeps the original unlimited '
              'search.  A truncated search '
              'is never eligible for agent auto-accept.'))
    argparser.add_argument(
        '--classifier_cache',
        type=str,
        help=
        'Cache file for automatic account prediction classifier.  This speeds up loading.'
    )
    argparser.add_argument(
        '--read-only',
        '--read_only',
        dest='read_only',
        action='store_true',
        help=('Disable journal writes and classifier-cache writes.  Source '
              'plugins are still responsible for avoiding their own side effects.'))
    argparser.add_argument(
        '--agent-auto-accept-with-errors',
        '--agent_auto_accept_with_errors',
        dest='agent_auto_accept_with_errors',
        action='store_true',
        help=('Allow automatic acceptance despite journal/source errors. '
              'This is a server-wide startup authorization.'))
    argparser.add_argument(
        '--agent-auto-accept-with-invalid-references',
        '--agent_auto_accept_with_invalid_references',
        dest='agent_auto_accept_with_invalid_references',
        action='store_true',
        help=('Allow automatic acceptance despite invalid source references. '
              'This is a server-wide startup authorization.'))
    argparser.add_argument(
        '--agent-account-probability-threshold',
        '--agent_account_probability_threshold',
        dest='agent_account_probability_threshold',
        type=float,
        default=0.99,
        help='Minimum decision-tree probability for agent auto-accept.')
    argparser.add_argument(
        '--agent-account-margin-threshold',
        '--agent_account_margin_threshold',
        dest='agent_account_margin_threshold',
        type=float,
        default=0.95,
        help='Minimum top-vs-runner-up probability margin for auto-accept.')
    argparser.add_argument(
        '--agent-account-min-leaf-samples',
        '--agent_account_min_leaf_samples',
        dest='agent_account_min_leaf_samples',
        type=int,
        default=5,
        help='Minimum decision-tree leaf support for agent auto-accept.')
    argparser.set_defaults(**kwargs)
    args = argparser.parse_args(argv)
    if args.max_matches is not None and args.max_matches < 0:
        argparser.error('--max-matches must be 0 or a positive integer')
    if not 0 <= args.agent_account_probability_threshold <= 1:
        argparser.error(
            '--agent-account-probability-threshold must be in [0, 1]')
    if not 0 <= args.agent_account_margin_threshold <= 1:
        argparser.error('--agent-account-margin-threshold must be in [0, 1]')
    if args.agent_account_min_leaf_samples < 1:
        argparser.error('--agent-account-min-leaf-samples must be positive')
    return args


def main(argv, **kwargs):
    args = parse_arguments(argv, **kwargs)
    logging_args = dict(level=args.loglevel)
    if args.log_output is not None:
        logging_args['filename'] = args.log_output
    logging.basicConfig(**logging_args)

    init_tornado_asyncio()

    ioloop = tornado.ioloop.IOLoop.instance()
    app = Application(args=args, ioloop=ioloop, debug=(args.loglevel == logging.DEBUG))

    http_server = tornado.httpserver.HTTPServer(app)
    sockets = tornado.netutil.bind_sockets(
        port=args.port or None, address=args.address)
    http_server.add_sockets(sockets)
    server_url = 'http://%s:%s' % sockets[0].getsockname()[0:2]
    print('Listening at %s' % server_url)
    print('Agent API at %s%s' % (server_url, app.agent_api_base_path))
    if app.read_only:
        print('Read-only mode: journal and classifier-cache writes are disabled')
    if args.browser:
        webbrowser.open(server_url, new=1)
    ioloop.start()


if __name__ == '__main__':
    main(sys.argv[1:])
