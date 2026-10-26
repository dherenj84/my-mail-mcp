from unittest.mock import MagicMock, call

import pytest
from imapclient import IMAPClient
from imapclient.imapclient import ARCHIVE, TRASH

from my_mail_mcp.config import Settings
from my_mail_mcp.mail.operations import MailboxOperations
from my_mail_mcp.mail.service import MailService


@pytest.fixture
def mailbox(monkeypatch):
    mail = MailService(
        Settings(
            _env_file=None,
            mail_username="test@example.com",
            mail_password="secret",
            mcp_auth_token="x" * 32,
            mail_archive_folder=None,
            mail_trash_folder=None,
        ).accounts()["default"]
    )
    client = MagicMock()
    client.select_folder.return_value = {b"UIDVALIDITY": 5, b"READ-WRITE": True}
    client.fetch.return_value = {7: {b"FLAGS": (b"\\Answered",)}, 8: {b"FLAGS": ()}}
    client.list_folders.return_value = [
        ((), b"/", "INBOX"),
        ((ARCHIVE,), b"/", "Archive"),
        ((TRASH,), b"/", "Trash"),
        ((), b"/", "Work"),
        ((), b"/", "Other"),
    ]
    client.has_capability.side_effect = lambda capability: (
        capability in {"MOVE", "UIDPLUS"}
    )
    client.find_special_folder.side_effect = lambda flag: {
        ARCHIVE: "Archive",
        TRASH: "Trash",
    }.get(flag)
    client.folder_status.return_value = {b"UIDVALIDITY": 5, b"MESSAGES": 0}
    context = MagicMock()
    context.__enter__.return_value = client
    monkeypatch.setattr(mail, "connection", lambda: context)
    return MailboxOperations(mail), client


def test_flags_preserve_unrelated_state(mailbox):
    ops, client = mailbox
    result = ops.update_flags("INBOX", [7, 8], 5, read=True, starred=False)
    client.add_flags.assert_called_once_with([7, 8], [r"\Seen"], silent=True)
    client.remove_flags.assert_called_once_with([7, 8], [r"\Flagged"], silent=True)
    client.set_flags.assert_not_called()
    client.select_folder.assert_called_once_with("INBOX", readonly=False)
    assert result["uids"] == [7, 8]


@pytest.mark.parametrize(
    "uids", [[], [0], [-1], [True], ["7"], [4294967296], [7, 7], list(range(1, 102))]
)
def test_invalid_uid_batches_do_not_write(mailbox, uids):
    ops, client = mailbox
    with pytest.raises(ValueError):
        ops.permanently_delete_messages("INBOX", uids, 5)
    client.delete_messages.assert_not_called()
    client.uid_expunge.assert_not_called()


def test_stale_uidvalidity_rejects_mutation(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="identifiers changed"):
        ops.move_messages("INBOX", [7], 4, "Work")
    client.fetch.assert_not_called()
    client.move.assert_not_called()
    client.copy.assert_not_called()


def test_missing_member_aborts_entire_batch(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="no longer exist"):
        ops.update_flags("INBOX", [7, 99], 5, read=True)
    client.add_flags.assert_not_called()


def test_readonly_folder_rejects_write(mailbox):
    ops, client = mailbox
    client.select_folder.return_value[b"READ-WRITE"] = False
    with pytest.raises(ValueError, match="read-only"):
        ops.update_flags("INBOX", [7], 5, read=True)
    client.add_flags.assert_not_called()


def test_atomic_move_does_not_expunge(mailbox):
    ops, client = mailbox
    result = ops.move_messages("INBOX", [7, 8], 5, "Work")
    client.move.assert_called_once_with([7, 8], "Work")
    client.copy.assert_not_called()
    client.delete_messages.assert_not_called()
    client.expunge.assert_not_called()
    assert result["destination"] == "Work"
    assert result["refresh_destination"]


def test_fallback_moves_only_requested_uids(mailbox):
    ops, client = mailbox
    client.has_capability.side_effect = lambda capability: capability == "UIDPLUS"
    ops.move_messages("INBOX", [7], 5, "Work")
    assert client.method_calls[-3:] == [
        call.copy([7], "Work"),
        call.delete_messages([7], silent=True),
        call.uid_expunge([7]),
    ]
    client.expunge.assert_not_called()


def test_move_without_safe_capabilities_changes_nothing(mailbox):
    ops, client = mailbox
    client.has_capability.side_effect = lambda capability: False
    with pytest.raises(ValueError, match="nothing was changed"):
        ops.move_messages("INBOX", [7], 5, "Work")
    client.copy.assert_not_called()
    client.delete_messages.assert_not_called()


def test_copy_failure_never_deletes_original(mailbox):
    ops, client = mailbox
    client.has_capability.side_effect = lambda capability: capability == "UIDPLUS"
    client.copy.side_effect = IMAPClient.Error("copy failed")
    with pytest.raises(IMAPClient.Error):
        ops.move_messages("INBOX", [7], 5, "Work")
    client.delete_messages.assert_not_called()
    client.uid_expunge.assert_not_called()


def test_copy_keeps_originals(mailbox):
    ops, client = mailbox
    ops.copy_messages("INBOX", [7], 5, "Work")
    client.copy.assert_called_once_with([7], "Work")
    client.move.assert_not_called()
    client.delete_messages.assert_not_called()


@pytest.mark.parametrize("destination", ["inbox", "Missing"])
def test_invalid_destination_has_no_mutations(mailbox, destination):
    ops, client = mailbox
    with pytest.raises(ValueError):
        ops.move_messages("INBOX", [7], 5, destination)
    client.move.assert_not_called()
    client.copy.assert_not_called()


@pytest.mark.parametrize(
    "method,destination", [("archive_messages", "Archive"), ("trash_messages", "Trash")]
)
def test_special_folder_discovery(mailbox, method, destination):
    ops, client = mailbox
    getattr(ops, method)("INBOX", [7], 5)
    client.move.assert_called_once_with([7], destination)


def test_special_folder_override(mailbox):
    ops, client = mailbox
    ops.mail.settings.archive_folder = "Work"
    ops.archive_messages("INBOX", [7], 5)
    client.move.assert_called_once_with([7], "Work")
    client.find_special_folder.assert_not_called()


def test_unresolved_archive_requires_configuration(mailbox):
    ops, client = mailbox
    client.find_special_folder.side_effect = None
    client.find_special_folder.return_value = None
    with pytest.raises(ValueError, match="MAIL_ARCHIVE_FOLDER"):
        ops.archive_messages("INBOX", [7], 5)
    client.move.assert_not_called()


def test_restore_uses_current_trash_uids(mailbox):
    ops, client = mailbox
    ops.restore_messages("Trash", [7], 5)
    client.move.assert_called_once_with([7], "INBOX")


def test_restore_rejects_nontrash_source(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="resolved Trash"):
        ops.restore_messages("Work", [7], 5)
    client.move.assert_not_called()


def test_permanent_delete_is_uid_scoped(mailbox):
    ops, client = mailbox
    ops.permanently_delete_messages("Trash", [7], 5)
    client.delete_messages.assert_called_once_with([7], silent=True)
    client.uid_expunge.assert_called_once_with([7])
    client.expunge.assert_not_called()


def test_permanent_delete_without_uidplus_does_not_mark_deleted(mailbox):
    ops, client = mailbox
    client.has_capability.side_effect = lambda capability: capability == "MOVE"
    with pytest.raises(ValueError, match="UIDPLUS"):
        ops.permanently_delete_messages("Trash", [7], 5)
    client.delete_messages.assert_not_called()


def test_folder_creation_and_duplicate_rejection(mailbox):
    ops, client = mailbox
    ops.create_folder("New")
    client.create_folder.assert_called_once_with("New")
    with pytest.raises(ValueError, match="already exists"):
        ops.create_folder("Work")


@pytest.mark.parametrize("name", ["", " ", "Bad\r\nName", "\x00"])
def test_invalid_folder_name(mailbox, name):
    ops, client = mailbox
    with pytest.raises(ValueError):
        ops.create_folder(name)
    client.create_folder.assert_not_called()


def test_wildcards_are_not_treated_as_existing_folder(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="does not exist"):
        ops.move_messages("INBOX", [7], 5, "*")
    client.move.assert_not_called()


def test_rename_folder(mailbox):
    ops, client = mailbox
    ops.rename_folder("Work", "Renamed", 5)
    client.rename_folder.assert_called_once_with("Work", "Renamed")


@pytest.mark.parametrize("folder", ["INBOX", "Archive", "Trash"])
@pytest.mark.parametrize("method", ["rename_folder", "delete_folder"])
def test_system_folder_protection(mailbox, folder, method):
    ops, client = mailbox
    with pytest.raises(ValueError, match="System folders"):
        if method == "rename_folder":
            ops.rename_folder(folder, "Renamed", 5)
        else:
            ops.delete_folder(folder, 5)
    client.rename_folder.assert_not_called()
    client.delete_folder.assert_not_called()


def test_rename_rejects_existing_destination(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="already exists"):
        ops.rename_folder("Work", "Other", 5)
    client.rename_folder.assert_not_called()


def test_delete_empty_folder(mailbox):
    ops, client = mailbox
    ops.delete_folder("Work", 5)
    client.delete_folder.assert_called_once_with("Work")


def test_delete_nonempty_folder_rejected(mailbox):
    ops, client = mailbox
    client.folder_status.return_value[b"MESSAGES"] = 1
    with pytest.raises(ValueError, match="not empty"):
        ops.delete_folder("Work", 5)
    client.delete_folder.assert_not_called()


def test_folder_with_children_rejected(mailbox):
    ops, client = mailbox
    client.list_folders.return_value.append(((), b"/", "Work/Child"))
    with pytest.raises(ValueError, match="child folders"):
        ops.delete_folder("Work", 5)
    client.delete_folder.assert_not_called()


def test_stale_folder_version_rejects_delete(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="identifiers changed"):
        ops.delete_folder("Work", 4)
    client.delete_folder.assert_not_called()


def test_no_flag_changes_rejected(mailbox):
    ops, client = mailbox
    with pytest.raises(ValueError, match="at least one"):
        ops.update_flags("INBOX", [7], 5)
    client.add_flags.assert_not_called()
    client.remove_flags.assert_not_called()


def test_noselect_destination_rejected_case_insensitively(mailbox):
    ops, client = mailbox
    client.list_folders.return_value.append(((rb"\NOSELECT",), b"/", "Container"))
    with pytest.raises(ValueError, match="cannot contain messages"):
        ops.move_messages("INBOX", [7], 5, "Container")
    client.move.assert_not_called()


def test_system_flags_case_insensitive(mailbox):
    ops, client = mailbox
    client.list_folders.return_value.append(((rb"\SENT",), b"/", "Sent Mail"))
    with pytest.raises(ValueError, match="System folders"):
        ops.delete_folder("Sent Mail", 5)
    client.delete_folder.assert_not_called()


def test_special_folder_name_fallback_is_protected(mailbox):
    ops, client = mailbox
    client.list_folders.return_value = [((), b"/", "INBOX"), ((), b"/", "Trash")]
    with pytest.raises(ValueError, match="System folders"):
        ops.delete_folder("Trash", 5)
    client.delete_folder.assert_not_called()


def test_copy_then_delete_failure_does_not_expunge_or_retry(mailbox):
    ops, client = mailbox
    client.has_capability.side_effect = lambda capability: capability == "UIDPLUS"
    client.delete_messages.side_effect = IMAPClient.Error("store failed")
    with pytest.raises(IMAPClient.Error):
        ops.move_messages("INBOX", [7], 5, "Work")
    client.copy.assert_called_once_with([7], "Work")
    client.uid_expunge.assert_not_called()
    client.expunge.assert_not_called()
