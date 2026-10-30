"""Local, administrator-owned product-ID or catalog grants, reloaded per lookup."""

import json
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .graph import PolicyDenied


@dataclass(frozen=True)
class ProductScope:
    """Administrator-owned catalog predicates; all nonempty rules are ANDed."""

    departments: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    excluded_categories: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.departments and not self.categories:
            raise ValueError("An explicit department or category grant is required")
        for values in (self.departments, self.categories, self.excluded_categories):
            if (type(values) is not tuple or len(values) > 100
                    or any(type(value) is not str or not value.strip() or len(value) > 100 for value in values)
                    or len(values) != len(set(values))):
                raise ValueError("Invalid catalog scope")
        if any(value not in {"Men", "Women"} for value in self.departments):
            raise ValueError("Unknown department")
        if set(self.categories) & set(self.excluded_categories):
            raise ValueError("Conflicting category rules")


def scope_fingerprint(access: tuple[int, ...] | ProductScope) -> str:
    """Stable scope binding for persisted aggregates, without storing product grants."""
    if isinstance(access, ProductScope):
        payload = {"kind": "catalog", "departments": sorted(access.departments),
                   "categories": sorted(access.categories),
                   "excluded_categories": sorted(access.excluded_categories)}
    else:
        payload = {"kind": "ids", "products": sorted(access)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class JsonGrantStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def product_ids_for_user(self, user_id: str) -> tuple[int, ...]:
        access = self.access_for_user(user_id)
        if isinstance(access, ProductScope):
            raise PolicyDenied("This user has catalog rules rather than product ID grants")
        return access

    def access_for_user(self, user_id: str) -> tuple[int, ...] | ProductScope:
        try:
            if self.path.stat().st_size > 1_000_000:
                raise ValueError("Grant configuration too large")
            data = json.loads(self.path.read_text(), object_pairs_hook=self._unique_keys)
            if (not isinstance(data, dict) or set(data) != {"version", "users"}
                    or type(data["version"]) is not int or data["version"] not in {1, 2}
                    or not isinstance(data["users"], dict)):
                raise ValueError("Invalid grant configuration")
            for user, products in data["users"].items():
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,127}", user):
                    raise ValueError("Invalid user ID")
                if data["version"] == 2:
                    self._scope(products)
                    continue
                if (not isinstance(products, list) or len(products) > 10_000
                        or any(type(value) is not int or value <= 0 for value in products)
                        or len(products) != len(set(products))):
                    raise ValueError("Invalid user grants")
            products = data["users"].get(user_id)
            if not products:
                raise ValueError("No product grants")
            if data["version"] == 2:
                return self._scope(products)
            return tuple(sorted(products))
        except (OSError, ValueError, TypeError, RecursionError):
            raise PolicyDenied("Product grants are unavailable or access is not configured") from None

    @staticmethod
    def _scope(rule) -> ProductScope:
        allowed = {"departments", "categories", "excluded_categories"}
        if (not isinstance(rule, dict) or not rule or set(rule) - allowed
                or any(type(values) is not list for values in rule.values())):
            raise ValueError("Invalid catalog grant")
        return ProductScope(**{key: tuple(values) for key, values in rule.items()})

    @staticmethod
    def _unique_keys(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("Duplicate configuration key")
            data[key] = value
        return data
