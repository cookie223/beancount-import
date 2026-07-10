import datetime
import os

import beancount.parser.printer
from beancount.core.data import Transaction, Posting, EMPTY_SET
from beancount.core.number import MISSING, Decimal
from beancount.core.amount import Amount
import pytest
import py

from . import journal_editor
from . import test_util

META_IGNORE = set({'__tolerances__', '__automatic__'})


def clean_meta(meta):
    if meta is None:
        return {}
    return {k: v for k, v in meta.items() if k not in META_IGNORE}


def clean_posting(posting):
    return posting._replace(meta=clean_meta(posting.meta))


def clean_entry(entry):
    entry = entry._replace(meta=clean_meta(entry.meta))
    if isinstance(entry, Transaction):
        entry = entry._replace(
            postings=list(map(clean_posting, entry.postings)))
    return entry


def clean_entries(entries):
    return list(map(clean_entry, entries))


def check_file_contents(path, expected_contents):
    with open(path, 'r') as f:
        contents = f.read()
    assert contents == expected_contents


def check_journal_entries(editor: journal_editor.JournalEditor):
    new_editor = journal_editor.JournalEditor(editor.journal_path,
                                              editor.ignored_path)
    assert clean_entries(editor.entries) == clean_entries(new_editor.entries)
    assert clean_entries(editor.ignored_entries) == clean_entries(
        new_editor.ignored_entries)


def create_journal(tmpdir: py.path.local,
                   contents: str,
                   name: str = 'journal.beancount'):
    path = tmpdir.join(name)
    path.write_binary(contents.encode())
    return str(path)


def test_load_file_simple(tmpdir):
    """Tests that partial booking resolves the missing units."""
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    expected_entries = test_util.parse("""
        2015-02-01 * "Test transaction"
          Assets:Account-A  100 USD
          Assets:Account-B  -100 USD
        """)
    assert test_util.format_entries(
        editor.entries) == test_util.format_entries(expected_entries)


def test_load_file_reduction(tmpdir):
    """Tests that partial booking leaves the reduction alone."""
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Test transaction"
  Assets:Account-A  -5 STOCK {}
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    expected_entries = test_util.parse("""
        2015-02-01 * "Test transaction"
          Assets:Account-A  -5 STOCK {}
          Assets:Account-B
        """)
    assert test_util.format_entries(
        editor.entries) == test_util.format_entries(expected_entries)


def test_load_file_buy(tmpdir):
    """Tests that partial booking handles a posting with a cost."""
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Test transaction"
  Assets:Account-A  5 STOCK {3.00 USD}
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    expected_entries = test_util.parse("""
        2015-02-01 * "Test transaction"
          Assets:Account-A  5 STOCK {3.00 USD}
          Assets:Account-B -15.00 USD
        """)
    assert test_util.format_entries(
        editor.entries) == test_util.format_entries(expected_entries)


def test_simple(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[0]
    new_entry = old_entry._replace(postings=[
        old_entry.postings[0]._replace(meta=dict(note="Hello")),
        old_entry.postings[1],
    ])
    stage.change_entry(old_entry, new_entry)
    result = stage.apply()
    assert result.old_entries == [old_entry]
    assert result.old_ignored_entries == []
    assert result.new_ignored_entries == []
    check_file_contents(
        journal_path, """
2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
    note: "Hello"
  Assets:Account-B
""")
    check_journal_entries(editor)


def test_stale_writer_is_rejected_after_another_editor_commits(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Original"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    first_editor = journal_editor.JournalEditor(journal_path)
    second_editor = journal_editor.JournalEditor(journal_path)

    first_stage = first_editor.stage_changes()
    first_old = first_editor.entries[0]
    first_stage.change_entry(
        first_old, first_old._replace(narration='First writer'))

    second_stage = second_editor.stage_changes()
    second_old = second_editor.entries[0]
    second_stage.change_entry(
        second_old, second_old._replace(narration='Second writer'))

    first_stage.apply()
    with pytest.raises(RuntimeError, match='modified concurrently'):
        second_stage.apply()

    check_file_contents(
        journal_path, """
2015-02-01 * "First writer"
  Assets:Account-A  100 USD
  Assets:Account-B
""")


def test_post_write_failure_reports_that_journal_was_applied(
        tmpdir, monkeypatch):
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Original"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[0]
    stage.change_entry(old_entry, old_entry._replace(narration='Written'))

    def fail_booking(*args, **kwargs):
        raise RuntimeError('booking failed after write')

    monkeypatch.setattr(journal_editor.beancount.parser.booking, 'book',
                        fail_booking)
    with pytest.raises(journal_editor.JournalWriteAppliedError) as exc_info:
        stage.apply()

    assert exc_info.value.modified_filenames == [
        os.path.realpath(journal_path)
    ]
    assert exc_info.value.new_entries[0].narration == 'Written'
    assert isinstance(exc_info.value.cause, RuntimeError)
    check_file_contents(
        journal_path, """
2015-02-01 * "Written"
  Assets:Account-A  100 USD
  Assets:Account-B
""")


def test_directory_fsync_failure_after_rename_is_reported_as_applied(
        tmpdir, monkeypatch):
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Original"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[0]
    stage.change_entry(old_entry, old_entry._replace(narration='Written'))

    def fail_directory_sync(*args, **kwargs):
        raise RuntimeError('directory fsync failed after rename')

    monkeypatch.setattr(journal_editor.atomicwrites, '_sync_directory',
                        fail_directory_sync)
    with pytest.raises(journal_editor.JournalWriteAppliedError) as exc_info:
        stage.apply()

    assert exc_info.value.fully_written is True
    assert exc_info.value.applied_filenames == [
        os.path.realpath(journal_path)
    ]
    assert exc_info.value.intended_filenames == [
        os.path.realpath(journal_path)
    ]
    check_file_contents(
        journal_path, """
2015-02-01 * "Written"
  Assets:Account-A  100 USD
  Assets:Account-B
""")


def test_multi_file_failure_reports_only_the_files_that_were_applied(
        tmpdir, monkeypatch):
    second_path = create_journal(
        tmpdir, """
2015-02-02 * "Second original"
  Assets:Account-A  200 USD
  Assets:Account-B
""", name='second.beancount')
    journal_path = create_journal(
        tmpdir, """
include "second.beancount"

2015-02-01 * "First original"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    entries = {
        entry.narration: entry
        for entry in editor.entries if isinstance(entry, Transaction)
    }
    stage = editor.stage_changes()
    first = entries['First original']
    second = entries['Second original']
    stage.change_entry(first, first._replace(narration='First written'))
    stage.change_entry(second, second._replace(narration='Second written'))
    original_write = editor._write_file_changes_result

    def fail_second_write(filename, result):
        if os.path.realpath(filename) == os.path.realpath(second_path):
            raise RuntimeError('second file failed before write')
        return original_write(filename, result)

    monkeypatch.setattr(editor, '_write_file_changes_result',
                        fail_second_write)
    with pytest.raises(journal_editor.JournalWriteAppliedError) as exc_info:
        stage.apply()

    assert exc_info.value.fully_written is False
    assert exc_info.value.applied_filenames == [
        os.path.realpath(journal_path)
    ]
    assert exc_info.value.intended_filenames == [
        os.path.realpath(journal_path),
        os.path.realpath(second_path),
    ]
    assert [entry.narration for entry in exc_info.value.new_entries] == [
        'First written'
    ]
    assert 'First written' in open(journal_path, encoding='utf-8').read()
    assert 'Second written' not in open(second_path,
                                        encoding='utf-8').read()


def test_transaction_add_meta(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Test transaction"
  transaction_note: "Hello"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[0]
    new_entry = old_entry._replace(postings=[
        old_entry.postings[0]._replace(meta=dict(note="Hello")),
        old_entry.postings[1],
    ])
    stage.change_entry(old_entry, new_entry)
    result = stage.apply()
    assert result.old_ignored_entries == []
    assert result.new_ignored_entries == []
    assert result.old_entries == [old_entry]
    check_file_contents(
        journal_path, """
2015-02-01 * "Test transaction"
  transaction_note: "Hello"
  Assets:Account-A  100 USD
    note: "Hello"
  Assets:Account-B
""")
    check_journal_entries(editor)


def test_transaction_modify_narration(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[0]
    new_entry = old_entry._replace(narration='Modified transaction')
    stage.change_entry(old_entry, new_entry)
    result = stage.apply()
    assert result.old_entries == [old_entry]
    assert result.old_ignored_entries == []
    assert result.new_ignored_entries == []
    check_file_contents(
        journal_path, """
2015-02-01 * "Modified transaction"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    check_journal_entries(editor)


def test_two(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B

""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[1]
    new_entry = old_entry._replace(postings=[
        old_entry.postings[0]._replace(meta=dict(note="Hello")),
        old_entry.postings[1],
    ])
    stage.change_entry(old_entry, new_entry)
    result = stage.apply()
    assert result.old_entries == [old_entry]
    assert result.old_ignored_entries == []
    assert result.new_ignored_entries == []
    check_file_contents(
        journal_path, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
    note: "Hello"
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B

""")
    check_journal_entries(editor)


def test_two2(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B

""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[1]
    new_entry = old_entry._replace(postings=[
        old_entry.postings[0]._replace(meta=dict(note="Hello")),
        old_entry.postings[1],
    ])
    stage.change_entry(old_entry, new_entry)

    old_entry2 = editor.entries[2]
    new_entry2 = old_entry2._replace(postings=[
        old_entry2.postings[0],
        old_entry2.postings[1]._replace(meta=dict(note="Foo")),
    ])
    stage.change_entry(old_entry2, new_entry2)

    result = stage.apply()
    assert result.old_entries == [old_entry, old_entry2]
    assert result.old_ignored_entries == []
    assert result.new_ignored_entries == []
    check_file_contents(
        journal_path, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
    note: "Hello"
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
    note: "Foo"

""")
    check_journal_entries(editor)


def test_remove(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    old_entry = editor.entries[1]
    stage.remove_entry(old_entry)
    result = stage.apply()
    assert result.old_entries == [old_entry]
    assert result.new_entries == []
    assert result.old_ignored_entries == []
    assert result.new_ignored_entries == []
    check_file_contents(
        journal_path, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    check_journal_entries(editor)


def test_add(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    editor = journal_editor.JournalEditor(journal_path)
    stage = editor.stage_changes()
    new_transaction = Transaction(
        meta=None,
        date=datetime.date(2015, 4, 1),
        flag='*',
        payee=None,
        narration='New transaction',
        tags=EMPTY_SET,
        links=EMPTY_SET,
        postings=[
            Posting(
                account='Assets:Account-A',
                units=Amount(Decimal(3), 'USD'),
                cost=None,
                price=None,
                flag=None,
                meta=None),
            Posting(
                account='Assets:Account-B',
                units=MISSING,
                cost=None,
                price=None,
                flag=None,
                meta=None),
        ],
    )
    stage.add_entry(new_transaction, journal_path)
    result = stage.apply()
    check_file_contents(
        journal_path, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-04-01 * "New transaction"
  Assets:Account-A  3 USD
  Assets:Account-B
""")
    check_journal_entries(editor)

def test_add_ignored(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    ignored_path = create_journal(
        tmpdir, """
2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
""",
        name='ignored.beancount')
    editor = journal_editor.JournalEditor(journal_path, ignored_path)
    stage = editor.stage_changes()
    new_transaction = Transaction(
        meta=None,
        date=datetime.date(2015, 4, 1),
        flag='*',
        payee=None,
        narration='New transaction',
        tags=EMPTY_SET,
        links=EMPTY_SET,
        postings=[
            Posting(
                account='Assets:Account-A',
                units=Amount(Decimal(3), 'USD'),
                cost=None,
                price=None,
                flag=None,
                meta=None),
            Posting(
                account='Assets:Account-B',
                units=MISSING,
                cost=None,
                price=None,
                flag=None,
                meta=None),
        ],
    )
    stage.add_entry(new_transaction, ignored_path)
    result = stage.apply()
    check_file_contents(
        journal_path, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    check_file_contents(
        ignored_path, """
2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-04-01 * "New transaction"
  Assets:Account-A  3 USD
  Assets:Account-B
""")
    check_journal_entries(editor)

def test_remove_ignored(tmpdir):
    journal_path = create_journal(
        tmpdir, """
2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    ignored_path = create_journal(
        tmpdir,
        """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B

2015-02-01 * "Test transaction"
  Assets:Account-A  100 USD
  Assets:Account-B
""",
        name='ignored.beancount')
    editor = journal_editor.JournalEditor(journal_path, ignored_path)
    stage = editor.stage_changes()
    old_entry = editor.ignored_entries[1]
    stage.remove_entry(old_entry)
    result = stage.apply()
    assert result.old_ignored_entries == [old_entry]
    assert result.new_entries == []
    assert result.old_entries == []
    assert result.new_ignored_entries == []
    check_file_contents(
        journal_path, """
2015-03-01 * "Test transaction 2"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    check_file_contents(
        ignored_path, """
2015-01-01 * "Test transaction 1"
  Assets:Account-A  100 USD
  Assets:Account-B
""")
    check_journal_entries(editor)
