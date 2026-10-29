from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, create_model

from store import DocumentStore, FirestoreStore, make_doc_id, utc_now

MAX_LIST_LIMIT = 500


class Paper(BaseModel):
    title: str
    author: str = ""
    first_author: str = ""
    journal: str = ""
    pub_date: str = ""
    url: str = ""
    doi: str = ""
    abstract: str = ""
    ai_insight: str = ""
    user_memo: str = ""
    matched_keyword: str = ""
    is_read: bool = False


class Researcher(BaseModel):
    name: str
    field: str = ""
    school_of_thought: str = ""
    key_concepts: str = ""
    academic_societies: str = ""
    memo: str = ""


class Society(BaseModel):
    name: str
    related_authors: str = ""
    category: str = "未分類"
    official_url: str = ""
    user_memo: str = ""
    is_target: bool = False


class Keyword(BaseModel):
    keyword: str
    category: str = ""


def _patch_model(model: type[BaseModel], key_field: str) -> type[BaseModel]:
    fields: dict[str, Any] = {
        name: (Optional[field.annotation], None)
        for name, field in model.model_fields.items()
        if name != key_field
    }
    return create_model(f"{model.__name__}Patch", **fields)


def _build_router(
    store: DocumentStore,
    collection: str,
    model: type[BaseModel],
    key_field: str,
) -> APIRouter:
    router = APIRouter(prefix=f"/{collection}", tags=[collection])
    patch_model = _patch_model(model, key_field)

    @router.get("")
    def list_items(limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT)):
        return store.list(collection, limit)

    @router.post("", status_code=201)
    def create_item(item: model):  # type: ignore[valid-type]
        key = getattr(item, key_field).strip()
        if not key:
            raise HTTPException(422, f"{key_field} must not be empty")
        doc_id = make_doc_id(key)
        now = utc_now()
        data = {**item.model_dump(), key_field: key, "created_at": now, "updated_at": now}
        if not store.create(collection, doc_id, data):
            raise HTTPException(409, f"{key_field} already exists")
        return {"id": doc_id, **data}

    @router.get("/{doc_id}")
    def get_item(doc_id: str):
        item = store.get(collection, doc_id)
        if item is None:
            raise HTTPException(404, "not found")
        return item

    @router.patch("/{doc_id}")
    def update_item(doc_id: str, patch: patch_model):  # type: ignore[valid-type]
        changes = patch.model_dump(exclude_unset=True)
        changes["updated_at"] = utc_now()
        if not store.update(collection, doc_id, changes):
            raise HTTPException(404, "not found")
        return store.get(collection, doc_id)

    @router.delete("/{doc_id}", status_code=204)
    def delete_item(doc_id: str):
        if not store.delete(collection, doc_id):
            raise HTTPException(404, "not found")

    return router


def create_app(store: DocumentStore | None = None) -> FastAPI:
    app = FastAPI(title="Academic Tracker API")
    resolved_store = store or FirestoreStore()

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            resolved_store.ping()
        except Exception as exc:
            raise HTTPException(503, f"Firestore unavailable: {type(exc).__name__}")
        return {"status": "ready"}

    for collection, model, key_field in (
        ("papers", Paper, "title"),
        ("researchers", Researcher, "name"),
        ("societies", Society, "name"),
        ("keywords", Keyword, "keyword"),
    ):
        app.include_router(_build_router(resolved_store, collection, model, key_field))

    return app


def _app_factory() -> FastAPI:
    return create_app()
