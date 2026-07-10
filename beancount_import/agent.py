"""Agent-oriented reconciliation protocol and confidence policy.

This module deliberately keeps policy and serialization independent of the
Tornado handlers in :mod:`beancount_import.webserver`, which makes the safety
rules testable without running an HTTP server.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import dataclasses
import datetime
import hashlib
import json
import os

from beancount.core.data import Directive, Open, Posting, Transaction
import beancount.parser.printer

from . import matching
from . import reconcile


@dataclasses.dataclass(frozen=True)
class AutoAcceptPolicy:
    """Conservative defaults for unattended reconciliation."""

    account_probability_threshold: float = 0.99
    account_margin_threshold: float = 0.95
    account_min_leaf_samples: int = 5
    require_recognized_value_feature: bool = True
    require_existing_account: bool = True
    require_cleared_match: bool = True
    max_used_transactions: int = 2
    max_modified_transactions: int = 1
    max_output_files: int = 1
    allow_new_accounts: bool = False
    allow_merged_transactions: bool = False

    @classmethod
    def from_mapping(
            cls,
            values: Optional[Mapping[str, Any]] = None) -> 'AutoAcceptPolicy':
        if values is None:
            return cls()
        allowed = {field.name for field in dataclasses.fields(cls)}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError('Unknown policy settings: %s' % ', '.join(unknown))
        policy = cls(**dict(values))
        for name in ('account_probability_threshold',
                     'account_margin_threshold'):
            value = getattr(policy, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError('%s must be a number' % name)
        for name in ('account_min_leaf_samples', 'max_used_transactions',
                     'max_modified_transactions', 'max_output_files'):
            value = getattr(policy, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError('%s must be an integer' % name)
        for name in ('require_recognized_value_feature',
                     'require_existing_account', 'require_cleared_match',
                     'allow_new_accounts', 'allow_merged_transactions'):
            if not isinstance(getattr(policy, name), bool):
                raise ValueError('%s must be a boolean' % name)
        if not 0 <= policy.account_probability_threshold <= 1:
            raise ValueError('account_probability_threshold must be in [0, 1]')
        if not 0 <= policy.account_margin_threshold <= 1:
            raise ValueError('account_margin_threshold must be in [0, 1]')
        if policy.account_min_leaf_samples < 1:
            raise ValueError('account_min_leaf_samples must be positive')
        if policy.max_used_transactions < 1:
            raise ValueError('max_used_transactions must be positive')
        if policy.max_modified_transactions < 0:
            raise ValueError('max_modified_transactions cannot be negative')
        if policy.max_output_files < 1:
            raise ValueError('max_output_files must be positive')
        return policy

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def json_encode_beancount_entry(entry: Directive) -> Dict[str, Any]:
    if isinstance(entry, Transaction):
        entry = entry._replace(postings=[
            posting._asdict() for posting in entry.postings
        ])
    result = entry._asdict()
    if result['meta'] is not None:
        result['meta'] = result['meta'].copy()
        result['meta'].pop('__tolerances__', None)
    return result


def format_entry(entry: Directive) -> str:
    return beancount.parser.printer.EntryPrinter()(entry)


def encode_pending(pending: reconcile.PendingEntry) -> Dict[str, Any]:
    return {
        'id': pending.id,
        'date': pending.date,
        'source': None if pending.source is None else pending.source.name,
        'info': pending.info,
        'formatted': pending.formatted,
        'entries': [json_encode_beancount_entry(x) for x in pending.entries],
    }


def _match_score(candidate: reconcile.Candidate) -> Tuple[int, int, int]:
    evidence = candidate.match_evidence
    if evidence is None:
        return (0, 0, 0)
    return (evidence.cleared_posting_matches,
            evidence.uncleared_posting_matches,
            -evidence.unknown_postings_removed)


def _is_merged(candidate: reconcile.Candidate) -> bool:
    return len(candidate.used_transactions) > 1


def _contains_unknown_account(candidate: reconcile.Candidate) -> bool:
    for entry in candidate.staged_changes.get_all_new_entries():
        if entry is None:
            continue
        if not isinstance(entry, Transaction):
            continue
        if any(matching.is_unknown_account(posting.account)
               for posting in entry.postings):
            return True
    return False


def get_diff_risk(candidate: reconcile.Candidate,
                  existing_accounts: Sequence[str]) -> Dict[str, Any]:
    additions = 0
    modifications = 0
    removals = 0
    modified_transactions = 0
    removed_transactions = 0
    new_open_accounts = []
    existing_account_set = set(existing_accounts)

    for changed_entries in candidate.staged_changes.changed_entries.values():
        for old_entry, new_entry in changed_entries:
            if old_entry is None:
                additions += 1
            elif new_entry is None:
                removals += 1
                if isinstance(old_entry, Transaction):
                    removed_transactions += 1
            else:
                modifications += 1
                if isinstance(old_entry, Transaction):
                    modified_transactions += 1
            if (isinstance(new_entry, Open) and
                    new_entry.account not in existing_account_set):
                new_open_accounts.append(new_entry.account)

    modified_filenames = list(candidate.staged_changes.changed_entries.keys())
    return {
        'added_entries': additions,
        'modified_entries': modifications,
        'removed_entries': removals,
        'modified_transactions': modified_transactions,
        'removed_transactions': removed_transactions,
        'new_open_accounts': sorted(set(new_open_accounts)),
        'modified_filenames': modified_filenames,
        'output_file_count': len(modified_filenames),
        'multi_file_atomic': len(modified_filenames) <= 1,
    }


def _prediction_to_dict(
        prediction: reconcile.AccountPredictionEvidence) -> Dict[str, Any]:
    return {
        'predicted_account': prediction.predicted_account,
        'probability': prediction.probability,
        'runner_up_account': prediction.runner_up_account,
        'runner_up_probability': prediction.runner_up_probability,
        'margin': prediction.margin,
        'leaf_sample_count': prediction.leaf_sample_count,
        'recognized_feature_count': prediction.recognized_feature_count,
        'recognized_value_features': prediction.recognized_value_features,
        'alternatives': [
            {
                'account': alternative.account,
                'probability': alternative.probability,
            } for alternative in prediction.alternatives
        ],
        'explanation': prediction.explanation,
        'input_available': prediction.input_available,
        'model_trusted': prediction.model_trusted,
        'model_reason': prediction.model_reason,
    }


def get_candidate_fingerprint(candidate: reconcile.Candidate,
                              candidate_index: int) -> str:
    staged_entries = []
    for filename, changed_entries in candidate.staged_changes.changed_entries.items(
    ):
        staged_entries.append({
            'filename': filename,
            'changes': [{
                'old': (None if old_entry is None else {
                    'filename': (old_entry.meta or {}).get('filename'),
                    'lineno': (old_entry.meta or {}).get('lineno'),
                    'formatted': format_entry(old_entry),
                }),
                'new': (None if new_entry is None else
                        format_entry(new_entry)),
            } for old_entry, new_entry in changed_entries],
        })
    payload = {
        'candidate_index': candidate_index,
        'used_transaction_ids': candidate.used_transaction_ids,
        'staged_entries': staged_entries,
        'accounts': [
            substitution.account_name
            for substitution in (candidate.substituted_accounts or [])
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def get_candidate_set_hash(candidates: reconcile.Candidates) -> str:
    fingerprints = [
        get_candidate_fingerprint(candidate, index)
        for index, candidate in enumerate(candidates.candidates)
    ]
    return hashlib.sha256(
        '\n'.join(fingerprints).encode('ascii')).hexdigest()


def assess_candidates(candidates: reconcile.Candidates,
                      existing_accounts: Sequence[str],
                      policy: AutoAcceptPolicy) -> Dict[str, Any]:
    candidate_list = candidates.candidates
    if not candidate_list:
        return {
            'action': 'review',
            'candidate_id': None,
            'candidate_index': None,
            'auto_accept_eligible': False,
            'confidence': 0.0,
            'reasons': [],
            'blockers': ['no_candidates'],
            'policy': policy.to_dict(),
        }

    merged_indices = [
        index for index, candidate in enumerate(candidate_list)
        if _is_merged(candidate)
    ]
    if merged_indices:
        best_score = max(_match_score(candidate_list[index])
                         for index in merged_indices)
        best_indices = [
            index for index in merged_indices
            if _match_score(candidate_list[index]) == best_score
        ]
        recommended_index = best_indices[0]
    else:
        best_score = _match_score(candidate_list[0])
        best_indices = [0]
        recommended_index = 0

    candidate = candidate_list[recommended_index]
    candidate_id = get_candidate_fingerprint(candidate, recommended_index)
    blockers = []  # type: List[str]
    reasons = []  # type: List[str]
    risk = get_diff_risk(candidate, existing_accounts)

    if len(best_indices) != 1:
        blockers.append('ambiguous_top_match')
    else:
        reasons.append('strict_unique_best_candidate')

    if len(candidate.used_transactions) > policy.max_used_transactions:
        blockers.append('too_many_used_transactions')

    evidence = candidate.match_evidence
    if evidence is not None and evidence.search_truncated:
        blockers.append('match_search_truncated')
    if _is_merged(candidate):
        if not policy.allow_merged_transactions:
            blockers.append('merged_candidate_requires_review')
        if evidence is None:
            blockers.append('match_evidence_unavailable')
        else:
            if (policy.require_cleared_match and
                    evidence.cleared_posting_matches < 1):
                blockers.append('no_cleared_posting_match')
            if evidence.unknown_postings_removed != 0:
                blockers.append('unknown_postings_removed')
            if evidence.cleared_posting_matches > 0:
                reasons.append('authoritative_match_present')
    elif merged_indices:
        blockers.append('unmerged_candidate_with_available_matches')
    else:
        reasons.append('no_merge_competitor')

    if risk['modified_transactions'] > policy.max_modified_transactions:
        blockers.append('too_many_modified_transactions')
    if risk['removed_transactions']:
        blockers.append('removes_existing_transactions')
    if risk['output_file_count'] > policy.max_output_files:
        blockers.append('multi_file_change')
    if risk['new_open_accounts'] and not policy.allow_new_accounts:
        blockers.append('creates_new_accounts')

    if _contains_unknown_account(candidate):
        blockers.append('unresolved_unknown_account')

    prediction_confidences = []  # type: List[float]
    seen_groups = set()
    substitutions = candidate.substituted_accounts or []
    for substitution, prediction in zip(
            substitutions, candidate.account_prediction_evidence):
        if substitution.group_number in seen_groups:
            continue
        seen_groups.add(substitution.group_number)
        prediction_confidences.append(prediction.probability)
        prefix = 'prediction_group_%d_' % substitution.group_number
        if not prediction.model_trusted:
            blockers.append(prefix + 'model_untrusted')
        if matching.is_unknown_account(prediction.predicted_account):
            blockers.append(prefix + 'unresolved')
        if prediction.probability < policy.account_probability_threshold:
            blockers.append(prefix + 'probability_below_threshold')
        if prediction.margin < policy.account_margin_threshold:
            blockers.append(prefix + 'margin_below_threshold')
        if prediction.leaf_sample_count < policy.account_min_leaf_samples:
            blockers.append(prefix + 'leaf_support_below_threshold')
        if (policy.require_recognized_value_feature and
                not prediction.recognized_value_features):
            blockers.append(prefix + 'no_recognized_value_feature')
        if (policy.require_existing_account and
                prediction.predicted_account not in existing_accounts):
            blockers.append(prefix + 'account_not_open')

    if substitutions and len(candidate.account_prediction_evidence) != len(
            substitutions):
        blockers.append('prediction_evidence_incomplete')

    if prediction_confidences:
        account_confidence = min(prediction_confidences)
    else:
        account_confidence = 1.0
        reasons.append('no_account_prediction_required')

    match_confidence = 0.99 if _is_merged(candidate) else 1.0
    raw_confidence = min(account_confidence, match_confidence)
    auto_accept_eligible = not blockers
    return {
        'action': 'accept' if auto_accept_eligible else 'review',
        'candidate_id': candidate_id,
        'candidate_index': recommended_index,
        'auto_accept_eligible': auto_accept_eligible,
        'confidence': raw_confidence if auto_accept_eligible else 0.0,
        'raw_confidence': raw_confidence,
        'account_confidence': account_confidence,
        'match_confidence': match_confidence,
        'match_score': {
            'legacy_cleared_posting_matches': best_score[0],
            'legacy_uncleared_posting_matches': best_score[1],
            'legacy_unknown_postings_removed': -best_score[2],
        },
        'diff_risk': risk,
        'reasons': reasons,
        'blockers': sorted(set(blockers)),
        'policy': policy.to_dict(),
    }


def encode_candidate(candidate: reconcile.Candidate, candidate_index: int,
                     existing_accounts: Sequence[str],
                     include_diff: bool = False) -> Dict[str, Any]:
    match_evidence = None
    if candidate.match_evidence is not None:
        match_evidence = {
            # These legacy counters are direction-dependent.  They are exposed
            # as ranking evidence, not calibrated probabilities.
            'legacy_cleared_posting_matches':
            candidate.match_evidence.cleared_posting_matches,
            'legacy_uncleared_posting_matches':
            candidate.match_evidence.uncleared_posting_matches,
            'legacy_unknown_postings_removed':
            candidate.match_evidence.unknown_postings_removed,
            'search_truncated': candidate.match_evidence.search_truncated,
            'legacy_rank': list(_match_score(candidate)),
        }

    predictions = []
    substitutions = candidate.substituted_accounts or []
    for index, substitution in enumerate(substitutions):
        prediction = (candidate.account_prediction_evidence[index]
                      if index < len(candidate.account_prediction_evidence)
                      else None)
        predictions.append({
            'posting_index': index,
            'group_number': substitution.group_number,
            'unknown_account_name': substitution.unknown_account_name,
            'selected_account': substitution.account_name,
            'predicted_account': substitution.predicted_account_name,
            'evidence': (None if prediction is None else
                         _prediction_to_dict(prediction)),
        })

    new_entries = [
        entry for entry in candidate.staged_changes.get_all_new_entries()
        if entry is not None
    ]
    result = {
        'id': get_candidate_fingerprint(candidate, candidate_index),
        'index': candidate_index,
        'kind': ('merged' if _is_merged(candidate) else
                 'direct' if not candidate.used_transactions else 'unmerged'),
        'used_transaction_ids': candidate.used_transaction_ids,
        'match_evidence': match_evidence,
        'account_predictions': predictions,
        'diff_risk': get_diff_risk(candidate, existing_accounts),
        'modified_filenames': list(
            candidate.staged_changes.changed_entries.keys()),
        'new_entries': [json_encode_beancount_entry(x) for x in new_entries],
        'new_entries_formatted': [format_entry(x) for x in new_entries],
    }
    if include_diff:
        diff = candidate.staged_changes.get_diff()
        result.update({
            'diff': candidate.staged_changes.get_textual_diff(),
            'new_entries': [
                json_encode_beancount_entry(x) for x in diff.new_entries
            ],
            'new_entries_formatted': [
                format_entry(x) for x in diff.new_entries
            ],
            'associated_data': [
                x.__dict__ for x in candidate.associated_data
            ],
        })
    return result


def encode_case(candidates: reconcile.Candidates, pending_index: int,
                existing_accounts: Sequence[str],
                policy: AutoAcceptPolicy,
                include_diffs: bool = False) -> Dict[str, Any]:
    pending = candidates.pending_data[pending_index]
    return {
        'pending_index': pending_index,
        'pending': encode_pending(pending),
        'candidate_set_hash': get_candidate_set_hash(candidates),
        'used_transactions': [{
            'formatted': format_entry(transaction),
            'entry': json_encode_beancount_entry(transaction),
            'pending_index': used_pending_index,
        } for transaction, used_pending_index in candidates.used_transactions],
        'candidates': [
            encode_candidate(candidate, index, existing_accounts,
                             include_diff=include_diffs)
            for index, candidate in enumerate(candidates.candidates)
        ],
        'recommendation': assess_candidates(candidates, existing_accounts,
                                            policy),
    }


def find_candidate(candidates: reconcile.Candidates,
                   candidate_id: str) -> Tuple[int, reconcile.Candidate]:
    for index, candidate in enumerate(candidates.candidates):
        if get_candidate_fingerprint(candidate, index) == candidate_id:
            return index, candidate
    raise KeyError(candidate_id)


def derive_candidate(candidate: reconcile.Candidate,
                     changes: Optional[Mapping[str, Any]]) -> reconcile.Candidate:
    if not changes:
        return candidate
    if candidate.substitute is None:
        raise ValueError('This candidate does not support transaction changes.')
    return candidate.substitute(dict(changes))


def prepare_candidate_for_action(candidate: reconcile.Candidate,
                                 action: str) -> reconcile.Candidate:
    if action == 'accept':
        return candidate
    if action == 'ignore':
        if len(candidate.used_transactions) != 1:
            raise ValueError(
                'Only the raw, unmerged pending transaction may be ignored.')
        if candidate.substituted_accounts:
            candidate = derive_candidate(
                candidate, {
                    'accounts': [
                        substitution.unknown_account_name
                        for substitution in candidate.substituted_accounts
                    ]
                })
        transaction_additions = []
        invalid_pairs = []
        for filename, pairs in candidate.staged_changes.changed_entries.items():
            for old_entry, new_entry in pairs:
                if old_entry is not None or new_entry is None:
                    invalid_pairs.append((old_entry, new_entry))
                elif isinstance(new_entry, Transaction):
                    transaction_additions.append((filename, new_entry))
                elif not isinstance(new_entry, Open):
                    invalid_pairs.append((old_entry, new_entry))
        if invalid_pairs or len(transaction_additions) != 1:
            raise ValueError(
                'Ignore requires one raw pending transaction addition and '
                'cannot move or modify an existing journal entry.')
        # Rebuilding a predicted transaction with Expenses:FIXME may stage a
        # missing Open directive.  Ignore records only the raw transaction; it
        # must not copy auxiliary directives into the ignored journal.
        filename, transaction = transaction_additions[0]
        raw_stage = candidate.staged_changes.journal_editor.stage_changes()
        raw_stage.add_entry(transaction, filename)
        raw_candidate = reconcile.Candidate(
            staged_changes=raw_stage,
            staged_changes_with_unique_account_names=raw_stage,
            used_import_results=candidate.used_import_results,
            used_transactions=candidate.used_transactions,
            substituted_accounts=candidate.substituted_accounts,
            original_transaction_properties=
            candidate.original_transaction_properties,
            substitute=candidate.substitute,
            match_evidence=candidate.match_evidence,
            account_prediction_evidence=
            candidate.account_prediction_evidence,
        )
        raw_candidate.used_transaction_ids = candidate.used_transaction_ids
        raw_candidate.associated_data = candidate.associated_data
        return raw_candidate
    raise ValueError('Unsupported action: %s' % action)


def get_action_stage(candidate: reconcile.Candidate, action: str,
                     ignored_path: Optional[str]):
    candidate = prepare_candidate_for_action(candidate, action)
    if action == 'accept':
        return candidate.staged_changes
    if ignored_path is None:
        raise ValueError('No ignored journal is configured.')
    return candidate.staged_changes.make_with_new_output_filename(ignored_path)


def get_file_hashes(filenames: Sequence[str]) -> Dict[str, Optional[str]]:
    result = {}
    for filename in filenames:
        realpath = os.path.realpath(filename)
        try:
            with open(realpath, 'rb') as f:
                result[realpath] = hashlib.sha256(f.read()).hexdigest()
        except FileNotFoundError:
            result[realpath] = None
    return result


def make_preview(candidate: reconcile.Candidate, action: str,
                 ignored_path: Optional[str],
                 input_filenames: Optional[Sequence[str]] = None
                 ) -> Dict[str, Any]:
    stage = get_action_stage(candidate, action, ignored_path)
    modified_filenames = stage.get_modified_filenames()
    if input_filenames is None:
        input_filenames = modified_filenames
    return {
        'action': action,
        'diff': stage.get_textual_diff(),
        'modified_filenames': modified_filenames,
        'input_file_hashes': get_file_hashes(
            sorted(set(input_filenames) | set(modified_filenames))),
        'multi_file_atomic': len(modified_filenames) <= 1,
    }
