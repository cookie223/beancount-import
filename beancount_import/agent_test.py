import decimal
import datetime
import types

import pytest
from beancount.core.amount import Amount
from beancount.core.data import Open, Transaction
from beancount.core.number import D

from . import agent
from . import journal_editor
from . import matching
from . import reconcile
from . import test_util
from . import training


def _is_cleared(posting):
    return bool(posting.meta and posting.meta.get('cleared') is True)


def test_legacy_matching_api_is_unchanged_and_evidence_is_retained():
    initial, = test_util.parse("""
        2016-01-01 * "Imported"
          Assets:A  -1 USD
            cleared: TRUE
            note1: "A"
          Assets:B   1 USD
            note1: "B"
        """)
    other, = test_util.parse("""
        2016-01-01 * "Imported"
          Assets:A  -1 USD
            note2: "A"
          Assets:B   1 USD
            cleared: TRUE
            note2: "B"
        """)
    # Treat both entries as imported data rather than entries already in a
    # journal.  This also keeps the test independent of parser filenames.
    del initial.meta['filename']
    del other.meta['filename']

    posting_db = matching.PostingDatabase(
        fuzzy_match_days=3,
        fuzzy_match_amount=decimal.Decimal('0.01'),
        is_cleared=_is_cleared,
        metadata_keys=frozenset([matching.CHECK_KEY]),
    )
    posting_db.add_transaction(initial)
    posting_db.add_transaction(other)

    legacy_results = matching.get_extended_transactions(initial, posting_db)
    scored_results = matching.get_extended_transactions_with_evidence(
        initial, posting_db)

    assert legacy_results == [
        matching.MergedTransaction(result.transaction,
                                   result.used_transactions)
        for result in scored_results
    ]
    assert legacy_results
    assert all(isinstance(result, matching.MergedTransaction)
               and len(result) == 2 for result in legacy_results)
    assert any(result.match_evidence.cleared_posting_matches > 0
               for result in scored_results)
    assert all(
        not any(key in (result.transaction.meta or {})
                for key in matching.TRANSACTION_TEMP_METADATA_KEYS)
        for result in scored_results)


def _make_editor(tmp_path):
    journal_path = tmp_path / 'journal.beancount'
    ignored_path = tmp_path / 'ignored.beancount'
    journal_path.write_text(
        'option "operating_currency" "USD"\n\n'
        '2000-01-01 open Assets:Checking USD\n'
        '2000-01-01 open Expenses:Food USD\n'
        '2000-01-01 open Expenses:Other USD\n',
        encoding='utf-8')
    ignored_path.write_text('', encoding='utf-8')
    return (journal_editor.JournalEditor(str(journal_path), str(ignored_path)),
            str(journal_path), str(ignored_path))


def _transaction(account='Expenses:Food', narration='Lunch'):
    transaction, = test_util.parse("""
        2020-01-02 * "%s"
          Assets:Checking  -10 USD
          %s                10 USD
        """ % (narration, account))
    return transaction


def _prediction(probability=1.0, runner_up_probability=0.0,
                leaf_sample_count=10):
    return reconcile.AccountPredictionEvidence(
        predicted_account='Expenses:Food',
        probability=probability,
        runner_up_account='Expenses:Other',
        runner_up_probability=runner_up_probability,
        margin=max(0.0, probability - runner_up_probability),
        leaf_sample_count=leaf_sample_count,
        recognized_feature_count=1,
        recognized_value_features=['description:lunch'],
        alternatives=[
            reconcile.AccountPrediction('Expenses:Food', probability),
            reconcile.AccountPrediction('Expenses:Other',
                                        runner_up_probability),
        ],
        explanation=['description:lunch = True'],
        input_available=True,
        model_trusted=True,
        model_reason=None,
    )


def _make_classified_candidate(editor, output_path, prediction):
    raw_transaction = _transaction('Expenses:FIXME')

    def build(account):
        transaction = raw_transaction._replace(postings=[
            raw_transaction.postings[0],
            raw_transaction.postings[1]._replace(account=account),
        ])
        stage = editor.stage_changes()
        stage.add_entry(transaction, output_path)
        substitution = reconcile.AccountSubstitution(
            unique_name='unique-account-name',
            account_name=account,
            group_number=0,
            unknown_account_name='Expenses:FIXME',
            predicted_account_name='Expenses:Food',
        )

        def substitute(changes):
            accounts = changes.get('accounts')
            return build(account if accounts is None else accounts[0])

        return reconcile.Candidate(
            staged_changes=stage,
            staged_changes_with_unique_account_names=stage,
            used_import_results=[raw_transaction],
            used_transactions=[raw_transaction],
            substituted_accounts=[substitution],
            substitute=substitute,
            match_evidence=matching.MatchEvidence(0, 0, 0),
            account_prediction_evidence=[prediction],
        )

    return build('Expenses:Food')


def _make_merged_candidate(editor, output_path, evidence, narration):
    transaction = _transaction(narration=narration)
    matching_transaction = _transaction(
        narration='%s counterpart' % narration)
    stage = editor.stage_changes()
    stage.add_entry(transaction, output_path)
    return reconcile.Candidate(
        staged_changes=stage,
        staged_changes_with_unique_account_names=stage,
        used_import_results=[transaction, matching_transaction],
        used_transactions=[transaction, matching_transaction],
        match_evidence=evidence,
    )


def _candidate_set(candidates):
    return reconcile.Candidates(
        candidates=candidates,
        pending_data=[],
        sources=[],
    )


def test_policy_rejects_perfect_probability_with_one_leaf_sample(tmp_path):
    editor, journal_path, _ = _make_editor(tmp_path)
    candidate = _make_classified_candidate(
        editor, journal_path,
        _prediction(probability=1.0, runner_up_probability=0.0,
                    leaf_sample_count=1))

    assessment = agent.assess_candidates(
        _candidate_set([candidate]),
        existing_accounts=['Assets:Checking', 'Expenses:Food',
                           'Expenses:Other'],
        policy=agent.AutoAcceptPolicy(),
    )

    assert assessment['auto_accept_eligible'] is False
    assert assessment['action'] == 'review'
    assert assessment['confidence'] == 0.0
    assert assessment['raw_confidence'] == 1.0
    assert 'prediction_group_0_leaf_support_below_threshold' in assessment[
        'blockers']


def test_policy_rejects_tied_top_matches(tmp_path):
    editor, journal_path, _ = _make_editor(tmp_path)
    evidence = matching.MatchEvidence(
        cleared_posting_matches=1,
        uncleared_posting_matches=1,
        unknown_postings_removed=0,
    )
    candidates = _candidate_set([
        _make_merged_candidate(editor, journal_path, evidence, 'Candidate A'),
        _make_merged_candidate(editor, journal_path, evidence, 'Candidate B'),
    ])

    assessment = agent.assess_candidates(
        candidates,
        existing_accounts=['Assets:Checking', 'Expenses:Food'],
        policy=agent.AutoAcceptPolicy(),
    )

    assert assessment['auto_accept_eligible'] is False
    assert assessment['action'] == 'review'
    assert 'ambiguous_top_match' in assessment['blockers']


def test_policy_requires_explicit_server_opt_in_for_legacy_merged_match(
        tmp_path):
    editor, journal_path, _ = _make_editor(tmp_path)
    strong = _make_merged_candidate(
        editor, journal_path,
        matching.MatchEvidence(
            cleared_posting_matches=2,
            uncleared_posting_matches=1,
            unknown_postings_removed=0,
        ), 'Strong candidate')
    weaker = _make_merged_candidate(
        editor, journal_path,
        matching.MatchEvidence(
            cleared_posting_matches=1,
            uncleared_posting_matches=2,
            unknown_postings_removed=0,
        ), 'Weaker candidate')

    default_assessment = agent.assess_candidates(
        _candidate_set([strong, weaker]),
        existing_accounts=['Assets:Checking', 'Expenses:Food'],
        policy=agent.AutoAcceptPolicy(),
    )

    assert default_assessment['auto_accept_eligible'] is False
    assert 'merged_candidate_requires_review' in default_assessment['blockers']

    assessment = agent.assess_candidates(
        _candidate_set([strong, weaker]),
        existing_accounts=['Assets:Checking', 'Expenses:Food'],
        policy=agent.AutoAcceptPolicy(allow_merged_transactions=True),
    )

    assert assessment['auto_accept_eligible'] is True
    assert assessment['action'] == 'accept'
    assert assessment['candidate_index'] == 0
    assert assessment['blockers'] == []
    assert 'strict_unique_best_candidate' in assessment['reasons']
    assert 'authoritative_match_present' in assessment['reasons']


def test_policy_rejects_a_truncated_match_search(tmp_path):
    editor, journal_path, _ = _make_editor(tmp_path)
    candidate = _make_merged_candidate(
        editor, journal_path,
        matching.MatchEvidence(
            cleared_posting_matches=2,
            uncleared_posting_matches=1,
            unknown_postings_removed=0,
            search_truncated=True,
        ), 'Truncated candidate')

    assessment = agent.assess_candidates(
        _candidate_set([candidate]),
        existing_accounts=['Assets:Checking', 'Expenses:Food'],
        policy=agent.AutoAcceptPolicy(),
    )

    assert assessment['auto_accept_eligible'] is False
    assert 'match_search_truncated' in assessment['blockers']


def test_policy_rejects_truncated_search_with_only_unmerged_candidate(
        tmp_path):
    editor, journal_path, _ = _make_editor(tmp_path)
    candidate = _make_classified_candidate(
        editor, journal_path, _prediction(leaf_sample_count=10))
    candidate.match_evidence = matching.MatchEvidence(
        cleared_posting_matches=0,
        uncleared_posting_matches=0,
        unknown_postings_removed=0,
        search_truncated=True,
    )

    assessment = agent.assess_candidates(
        _candidate_set([candidate]),
        existing_accounts=['Assets:Checking', 'Expenses:Food',
                           'Expenses:Other'],
        policy=agent.AutoAcceptPolicy(),
    )

    assert assessment['auto_accept_eligible'] is False
    assert 'match_search_truncated' in assessment['blockers']


def test_loaded_reconciler_propagates_empty_search_truncation_to_raw_candidate(
        tmp_path, monkeypatch):
    editor, journal_path, _ = _make_editor(tmp_path)
    transaction = _transaction(account='Expenses:Food')
    pending = reconcile.PendingEntry(
        date=transaction.date,
        entries=[transaction],
        source=None,
        info=None,
        formatted=agent.format_entry(transaction),
        id='pending-id',
    )
    loaded = object.__new__(reconcile.LoadedReconciler)
    loaded.posting_db = object()
    loaded.pending_data = [pending]
    loaded.sources = []
    loaded._get_primary_transaction_amount_number = lambda entry: D('-10')
    loaded._get_unknown_account_prediction_evidence = lambda entry: []

    def make_candidate(transaction, used_transactions,
                       account_prediction_evidence, match_evidence):
        stage = editor.stage_changes()
        stage.add_entry(transaction, journal_path)
        return reconcile.Candidate(
            staged_changes=stage,
            staged_changes_with_unique_account_names=stage,
            used_import_results=used_transactions,
            used_transactions=used_transactions,
            match_evidence=match_evidence,
            account_prediction_evidence=account_prediction_evidence,
        )

    loaded._make_candidate_with_substitutions = make_candidate
    monkeypatch.setattr(
        matching, 'get_extended_transactions_with_evidence',
        lambda transaction, posting_db: matching.ScoredMergedTransactions(
            [], search_truncated=True))

    candidates = loaded._make_candidates_from_import_result(pending)
    assert len(candidates.candidates) == 1
    assert candidates.candidates[0].match_evidence.search_truncated is True
    assessment = agent.assess_candidates(
        candidates,
        existing_accounts=['Assets:Checking', 'Expenses:Food'],
        policy=agent.AutoAcceptPolicy(),
    )
    assert assessment['auto_accept_eligible'] is False
    assert 'match_search_truncated' in assessment['blockers']


def test_decision_tree_prediction_evidence_includes_probability_and_support():
    import nltk
    import sklearn.tree

    coffee = training.PredictionInput(
        source_account='Assets:Checking',
        amount=Amount(number=D('-10'), currency='USD'),
        date=datetime.date(2024, 1, 1),
        key_value_pairs={'description': 'coffee shop'},
    )
    groceries = training.PredictionInput(
        source_account='Assets:Checking',
        amount=Amount(number=D('-10'), currency='USD'),
        date=datetime.date(2024, 1, 1),
        key_value_pairs={'description': 'grocery store'},
    )
    classifier = nltk.classify.scikitlearn.SklearnClassifier(
        estimator=sklearn.tree.DecisionTreeClassifier(random_state=0))
    classifier.train(
        [(training.get_features(coffee), 'Expenses:Food')] * 5 +
        [(training.get_features(groceries), 'Expenses:Groceries')] * 2)

    loaded = object.__new__(reconcile.LoadedReconciler)
    loaded.classifier = classifier
    loaded.classifier_model_trusted = True
    loaded.classifier_model_reason = None

    evidence = loaded.predict_account_with_evidence(coffee)

    assert evidence.predicted_account == 'Expenses:Food'
    assert evidence.probability == 1.0
    assert evidence.margin == 1.0
    assert evidence.leaf_sample_count == 5
    assert 'description:coffee' in evidence.recognized_value_features

    unseen = training.PredictionInput(
        source_account='Assets:Checking',
        amount=Amount(number=D('-10'), currency='USD'),
        date=datetime.date(2024, 1, 1),
        key_value_pairs={'description': 'xylophone nebula'},
    )
    unseen_evidence = loaded.predict_account_with_evidence(unseen)
    assert unseen_evidence.recognized_value_features == []


def test_retrain_rebuilds_examples_from_the_current_journal():
    loaded = object.__new__(reconcile.LoadedReconciler)
    loaded.editor = types.SimpleNamespace(entries=['current-entry'])
    loaded.training_examples = training.TrainingExamples()
    loaded.training_examples.training_examples.append(({
        'stale': True
    }, 'Expenses:Stale'))

    class Extractor:
        def extract_examples(self, entries, examples):
            assert entries == ['current-entry']
            examples.training_examples.append(({
                'current': True
            }, 'Expenses:Current'))

    loaded._feature_extractor = Extractor()
    trained = []
    loaded._maybe_train_classifier = lambda: trained.extend(
        loaded.training_examples.training_examples)

    loaded.retrain()

    assert trained == [({'current': True}, 'Expenses:Current')]
    assert loaded.training_examples.training_examples == trained


def test_fingerprint_and_preview_do_not_modify_shared_candidate(tmp_path):
    editor, journal_path, ignored_path = _make_editor(tmp_path)
    candidate = _make_classified_candidate(
        editor, journal_path, _prediction(leaf_sample_count=10))
    original_stage = candidate.staged_changes
    original_accounts = [
        substitution.account_name
        for substitution in candidate.substituted_accounts
    ]
    original_diff = original_stage.get_textual_diff()
    original_fingerprint = agent.get_candidate_fingerprint(candidate, 0)

    preview = agent.make_preview(candidate, 'ignore', ignored_path)

    assert candidate.staged_changes is original_stage
    assert [
        substitution.account_name
        for substitution in candidate.substituted_accounts
    ] == original_accounts
    assert candidate.staged_changes.get_textual_diff() == original_diff
    assert agent.get_candidate_fingerprint(candidate, 0) == original_fingerprint
    assert 'Expenses:Food' in original_diff
    assert 'Expenses:FIXME' in preview['diff']
    assert preview['action'] == 'ignore'
    assert preview['modified_filenames'] == [ignored_path]


def test_ignore_rejects_moving_an_existing_journal_transaction(tmp_path):
    _, journal_path, ignored_path = _make_editor(tmp_path)
    with open(journal_path, 'a', encoding='utf-8') as f:
        f.write('\n2020-01-02 * "Existing FIXME"\n'
                '  Assets:Checking  -10 USD\n'
                '  Expenses:FIXME   10 USD\n')
    editor = journal_editor.JournalEditor(journal_path, ignored_path)
    old_entry = next(
        entry for entry in editor.entries if isinstance(entry, Transaction))
    stage = editor.stage_changes()
    stage.change_entry(old_entry, old_entry._replace(narration='Changed'))
    candidate = reconcile.Candidate(
        staged_changes=stage,
        staged_changes_with_unique_account_names=stage,
        used_import_results=[old_entry],
        used_transactions=[old_entry],
    )

    with pytest.raises(ValueError, match='cannot move or modify'):
        agent.prepare_candidate_for_action(candidate, 'ignore')


def test_ignore_strips_auxiliary_missing_account_open(tmp_path):
    editor, journal_path, _ = _make_editor(tmp_path)
    transaction = _transaction(account='Expenses:FIXME')
    stage = editor.stage_changes()
    stage.add_entry(transaction, journal_path)
    stage.add_entry(
        Open(
            meta=None,
            date=datetime.date(2020, 1, 1),
            account='Expenses:FIXME',
            currencies=['USD'],
            booking=None), journal_path)
    candidate = reconcile.Candidate(
        staged_changes=stage,
        staged_changes_with_unique_account_names=stage,
        used_import_results=[transaction],
        used_transactions=[transaction],
    )

    prepared = agent.prepare_candidate_for_action(candidate, 'ignore')
    staged_pairs = [
        pair
        for pairs in prepared.staged_changes.changed_entries.values()
        for pair in pairs
    ]
    assert len(staged_pairs) == 1
    assert staged_pairs[0][0] is None
    assert isinstance(staged_pairs[0][1], Transaction)
