"""Multi-workspace OAuth stores for the Ledgr Slack app (plan task #5.1).

Makes the bot installable into MANY client workspaces via Slack OAuth. Slack
Bolt's ``InstallationStoreAuthorize`` resolves the per-team bot token from a
custom :class:`slack_sdk.oauth.installation_store.InstallationStore`; we persist
installs to Firestore ``workspaces/{key}`` and OAuth ``state`` values to
``oauth_states/{state}``.

Mirrors the lazy/​injectable pattern of ``FirestoreClientStore`` (see
``invoice_processing/export/client_context.py``):

* ``google.cloud.firestore`` is imported lazily so importing this module never
  requires the dependency and no Firestore call is made on construction.
* A ``client=`` injection seam bypasses the real ``firestore.Client``
  entirely — ``_db()`` returns the injected object directly (hermetic tests).

Firestore layout::

    workspaces/{enterprise_id|none}-{team_id|none}  -> Installation.to_dict()
    oauth_states/{state}                            -> { "created_at": <epoch> }
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Optional

from slack_sdk.oauth.installation_store import Bot, Installation, InstallationStore
from slack_sdk.oauth.state_store import OAuthStateStore


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _key(
    enterprise_id: Optional[str],
    team_id: Optional[str],
    is_enterprise_install: Optional[bool] = None,
) -> str:
    """Composite Firestore doc key for a workspace install.

    For enterprise installs the bot token is shared org-wide, so we key by the
    enterprise only (team component collapses to ``none``); otherwise we key by
    the ``{enterprise_id|none}-{team_id|none}`` pair.
    """
    if is_enterprise_install:
        return f"{enterprise_id or 'none'}-none"
    return f"{enterprise_id or 'none'}-{team_id or 'none'}"


def _installation_init_keys() -> set[str]:
    """Keys accepted by ``Installation.__init__`` (excluding ``self``).

    Used to filter a stored dict down to the constructor's accepted kwargs so
    reconstruction never throws if Firestore holds extra/legacy fields or the
    serialized shape drifts from the constructor across slack_sdk versions.
    """
    params = inspect.signature(Installation.__init__).parameters
    return {name for name in params if name != "self"}


def _filtered(data: dict) -> dict:
    """Drop keys not accepted by ``Installation.__init__`` (defensive)."""
    accepted = _installation_init_keys()
    return {k: v for k, v in data.items() if k in accepted}


# --------------------------------------------------------------------------- #
# Installation store (workspaces/{key})
# --------------------------------------------------------------------------- #
class FirestoreInstallationStore(InstallationStore):
    """Persist Slack OAuth installs to Firestore ``workspaces/{key}``.

    The ``client`` injection seam mirrors ``FirestoreClientStore``: pass
    ``client=<fake>`` to bypass real ``firestore.Client`` construction entirely
    (``_db()`` returns the injected object directly — no network).
    """

    def __init__(self, *, collection: str = "workspaces", client=None, logger=None):
        from ledgr_slack.config import _ns
        self._collection = _ns(collection)
        self._injected_client = client  # test seam: if set, _db() returns it directly
        self._client = None  # lazy real client
        self._logger = logger

    @property
    def logger(self):  # InstallationStore exposes a `logger` property
        return self._logger

    def _db(self):
        # If a client was injected (e.g. in tests), return it without touching GCP.
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            from google.cloud import firestore  # lazy import — never loaded in tests

            self._client = firestore.Client()
        return self._client

    def _doc(self, key: str):
        return self._db().collection(self._collection).document(key)

    # ---- writes ----

    def save(self, installation: Installation) -> None:
        """Write ``installation.to_dict()`` to ``workspaces/{key}`` (full replace)."""
        key = _key(
            installation.enterprise_id,
            installation.team_id,
            installation.is_enterprise_install,
        )
        self._doc(key).set(installation.to_dict(), merge=False)

    def save_bot(self, bot: Bot) -> None:
        """Persist bot fields.

        ``save()`` already stores the full installation (including every bot
        field), so the bot is recoverable from the same workspace doc. We still
        write the bot dict (merge) so a bare ``save_bot`` call is also durable.
        """
        key = _key(bot.enterprise_id, bot.team_id, bot.is_enterprise_install)
        self._doc(key).set(bot.to_dict(), merge=True)

    # ---- reads ----

    def find_installation(
        self,
        *,
        enterprise_id: Optional[str],
        team_id: Optional[str],
        user_id: Optional[str] = None,
        is_enterprise_install: Optional[bool] = False,
    ) -> Optional[Installation]:
        key = _key(enterprise_id, team_id, is_enterprise_install)
        snap = self._doc(key).get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        # Filter to constructor-accepted keys so reconstruction never throws.
        return Installation(**_filtered(data))

    def find_bot(
        self,
        *,
        enterprise_id: Optional[str],
        team_id: Optional[str],
        is_enterprise_install: Optional[bool] = False,
    ) -> Optional[Bot]:
        installation = self.find_installation(
            enterprise_id=enterprise_id,
            team_id=team_id,
            is_enterprise_install=is_enterprise_install,
        )
        if installation is None:
            return None
        return installation.to_bot()

    # ---- deletes (best-effort / defensive) ----

    def delete_installation(
        self,
        *,
        enterprise_id: Optional[str],
        team_id: Optional[str],
        user_id: Optional[str] = None,
    ) -> None:
        for is_ent in (False, True):
            try:
                self._doc(_key(enterprise_id, team_id, is_ent)).delete()
            except Exception:  # noqa: BLE001 — best-effort delete
                pass

    def delete_bot(
        self,
        *,
        enterprise_id: Optional[str],
        team_id: Optional[str],
    ) -> None:
        for is_ent in (False, True):
            try:
                self._doc(_key(enterprise_id, team_id, is_ent)).delete()
            except Exception:  # noqa: BLE001 — best-effort delete
                pass

    def delete_all(
        self,
        *,
        enterprise_id: Optional[str],
        team_id: Optional[str],
    ) -> None:
        self.delete_installation(enterprise_id=enterprise_id, team_id=team_id)
        self.delete_bot(enterprise_id=enterprise_id, team_id=team_id)

    # ---- async variants (AsyncApp / AsyncOAuthFlow + per-event authorize) ----
    # bolt-python's AsyncApp calls the ``async_*`` methods: AsyncOAuthFlow uses
    # ``async_save`` on the OAuth callback, and AsyncInstallationStoreAuthorize
    # uses ``async_find_bot`` / ``async_find_installation`` on EVERY inbound event
    # to resolve the per-workspace token. The sync slack_sdk base provides no async
    # delegators, so we add them here and offload the blocking Firestore I/O to a
    # worker thread to keep the event loop responsive.

    async def async_save(self, installation: Installation) -> None:
        await asyncio.to_thread(self.save, installation)

    async def async_save_bot(self, bot: Bot) -> None:
        await asyncio.to_thread(self.save_bot, bot)

    async def async_find_installation(self, **kwargs) -> Optional[Installation]:
        return await asyncio.to_thread(self.find_installation, **kwargs)

    async def async_find_bot(self, **kwargs) -> Optional[Bot]:
        return await asyncio.to_thread(self.find_bot, **kwargs)

    async def async_delete_installation(self, **kwargs) -> None:
        await asyncio.to_thread(self.delete_installation, **kwargs)

    async def async_delete_bot(self, **kwargs) -> None:
        await asyncio.to_thread(self.delete_bot, **kwargs)

    async def async_delete_all(self, **kwargs) -> None:
        await asyncio.to_thread(self.delete_all, **kwargs)


# --------------------------------------------------------------------------- #
# OAuth state store (oauth_states/{state})
# --------------------------------------------------------------------------- #
class FirestoreOAuthStateStore(OAuthStateStore):
    """Persist OAuth ``state`` values to Firestore ``oauth_states/{state}``.

    Same lazy/​injectable seam as :class:`FirestoreInstallationStore`.
    """

    def __init__(
        self,
        *,
        collection: str = "oauth_states",
        expiration_seconds: int = 600,
        client=None,
    ):
        from ledgr_slack.config import _ns
        self._collection = _ns(collection)
        self._expiration_seconds = expiration_seconds
        self._injected_client = client  # test seam: if set, _db() returns it directly
        self._client = None  # lazy real client

    def _db(self):
        if self._injected_client is not None:
            return self._injected_client
        if self._client is None:
            from google.cloud import firestore  # lazy import — never loaded in tests

            self._client = firestore.Client()
        return self._client

    def _doc(self, state: str):
        return self._db().collection(self._collection).document(state)

    def issue(self, *args, **kwargs) -> str:
        """Generate a random ``state``, persist its creation time, return it."""
        state = uuid.uuid4().hex
        self._doc(state).set({"created_at": time.time()}, merge=False)
        return state

    def consume(self, state: str) -> bool:
        """One-time-use check: delete + return True if present and unexpired."""
        doc = self._doc(state)
        snap = doc.get()
        if not snap.exists:
            return False
        data = snap.to_dict() or {}
        created_at = data.get("created_at", 0)
        if (time.time() - created_at) > self._expiration_seconds:
            doc.delete()
            return False
        doc.delete()
        return True

    # ---- async variants (AsyncOAuthFlow issues/consumes state via async_*) ----

    async def async_issue(self, *args, **kwargs) -> str:
        return await asyncio.to_thread(self.issue, *args, **kwargs)

    async def async_consume(self, state: str) -> bool:
        return await asyncio.to_thread(self.consume, state)
