from __future__ import annotations

from threading import RLock

from .models import AuditEvent, DispatchRecord, StoredVersion, VersionStatus


class VersionNotFoundError(KeyError):
    pass


class ImmutableVersionError(ValueError):
    pass


class RuleRepository:
    """Thread-safe in-memory repository.

    The active pointer and version snapshots are protected by one lock. Each
    published :class:`StoredVersion` is itself immutable, so evaluators can use
    it after releasing the lock without observing field-level mixed versions.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._versions: dict[str, StoredVersion] = {}
        self._active_version: str | None = None
        self._audit: list[AuditEvent] = []
        self._dispatches: dict[str, DispatchRecord] = {}

    def save_version(self, stored: StoredVersion) -> None:
        with self._lock:
            existing = self._versions.get(stored.ruleset.version)
            if existing is not None:
                raise ImmutableVersionError(
                    f"version {stored.ruleset.version!r} already exists"
                )
            self._versions[stored.ruleset.version] = stored

    def get_version(self, version: str) -> StoredVersion:
        with self._lock:
            try:
                return self._versions[version]
            except KeyError as exc:
                raise VersionNotFoundError(version) from exc

    def active_version_ref(self) -> str | None:
        with self._lock:
            return self._active_version

    def active_snapshot(self) -> StoredVersion | None:
        with self._lock:
            if self._active_version is None:
                return None
            return self._versions[self._active_version]

    def commit_activation(
        self,
        version: str,
        expected_status: VersionStatus,
        previous_status: VersionStatus,
    ) -> StoredVersion:
        """Atomically publish/select a version and move the active pointer.

        The target snapshot is replaced atomically with a published copy;
        readers never see a published pointer pointing to a draft or a mix of
        fields from two versions.
        """
        with self._lock:
            target = self._versions.get(version)
            if target is None:
                raise VersionNotFoundError(version)
            if target.status is not expected_status:
                raise ValueError(
                    f"version {version!r} status is {target.status.value}, "
                    f"expected {expected_status.value}"
                )

            new_versions = dict(self._versions)
            new_versions[version] = StoredVersion(
                ruleset=target.ruleset,
                status=VersionStatus.PUBLISHED,
                checksum=target.checksum,
            )

            previous_ref = self._active_version
            if previous_ref is not None and previous_ref != version:
                previous = self._versions[previous_ref]
                new_versions[previous_ref] = StoredVersion(
                    ruleset=previous.ruleset,
                    status=previous_status,
                    checksum=previous.checksum,
                )

            self._versions = new_versions
            self._active_version = version
            return new_versions[version]

    def add_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._audit.append(event)

    def list_audit(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._audit)

    def get_dispatch(self, idempotency_key: str) -> DispatchRecord | None:
        with self._lock:
            return self._dispatches.get(idempotency_key)

    def put_dispatch_if_absent(
        self, record: DispatchRecord
    ) -> tuple[DispatchRecord, bool]:
        with self._lock:
            existing = self._dispatches.get(record.idempotency_key)
            if existing is not None:
                return existing, False
            self._dispatches[record.idempotency_key] = record
            return record, True
