"""Mailbox mutations using UID-based IMAP commands on request-owned connections."""

from imapclient.imapclient import ARCHIVE, DRAFTS, JUNK, SENT, TRASH

from my_mail_mcp.mail.service import MailService


class MailboxOperations:
    def __init__(self, mail: MailService):
        self.mail = mail

    @staticmethod
    def _folder_name(folder: str) -> None:
        if not folder.strip() or any(ord(c) < 32 or ord(c) == 127 for c in folder):
            raise ValueError(
                "Folder name must be nonempty and contain no control characters"
            )

    @staticmethod
    def _same_folder(first: str, second: str) -> bool:
        return first == second or first.upper() == second.upper() == "INBOX"

    @staticmethod
    def _uids(uids: list[int]) -> list[int]:
        if not 1 <= len(uids) <= 100 or any(
            type(uid) is not int or not 1 <= uid <= 4294967295 for uid in uids
        ):
            raise ValueError("Provide 1–100 positive integer UIDs (at most 4294967295)")
        if len(set(uids)) != len(uids):
            raise ValueError("Duplicate UIDs are not allowed")
        return uids

    def _select(self, client, folder: str, uids: list[int], uid_validity: int):
        self._folder_name(folder)
        self._uids(uids)
        selected = client.select_folder(folder, readonly=False)
        if selected[b"UIDVALIDITY"] != uid_validity:
            raise ValueError(
                "Folder identifiers changed; search again before modifying messages"
            )
        if selected.get(b"READ-WRITE") is False:
            raise ValueError("Folder is read-only")
        found = client.fetch(uids, ["FLAGS"])
        if set(uids) - set(found):
            raise ValueError(
                "Some messages no longer exist; search again before modifying messages"
            )

    def _existing_folder(self, client, folder: str):
        self._folder_name(folder)
        for item in client.list_folders():
            if self._same_folder(item[2], folder):
                return item
        raise ValueError("Folder does not exist; list folders again")

    def _special_folder(self, client, role: str) -> str:
        override = getattr(self.mail.settings, f"mail_{role}_folder")
        folder = override or client.find_special_folder(
            {"archive": ARCHIVE, "trash": TRASH}[role]
        )
        if not folder:
            raise ValueError(
                f"Could not resolve {role} folder; configure MAIL_{role.upper()}_FOLDER"
            )
        self._existing_folder(client, folder)
        return folder

    @staticmethod
    def _result(
        operation: str, folder: str, uids: list[int], uid_validity: int, **extra
    ):
        return {
            "operation": operation,
            "folder": folder,
            "uids": uids,
            "uid_validity": uid_validity,
            **extra,
        }

    def update_flags(
        self,
        folder: str,
        uids: list[int],
        uid_validity: int,
        read: bool | None = None,
        starred: bool | None = None,
        answered: bool | None = None,
    ) -> dict:
        changes = {r"\Seen": read, r"\Flagged": starred, r"\Answered": answered}
        if all(value is None for value in changes.values()):
            raise ValueError("Specify at least one of read, starred, or answered")
        with self.mail.connection() as client:
            self._select(client, folder, uids, uid_validity)
            add = [flag for flag, value in changes.items() if value is True]
            remove = [flag for flag, value in changes.items() if value is False]
            if add:
                client.add_flags(uids, add, silent=True)
            if remove:
                client.remove_flags(uids, remove, silent=True)
            return self._result("update_flags", folder, uids, uid_validity)

    def _transfer(
        self,
        client,
        folder: str,
        uids: list[int],
        uid_validity: int,
        destination: str,
        copy: bool = False,
    ) -> dict:
        if self._same_folder(folder, destination):
            raise ValueError("Source and destination must be different folders")
        destination_info = self._existing_folder(client, destination)
        if rb"\noselect" in {flag.lower() for flag in destination_info[0]}:
            raise ValueError("Destination cannot contain messages")
        self._select(client, folder, uids, uid_validity)
        if copy:
            client.copy(uids, destination)
        elif client.has_capability("MOVE"):
            client.move(uids, destination)
        elif client.has_capability("UIDPLUS"):
            # Never use unscoped EXPUNGE; it also deletes unrelated flagged mail.
            client.copy(uids, destination)
            client.delete_messages(uids, silent=True)
            client.uid_expunge(uids)
        else:
            raise ValueError(
                "Moving requires server support for MOVE or UIDPLUS; nothing was changed"
            )
        return self._result(
            "copy" if copy else "move",
            folder,
            uids,
            uid_validity,
            destination=destination,
            refresh_destination=True,
        )

    def move_messages(
        self, folder: str, uids: list[int], uid_validity: int, destination: str
    ) -> dict:
        with self.mail.connection() as client:
            return self._transfer(client, folder, uids, uid_validity, destination)

    def copy_messages(
        self, folder: str, uids: list[int], uid_validity: int, destination: str
    ) -> dict:
        with self.mail.connection() as client:
            return self._transfer(
                client, folder, uids, uid_validity, destination, copy=True
            )

    def archive_messages(self, folder: str, uids: list[int], uid_validity: int) -> dict:
        with self.mail.connection() as client:
            destination = self._special_folder(client, "archive")
            return self._transfer(client, folder, uids, uid_validity, destination)

    def trash_messages(self, folder: str, uids: list[int], uid_validity: int) -> dict:
        with self.mail.connection() as client:
            destination = self._special_folder(client, "trash")
            return self._transfer(client, folder, uids, uid_validity, destination)

    def restore_messages(
        self,
        folder: str,
        uids: list[int],
        uid_validity: int,
        destination: str = "INBOX",
    ) -> dict:
        with self.mail.connection() as client:
            if not self._same_folder(folder, self._special_folder(client, "trash")):
                raise ValueError(
                    "Restore requires messages in the resolved Trash folder"
                )
            return self._transfer(client, folder, uids, uid_validity, destination)

    def permanently_delete_messages(
        self, folder: str, uids: list[int], uid_validity: int
    ) -> dict:
        with self.mail.connection() as client:
            self._select(client, folder, uids, uid_validity)
            if not client.has_capability("UIDPLUS"):
                raise ValueError(
                    "Permanent deletion requires UIDPLUS; nothing was changed"
                )
            client.delete_messages(uids, silent=True)
            client.uid_expunge(uids)
            return self._result("permanently_delete", folder, uids, uid_validity)

    def create_folder(self, folder: str) -> dict:
        self._folder_name(folder)
        with self.mail.connection() as client:
            if any(
                self._same_folder(item[2], folder) for item in client.list_folders()
            ):
                raise ValueError("Folder already exists")
            client.create_folder(folder)
            return {"operation": "create_folder", "folder": folder}

    def _mutable_folder(self, client, folder: str, uid_validity: int):
        info = self._existing_folder(client, folder)
        protected = {
            b"\\Inbox",
            b"\\Archive",
            b"\\All",
            b"\\AllMail",
            b"\\Sent",
            b"\\Drafts",
            b"\\Junk",
            b"\\Trash",
            b"\\Important",
        }
        configured = [
            self.mail.settings.mail_archive_folder,
            self.mail.settings.mail_trash_folder,
        ]
        configured.extend(
            client.find_special_folder(flag)
            for flag in (ARCHIVE, TRASH, SENT, DRAFTS, JUNK)
        )
        if (
            folder.upper() == "INBOX"
            or {flag.lower() for flag in protected}.intersection(
                flag.lower() for flag in info[0]
            )
            or folder in configured
        ):
            raise ValueError("System folders cannot be renamed or deleted")
        delimiter = info[1].decode() if isinstance(info[1], bytes) else info[1]
        if delimiter and any(
            item[2].startswith(folder + delimiter) for item in client.list_folders()
        ):
            raise ValueError("Rename or delete child folders first")
        status = client.folder_status(folder, ["UIDVALIDITY", "MESSAGES"])
        if status[b"UIDVALIDITY"] != uid_validity:
            raise ValueError("Folder identifiers changed; search again")
        return status

    def rename_folder(self, folder: str, new_name: str, uid_validity: int) -> dict:
        self._folder_name(new_name)
        with self.mail.connection() as client:
            self._mutable_folder(client, folder, uid_validity)
            if any(
                self._same_folder(item[2], new_name) for item in client.list_folders()
            ):
                raise ValueError("Destination folder already exists")
            client.rename_folder(folder, new_name)
            return {
                "operation": "rename_folder",
                "folder": folder,
                "new_name": new_name,
                "refresh_destination": True,
            }

    def delete_folder(self, folder: str, uid_validity: int) -> dict:
        with self.mail.connection() as client:
            status = self._mutable_folder(client, folder, uid_validity)
            if status[b"MESSAGES"]:
                raise ValueError(
                    "Folder is not empty; move or delete its messages first"
                )
            client.delete_folder(folder)
            return {"operation": "delete_folder", "folder": folder}
