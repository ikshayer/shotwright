"""Provider catalogue parsing and strict licence normalization."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from orchestrator.platform.errors import JobError, JobErrorKind

from .models import AssetSearchResult, ProviderAsset

PH = "https://api.polyhaven.com"
SF = "https://api.sketchfab.com/v3"
PH_HOME = "https://polyhaven.com"
SF_HOME = "https://sketchfab.com"


def search_polyhaven(http, q: str, asset_type: str, limit: int, page: int, categories=()) -> AssetSearchResult:
    types = {"model": "models", "texture": "textures", "hdri": "hdris"}
    data = http.get_json(f"{PH}/search?q={_q(q)}&t={types[asset_type]}")
    rows = data.get("results")
    if isinstance(rows, dict):
        rows = list(rows)
    if not isinstance(rows, list):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven search schema is invalid")
    slugs = []
    for row in rows:
        slug = row.get("slug") if isinstance(row, dict) else row if isinstance(row, str) else None
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", slug):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven search contains an invalid asset id")
        slugs.append(slug)
    expected = {"model": 2, "texture": 1, "hdri": 0}[asset_type]
    selected_slugs = []
    has_more = False
    if categories:
        # Search relevance remains provider-owned; inspect no more than the first three
        # 50-result pages and apply metadata category filtering before caller pagination.
        candidates = slugs[:150]
        has_more = len(slugs) > len(candidates)
        metadata = {}
        for slug in candidates:
            info = http.get_json(f"{PH}/info/{slug}")
            if not isinstance(info, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven asset metadata schema is invalid")
            metadata[slug] = info
            if info.get("type") == expected and _polyhaven_category_match(info, categories):
                selected_slugs.append(slug)
    else:
        start = (page - 1) * limit
        selected_slugs = slugs[start:start + limit]
        has_more = start + limit < len(slugs)
    if categories:
        start = (page - 1) * limit
        has_more = has_more or start + limit < len(selected_slugs)
        selected_slugs = selected_slugs[start:start + limit]
    results = []
    for slug in selected_slugs:
        info = http.get_json(f"{PH}/info/{slug}") if not categories else None
        if info is None:
            # Category filtering already fetched this metadata. Re-fetch is avoided by
            # the below cache populated in the filtering path.
            info = metadata[slug]
        if not isinstance(info, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven asset metadata schema is invalid")
        if info.get("type") != expected:
            continue
        authors = _ph_authors(info.get("authors"))
        name = _text(info.get("name"), slug)
        results.append(ProviderAsset(provider="polyhaven", source_asset_id=slug,
            name=name, asset_type=asset_type, source_url=f"https://polyhaven.com/a/{slug}", license="CC0-1.0",
            attribution=name + (f" - {authors}" if authors else ""), attribution_surface_required=True,
            thumbnail_url=_safe_thumbnail(info.get("thumbnail_url"), {"cdn.polyhaven.com"})))
    return AssetSearchResult(results=results, provider="polyhaven", credit="Assets from Poly Haven", credit_url=PH_HOME, page=page, has_more=has_more)


def _polyhaven_category_match(info, categories) -> bool:
    # Taxonomy node IDs match the exact stable category_id. Legacy category/tag
    # values match exactly apart from case; never derive a slug by lossy punctuation
    # normalization, which could conflate distinct provider categories.
    values = []
    def collect(value, *, include_keys=False):
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if include_keys and isinstance(key, str):
                    values.append(key)
                collect(item, include_keys=include_keys)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, include_keys=include_keys)
    for key in ("category_id", "category_slug", "category"):
        collect(info.get(key))
    collect(info.get("categories"), include_keys=True)
    normalized = {value.casefold() for value in values}
    return any(category.casefold() in normalized for category in categories)


def list_polyhaven_categories(http, asset_type: str) -> list[dict]:
    endpoint = {"model": "models", "texture": "textures", "hdri": "hdris"}[asset_type]
    # The current official API exposes stable category IDs and names in taxonomy;
    # legacy /categories returns names/counts but no safe stable identifier.
    data = http.get_json(f"{PH}/taxonomy/{endpoint}")
    if not isinstance(data, dict):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category catalogue schema is invalid")
    rows = data.get("categories")
    result = []
    seen = set()
    def visit(items, depth=0):
        if not isinstance(items, list) or depth > 16:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category tree is invalid or too deep")
        for row in items:
            if not isinstance(row, dict):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category entry is invalid")
            slug, name = row.get("id"), row.get("name")
            if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", slug) or not isinstance(name, str) or not name.strip() or slug in seen:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category entry has an invalid or duplicate stable ID")
            seen.add(slug)
            result.append({"slug": slug, "name": name.strip()[:200], "count": None})
            if len(result) > 1000:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category catalogue exceeds its item limit")
            visit(row.get("children", []), depth + 1)
    if isinstance(rows, list):
        visit(rows)
        return result

    # Compatibility for the prior documented legacy endpoint schema. Do not invent
    # slugs from display names: only already-safe keys round-trip without ambiguity.
    legacy = data.get("categories", data.get("results"))
    if isinstance(legacy, dict):
        for key, count in legacy.items():
            if key == "all":
                continue
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Legacy Poly Haven category name has no safe stable slug")
            if type(count) is not int or count < 0:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category count is invalid")
            result.append({"slug": key, "name": key, "count": count})
            if len(result) > 1000:
                raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category catalogue exceeds its item limit")
        return result
    raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Poly Haven category catalogue schema is invalid")



def list_sketchfab_categories(http) -> list[dict]:
    data = http.get_json(f"{SF}/categories")
    rows = data.get("results")
    if not isinstance(rows, list):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab category catalogue schema is invalid")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab category entry is invalid")
        slug, name = row.get("slug"), row.get("name")
        count = row.get("modelCount", row.get("count"))
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", slug) or not isinstance(name, str) or not name.strip():
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab category entry is missing a safe slug or name")
        if count is not None and (type(count) is not int or count < 0):
            count = None
        result.append({"slug": slug, "name": name.strip()[:200], "count": count})
        if len(result) > 1000:
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab category catalogue exceeds its item limit")
    return result


def _sketchfab_thumbnail(value) -> str | None:
    images = value.get("images") if isinstance(value, dict) else None
    if not isinstance(images, list):
        return None
    candidates = []
    for image in images:
        if not isinstance(image, dict): continue
        url = _safe_thumbnail(image.get("url"), {"media.sketchfab.com"})
        width = image.get("width")
        if url:
            candidates.append((width if type(width) is int else 0, url))
    preferred = [x for x in candidates if 400 <= x[0] <= 800]
    return (preferred or candidates)[0][1] if candidates else None


def _safe_thumbnail(value, hosts: set[str]) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 32 for char in value): return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len({key for key, _ in pairs}) != len(pairs):
            return None
        for key, item in pairs:
            if key in {"width", "height"}:
                if not re.fullmatch(r"[1-9][0-9]{0,3}", item) or int(item) > 2048:
                    return None
            elif key == "v":
                if not re.fullmatch(r"[A-Fa-f0-9]{1,64}", item): return None
            elif key == "format" and parsed.hostname == "cdn.polyhaven.com":
                if item not in {"jpg", "jpeg", "png", "webp"}: return None
            else:
                # Reject auth, signature, and arbitrary query parameters.
                return None
        return value
    except ValueError:
        return None

def sketchfab_licenses(http, *, authenticated: bool = False) -> dict[str, tuple[str, str, str]]:
    data = http.get_json(f"{SF}/licenses", authenticated=authenticated)
    rows = data.get("results")
    if not isinstance(rows, list):
        raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab licence catalogue schema is invalid")
    catalog = {}
    for row in rows:
        if not isinstance(row, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab licence catalogue contains an invalid entry")
        uid = _uid(row.get("uri"), catalogue=True)
        if uid:
            catalog[uid] = (str(row.get("slug", "")), str(row.get("url", "")), str(row.get("label", "")))
    return catalog


def classify_sketchfab_license(model_license: object, catalog: dict[str, tuple[str, str, str]]) -> tuple[str, str, bool] | None:
    if not isinstance(model_license, dict):
        return None
    uid = model_license.get("uid")
    uri = model_license.get("uri")
    uri_uid = _uid(uri, catalogue=True) if isinstance(uri, str) else None
    if uri is not None and uri_uid is None:
        return None
    if uid is not None and uri_uid is not None and uid != uri_uid:
        return None
    uid = uid or uri_uid
    if not isinstance(uid, str) or uid not in catalog:
        return None
    slug, canonical, _ = catalog[uid]
    if model_license.get("slug") is not None and model_license.get("slug") != slug:
        return None
    if model_license.get("url") is not None:
        supplied_url = _canonical_cc(model_license.get("url"))
        catalog_url = _canonical_cc(canonical)
        if supplied_url is None or supplied_url != catalog_url:
            return None
    path = _canonical_cc(canonical)
    if slug == "cc0" and path == "/publicdomain/zero/1.0/":
        return "CC0-1.0", f"https://creativecommons.org{path}", False
    if slug == "by" and path == "/licenses/by/4.0/":
        return "CC-BY-4.0", f"https://creativecommons.org{path}", True
    return None


def search_sketchfab(http, q: str, asset_type: str, limit: int, page: int, settings, categories=()) -> AssetSearchResult:
    if asset_type != "model":
        from .models import ProviderConfigurationError
        raise ProviderConfigurationError("sketchfab", "Sketchfab search supports models only")
    if page > settings.asset_providers.max_search_pages:
        from .models import ProviderConfigurationError
        raise ProviderConfigurationError("sketchfab", "Requested page exceeds configured search bound")
    licenses = sketchfab_licenses(http)
    query = [("type", "models"), ("downloadable", "true"), ("q", q), ("count", str(limit))]
    query.extend(("categories", category) for category in categories)
    url = f"{SF}/search?{urlencode(query)}"
    data = {}
    for page_index in range(1, page + 1):
        data = http.get_json(url)
        if not isinstance(data.get("results"), list):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab search schema is invalid")
        if page_index < page:
            next_url = data.get("next")
            if not next_url:
                data = {"results": [], "next": None}
                break
            _validate_search_next(next_url)
            url = next_url
        elif not data.get("next"):
            break
    rows = data.get("results", [])
    results = []
    for item in rows:
        if not isinstance(item, dict):
            raise JobError(JobErrorKind.CORRUPT_ARTIFACT, "Sketchfab search item is invalid")
        rights = classify_sketchfab_license(item.get("license"), licenses)
        uid = item.get("uid")
        if rights is None or not isinstance(uid, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", uid):
            continue
        name = _text(item.get("name"), uid)
        creator = item.get("user") if isinstance(item.get("user"), dict) else {}
        creator_name = _text(creator.get("displayName"), "Unknown creator")
        profile = _optional_http(creator.get("profileUrl"), "sketchfab.com")
        attribution = f"{name} by {creator_name} ({profile or 'Sketchfab creator profile'}) - {rights[0]} {rights[1]}"
        results.append(ProviderAsset(provider="sketchfab", source_asset_id=uid, name=name, asset_type="model",
            source_url=f"https://sketchfab.com/3d-models/{uid}", license=rights[0], attribution=attribution,
            attribution_surface_required=rights[2], thumbnail_url=_sketchfab_thumbnail(item.get("thumbnails"))))
    return AssetSearchResult(results=results[:limit], provider="sketchfab", credit="Models from Sketchfab", credit_url=SF_HOME, page=page, has_more=bool(data.get("next")))


def _validate_search_next(value):
    p = urlsplit(value) if isinstance(value, str) else None
    if p is None or p.scheme != "https" or p.hostname != "api.sketchfab.com" or p.port not in (None, 443) or p.path != "/v3/search" or p.username or p.password or p.fragment:
        raise JobError(JobErrorKind.INVALID_MANIFEST, "Sketchfab pagination URL is invalid")


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _text(value, default: str) -> str:
    return value.strip()[:500] if isinstance(value, str) and value.strip() else default


def _ph_authors(value) -> str:
    if not isinstance(value, dict):
        return ""
    return ", ".join(sorted(_text(name, "") for name in value if isinstance(name, str)))


def _optional_http(value, host: str) -> str | None:
    if not isinstance(value, str): return None
    p = urlsplit(value)
    return value if p.scheme == "https" and p.hostname == host and not p.username and not p.password else None


def _uid(value: object, *, catalogue: bool = False) -> str | None:
    if not isinstance(value, str): return None
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or p.hostname not in {"sketchfab.com", "www.sketchfab.com", "api.sketchfab.com"} or p.query or p.fragment:
            return None
        if p.hostname == "api.sketchfab.com" and not (catalogue and p.path.startswith("/v3/licenses/")):
            return None
        uid = p.path.rstrip("/").split("/")[-1]
        return uid if re.fullmatch(r"[0-9a-fA-F]{32}", uid) else None
    except ValueError:
        return None


def _canonical_cc(value: object) -> str | None:
    if not isinstance(value, str): return None
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or (p.hostname or "").lower() not in {"creativecommons.org", "www.creativecommons.org"} or p.query or p.fragment or p.username or p.password:
            return None
        path = p.path
        if not path.endswith("/"): path += "/"
        return path
    except ValueError:
        return None
