"""MCP tools for organizing messages and managing mailbox folders."""

from functools import wraps

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from imapclient import IMAPClient

from my_mail_mcp.mail.operations import MailboxOperations


def register_mailbox_tools(mcp: FastMCP, operations: MailboxOperations) -> None:
    def mutation(*, destructive: bool = True, idempotent: bool = False):
        def decorate(function):
            @wraps(function)
            def invoke(*args, **kwargs):
                try:
                    return function(*args, **kwargs)
                except ValueError as exc:
                    raise ToolError(str(exc)) from None
                except (OSError, IMAPClient.Error):
                    raise ToolError(
                        "Mailbox operation could not be completed. Changes may already have applied; "
                        "refresh affected folders before retrying."
                    ) from None

            return mcp.tool(
                invoke,
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": destructive,
                    "idempotentHint": idempotent,
                },
            )

        return decorate

    @mutation(destructive=False, idempotent=True)
    def update_flags(
        folder: str,
        uids: list[int],
        uid_validity: int,
        read: bool | None = None,
        starred: bool | None = None,
        answered: bool | None = None,
    ) -> dict:
        """Set read/unread, starred/unstarred, or answered state for 1–100 messages.

        Use UIDs and UIDVALIDITY from search_messages. Omitted flags stay unchanged.
        """
        return operations.update_flags(
            folder, uids, uid_validity, read, starred, answered
        )

    @mutation()
    def move_messages(
        folder: str, uids: list[int], uid_validity: int, destination: str
    ) -> dict:
        """Move 1–100 messages to an existing folder. Search destination for their new UIDs.

        Prefer atomic MOVE; fallback needs UIDPLUS and can partially succeed. Do not
        automatically retry failed writes. Source UIDs are invalid in the destination.
        """
        return operations.move_messages(folder, uids, uid_validity, destination)

    @mutation(destructive=False)
    def copy_messages(
        folder: str, uids: list[int], uid_validity: int, destination: str
    ) -> dict:
        """Copy 1–100 messages into an existing folder; originals remain. Retrying makes duplicates.

        Search the destination for new UIDs; source UIDs do not identify the copies.
        """
        return operations.copy_messages(folder, uids, uid_validity, destination)

    @mutation()
    def archive_messages(folder: str, uids: list[int], uid_validity: int) -> dict:
        """Move 1–100 messages to the detected or configured Archive folder. Search there afterward."""
        return operations.archive_messages(folder, uids, uid_validity)

    @mutation()
    def trash_messages(folder: str, uids: list[int], uid_validity: int) -> dict:
        """Move 1–100 messages to Trash, without permanently deleting them. Search Trash for new UIDs."""
        return operations.trash_messages(folder, uids, uid_validity)

    @mutation()
    def restore_messages(
        folder: str, uids: list[int], uid_validity: int, destination: str = "INBOX"
    ) -> dict:
        """Move 1–100 messages out of Trash into INBOX or an explicit destination.

        Source must be the detected/configured Trash. Original folders are not recorded.
        """
        return operations.restore_messages(folder, uids, uid_validity, destination)

    @mutation()
    def permanently_delete_messages(
        folder: str, uids: list[int], uid_validity: int
    ) -> dict:
        """Irreversibly delete exactly the specified 1–100 messages. Use trash_messages for recoverable deletion.

        Requires UIDPLUS. Never empties the folder or expunges unrelated messages.
        """
        return operations.permanently_delete_messages(folder, uids, uid_validity)

    @mutation(destructive=False)
    def create_folder(folder: str) -> dict:
        """Create a mailbox folder. Fails if it exists; use the server's hierarchy delimiter for subfolders."""
        return operations.create_folder(folder)

    @mutation()
    def rename_folder(folder: str, new_name: str, uid_validity: int) -> dict:
        """Rename a user folder with no child folders; messages remain inside it.

        Supply UIDVALIDITY from search_messages, even for an empty folder. System
        folders are protected. Search again after renaming to refresh references.
        """
        return operations.rename_folder(folder, new_name, uid_validity)

    @mutation()
    def delete_folder(folder: str, uid_validity: int) -> dict:
        """Delete an empty user folder with no children. System folders are protected.

        Supply UIDVALIDITY from search_messages. Move or delete contained messages first.
        """
        return operations.delete_folder(folder, uid_validity)
