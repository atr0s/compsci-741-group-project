"""Store registry: the map from a chosen store to its RLS identity.

This lives in the application tier on purpose. The store must be decided by
something the user cannot forge and the model cannot choose - otherwise the
"authorisation" is just the agent asking itself which rows it would like.

Today the picker in the UI supplies it, which makes the store dimension real
but does NOT yet make it authorisation: a caller who can reach /chat/stream can
name any store. Binding store to the authenticated principal - Entra claims
mapped to a store, checked here - is the change that closes that gap, and it is
the one place that needs to change to close it.

UUIDs match retail.stores.rls_user_id, seeded from data/database/reference_data.json.
"""

from typing import Optional

SUPER_MANAGER_UUID = "00000000-0000-0000-0000-000000000000"

STORES: list[dict[str, str]] = [
    {"key": "seattle",  "label": "Seattle",  "rls_user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"},
    {"key": "bellevue", "label": "Bellevue", "rls_user_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"},
    {"key": "tacoma",   "label": "Tacoma",   "rls_user_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
    {"key": "spokane",  "label": "Spokane",  "rls_user_id": "d8e9f0a1-b2c3-4567-8901-234567890abc"},
    {"key": "everett",  "label": "Everett",  "rls_user_id": "3b9ac9fa-cd5e-4b92-a7f2-b8c1d0e9f2a3"},
    {"key": "redmond",  "label": "Redmond",  "rls_user_id": "e7f8a9b0-c1d2-3e4f-5678-90abcdef1234"},
    {"key": "kirkland", "label": "Kirkland", "rls_user_id": "9c8b7a65-4321-fed0-9876-543210fedcba"},
    {"key": "online",   "label": "Online",   "rls_user_id": "2f4e6d8c-1a3b-5c7e-9f0a-b2d4f6e8c0a2"},
    # Sees every store. Kept last and labelled, because a regional view is a
    # privilege, not a default.
    {"key": "all", "label": "All stores (regional)", "rls_user_id": SUPER_MANAGER_UUID},
]

_BY_KEY = {store["key"]: store for store in STORES}

# A single store, not the regional view: an unrecognised key must never widen
# what the caller can see. Online is the safe pick as well as the sensible one -
# it is the store that serves a customer who has not said where they are.
DEFAULT_STORE_KEY = "online"


def resolve(store_key: Optional[str]) -> dict[str, str]:
    """Look up a store, falling back to the default rather than to all-stores."""
    return _BY_KEY.get((store_key or "").strip().lower(), _BY_KEY[DEFAULT_STORE_KEY])


def rls_user_id_for(store_key: Optional[str]) -> str:
    return resolve(store_key)["rls_user_id"]


def label_for(store_key: Optional[str]) -> str:
    return resolve(store_key)["label"]


def is_known(store_key: str) -> bool:
    return store_key in _BY_KEY
