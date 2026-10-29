import hashlib
from datetime import datetime, timezone
from typing import Any, Protocol


def make_doc_id(key: str) -> str:
    """Firestore document IDs cannot contain '/', so derive a stable hash from the natural key."""
    normalized = " ".join(key.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DocumentStore(Protocol):
    def list(self, collection: str, limit: int) -> list[dict[str, Any]]: ...
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool: ...
    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool: ...
    def delete(self, collection: str, doc_id: str) -> bool: ...
    def ping(self) -> None: ...


class FirestoreStore:
    """Firestore access through the Firebase Admin SDK.

    Credentials come from Application Default Credentials, i.e. the identity of
    the Cloud Run service account. No key file is read or required.
    """

    def __init__(self) -> None:
        import firebase_admin
        from firebase_admin import firestore

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        self._db = firestore.client()

    def list(self, collection: str, limit: int) -> list[dict[str, Any]]:
        docs = self._db.collection(collection).limit(limit).stream()
        return [{"id": doc.id, **doc.to_dict()} for doc in docs]

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._db.collection(collection).document(doc_id).get()
        return {"id": doc.id, **doc.to_dict()} if doc.exists else None

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        from google.api_core.exceptions import AlreadyExists

        try:
            self._db.collection(collection).document(doc_id).create(data)
        except AlreadyExists:
            return False
        return True

    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        from google.api_core.exceptions import NotFound

        try:
            self._db.collection(collection).document(doc_id).update(data)
        except NotFound:
            return False
        return True

    def delete(self, collection: str, doc_id: str) -> bool:
        ref = self._db.collection(collection).document(doc_id)
        if not ref.get().exists:
            return False
        ref.delete()
        return True

    def ping(self) -> None:
        next(iter(self._db.collection("papers").limit(1).stream()), None)


class InMemoryStore:
    """Test double with the same semantics as FirestoreStore."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    def list(self, collection: str, limit: int) -> list[dict[str, Any]]:
        docs = self._data.get(collection, {})
        return [{"id": doc_id, **doc} for doc_id, doc in list(docs.items())[:limit]]

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._data.get(collection, {}).get(doc_id)
        return {"id": doc_id, **doc} if doc is not None else None

    def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        docs = self._data.setdefault(collection, {})
        if doc_id in docs:
            return False
        docs[doc_id] = dict(data)
        return True

    def update(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        docs = self._data.get(collection, {})
        if doc_id not in docs:
            return False
        docs[doc_id].update(data)
        return True

    def delete(self, collection: str, doc_id: str) -> bool:
        return self._data.get(collection, {}).pop(doc_id, None) is not None

    def ping(self) -> None:
        return None
