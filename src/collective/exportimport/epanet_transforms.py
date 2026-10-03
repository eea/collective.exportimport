# -*- coding: utf-8 -*-
"""Per-item transforms that turn a classic Plone export into Volto content.

These functions hold the migration logic used by ``@@export_epanet`` (see
:mod:`collective.exportimport.export_epanet`). They work on the serialized
item dicts only and never touch Plone, so they can be tested without a site.
Anything that needs the source site (the HTML converter, the default page of a
Folder, looking up an object by UID) is reached through :class:`Context`.

The output of :func:`transform_item` is ready for ``@@import_content`` on the
target site, imported with "Update existing content"
(``handle_existing_content=2``).
"""
from __future__ import annotations

from typing import Any
from typing import Callable
from typing import Iterator
from typing import Optional
from urllib.parse import urljoin
from urllib.parse import urlparse

import copy
import re
import uuid


RESOLVEUID_REF_RE = re.compile(r"resolveuid/([a-f0-9]{32})")

SUBSITE_TYPE = "Subsite"

# Type of the folder that holds the target subsite (the language root).
SUBSITE_PARENT_TYPE = "LRF"

# Map classic types to the target Dexterity types.
TYPE_MAP = {
    "Folder": "Document",
    "Collection": "Document",
    "Document": "Document",
    "News Item": "News Item",
    "File": "File",
    "Image": "Image",
    "Link": "Link",
}

# Types that receive Volto blocks.
BLOCKS_TYPES = {"Document", "News Item"}

# Serializer noise or source-only fields that are never imported.
DROP_FIELDS = {
    "is_folderish",
    "layout",
    "lock",
    "nextPreviousEnabled",
    "type_title",
    "version",
    "versioning_enabled",
    "working_copy",
    "working_copy_of",
}

# Default header blocks of a DX News Item (the EEA press release layout),
# placed between the title and the body.
NEWS_ITEM_HEADER_BLOCKS = [
    {
        "@layout": "73523456-bde7-4d0d-bdfb-84289049387a",
        "@type": "layoutSettings",
        "block": "3a979c6f-e45c-4168-9989-52ad08d480e4",
        "fixed": True,
        "layout_size": "narrow_view",
        "required": True,
    },
    {
        "@layout": "1dc7ff13-445f-49a5-8d97-4831d50777d9",
        "@type": "description",
        "block": "1155de0c-f267-429d-bc42-46e0c1150577",
        "fixed": True,
        "placeholder": "Add news item description",
        "required": True,
    },
    {
        "@layout": "c3e9b58c-2229-4383-aaf6-06c288d16e45",
        "@type": "dividerBlock",
        "block": "047c43eb-80a1-4073-973d-6b534fcf4f7a",
        "hidden": True,
        "section": True,
        "styles": {},
    },
]

TEASER_COLUMNS = 4


class Context(object):
    """What the transforms need from the source site and the target.

    :param old_root: URL of the source site root, as found in the export.
    :param target_root: URL of the target subsite, e.g.
        ``https://www.eea.europa.eu/en/epanet``.
    :param convert: ``convert(html) -> (blocks, layout)``; the blocks converter.
    :param old_site_path: physical path of the source site (``/epanet``); used
        to rewrite path criteria of Collections.
    :param default_page: ``default_page(item) -> serialized item or None``: the
        default page of a Folder, serialized like an exported item (``@type``,
        ``text``, and for a Collection ``query``, ``sort_on``,
        ``sort_reversed`` and ``limit``).
    :param resolve_uid: ``resolve_uid(uid) -> target URL or None``.
    :param site_default_page: id of the source site root's default page; its
        blocks are put on the subsite.
    :param teaser_page: path below the subsite whose logo table becomes a
        teaser grid (``/our-group``), or None.
    :param subsite_parent_uid: UID of the subsite's parent on the target. Only
        needed when importing at the subsite itself; see :func:`subsite_item`.
    """

    def __init__(
        self,
        old_root: str,
        target_root: str,
        convert: Callable[[str], tuple],
        old_site_path: str = "",
        default_page: Optional[Callable[[dict], Optional[dict]]] = None,
        resolve_uid: Optional[Callable[[str], Optional[str]]] = None,
        site_default_page: Optional[str] = None,
        teaser_page: Optional[str] = None,
        subsite_parent_uid: Optional[str] = None,
    ):
        self.old_root = old_root.rstrip("/")
        self.target_root = target_root.rstrip("/")
        self.convert = convert
        self.old_site_path = (old_site_path or urlparse(self.old_root).path).rstrip("/")
        self.default_page = default_page or (lambda item: None)
        self.resolve_uid = resolve_uid or (lambda uid: None)
        self.site_default_page = site_default_page
        self.teaser_page = teaser_page
        self.subsite_parent_uid = subsite_parent_uid


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def transform_item(item: dict, ctx: Context) -> dict:
    """Return the importable version of one serialized item.

    Raises ``ValueError`` for a type that has no target type.
    """
    old_type = item.get("@type")
    new_type = TYPE_MAP.get(old_type)
    if new_type is None:
        raise ValueError("Unknown type {}".format(old_type))

    html = html_of(item)
    top_level = is_top_level(item, ctx.old_root)

    new_item = filter_fields(item)
    keep_effective_date(new_item)
    new_item["@type"] = new_type
    new_item["language"] = normalize_language(item.get("language"))
    rewrite_urls(new_item, ctx, top_level)

    if new_type in BLOCKS_TYPES:
        blocks, layout = build_blocks(item, html, ctx)
        if blocks:
            new_item["blocks"] = blocks
            new_item["blocks_layout"] = {"items": layout}
    new_item.pop("text", None)

    default_html = None
    if old_type == "Folder":
        page = ctx.default_page(item)
        if page:
            default_html = html_of(page)
            merge_default_page(new_item, page, ctx)

    if new_item.get("blocks"):
        extract_inline_images(new_item)
        normalize_blocks(new_item)
        alignments = image_alignments(html)
        for uid, align in image_alignments(default_html or "").items():
            alignments.setdefault(uid, align)
        align_images(new_item, alignments)
        if ctx.teaser_page and path_below(new_item["@id"], ctx.target_root) == ctx.teaser_page:
            build_teaser_grid(new_item, ctx.resolve_uid)
        clean_slate(new_item)

    if top_level and ctx.site_default_page and item.get("id") == ctx.site_default_page:
        return subsite_item(new_item, ctx)
    return new_item


# --------------------------------------------------------------------------- #
# fields and URLs
# --------------------------------------------------------------------------- #


def html_of(item: dict) -> str:
    """The rich text HTML of a serialized item."""
    text = item.get("text")
    if isinstance(text, dict):
        return text.get("data") or ""
    if isinstance(text, str):
        return text
    return ""


def filter_fields(item: dict) -> dict:
    """Drop serializer noise; keep known and unknown fields."""
    return {key: value for key, value in item.items() if key not in DROP_FIELDS}


def keep_effective_date(item: dict) -> None:
    """Drop ``review_state`` where publishing would invent an effective date.

    The importer runs a transition to ``review_state``; that also updates the
    catalog's ``review_state`` index (the workflow history it imports
    afterwards does not), so the navigation lists the item. Publishing keeps
    an existing ``effective`` but sets an empty one to now, so a published item
    without one goes without ``review_state``; its state still arrives through
    ``workflow_history``.
    """
    if item.get("review_state") == "published" and not item.get("effective"):
        item.pop("review_state")


def normalize_language(lang: Optional[str]) -> str:
    if not lang or lang.lower() in ("en-gb", "en_gb"):
        return "en"
    return lang


def relative_path(url: str, root: str) -> str:
    """Path of ``url`` below ``root``, with a leading slash."""
    root_path = urlparse(root.rstrip("/") + "/").path.rstrip("/")
    url_path = urlparse(url).path
    if url_path.startswith(root_path + "/"):
        return url_path[len(root_path):]
    if url_path == root_path:
        return "/"
    return url_path


def target_url(old_url: str, ctx: Context) -> str:
    rel = relative_path(old_url, ctx.old_root)
    return urljoin(ctx.target_root + "/", rel.lstrip("/"))


def path_below(url: str, root: str) -> str:
    """Path of ``url`` below ``root`` without a trailing slash (``/a/b``)."""
    return relative_path(url, root).rstrip("/")


def is_top_level(item: dict, old_root: str) -> bool:
    parent = (item.get("parent") or {}).get("@id", "")
    return parent.rstrip("/") == old_root.rstrip("/")


def rewrite_urls(item: dict, ctx: Context, top_level: bool) -> None:
    """Move ``@id`` and ``parent`` below the target subsite."""
    item["@id"] = target_url(item["@id"], ctx)
    parent = item.get("parent")
    if parent:
        parent = dict(parent)
        parent["@id"] = target_url(parent["@id"], ctx)
        if top_level:
            # The subsite exists on the target; it is found by path.
            parent["@type"] = SUBSITE_TYPE
            parent.pop("UID", None)
        item["parent"] = parent


# --------------------------------------------------------------------------- #
# blocks
# --------------------------------------------------------------------------- #


def new_uid() -> str:
    return str(uuid.uuid4())


def build_blocks(item: dict, html: str, ctx: Context) -> tuple:
    """Title, header blocks, the converted body and, for a Collection, a listing."""
    blocks: dict = {}
    layout: list = []
    is_news = item.get("@type") == "News Item"

    title = {"@type": "title"}
    if is_news:
        title["hideContentType"] = True
    _append(blocks, layout, title)

    if is_news:
        for block in NEWS_ITEM_HEADER_BLOCKS:
            _append(blocks, layout, copy.deepcopy(block))
    elif (item.get("description") or "").strip():
        _append(blocks, layout, {"@type": "description"})

    if html.strip():
        body_blocks, body_layout = ctx.convert(html)
        blocks.update(body_blocks)
        layout.extend(body_layout)

    if item.get("@type") == "Collection":
        uid = new_uid()
        listing = collection_listing_block(item, ctx)
        listing["block"] = uid
        blocks[uid] = listing
        layout.append(uid)

    return blocks, layout


def _append(blocks: dict, layout: list, block: dict) -> None:
    uid = new_uid()
    blocks[uid] = block
    layout.append(uid)


def collection_listing_block(item: dict, ctx: Context) -> dict:
    """A listing block running the Collection's own saved query.

    A path criterion is moved below the target subsite. It stays a path:
    ``absolutePath`` prefixes the portal path to anything else, so a URL would
    match nothing.
    """
    query = copy.deepcopy(item.get("query") or [])
    target_path = urlparse(ctx.target_root).path.rstrip("/")
    for criterion in query:
        value = criterion.get("v")
        if criterion.get("i") != "path" or not isinstance(value, str):
            continue
        path, sep, depth = value.partition("::")
        if path == ctx.old_site_path or path.startswith(ctx.old_site_path + "/"):
            path = path[len(ctx.old_site_path):]
        criterion["v"] = target_path + "/" + path.lstrip("/") + sep + depth
    return {
        "@type": "listing",
        "querystring": {
            "query": query,
            "sort_on": item.get("sort_on") or "effective",
            "sort_order": "descending" if item.get("sort_reversed") else "ascending",
            "limit": str(item.get("limit") or 1000),
            "b_size": "20",
        },
        "variation": "default",
        "itemModel": {"@type": "card", "hasLink": True},
    }


def merge_default_page(item: dict, page: dict, ctx: Context) -> None:
    """Append a default page's body (without its title) to a Folder's blocks.

    A Collection as default page also contributes its listing, so the Folder
    shows the same items as the old site did.
    """
    blocks = item.setdefault("blocks", {})
    layout = item.setdefault("blocks_layout", {"items": []})["items"]
    html = html_of(page)
    if html.strip():
        body_blocks, body_layout = ctx.convert(html)
        for uid in body_layout:
            if body_blocks[uid].get("@type") == "title":
                continue
            blocks[uid] = body_blocks[uid]
            layout.append(uid)
    if page.get("@type") == "Collection":
        uid = new_uid()
        listing = collection_listing_block(page, ctx)
        listing["block"] = uid
        blocks[uid] = listing
        layout.append(uid)


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #


def extract_inline_images(item: dict) -> None:
    """Move inline Slate images into image blocks placed before their paragraph."""
    blocks = item["blocks"]
    new_layout = []
    for block_id in item["blocks_layout"]["items"]:
        block = blocks.get(block_id)
        if block and block.get("@type") == "slate":
            extracted: list = []
            value = _remove_inline_images(block.get("value", []), extracted)
            for img in extracted:
                image = {
                    "@type": "image",
                    "url": img.get("url"),
                    "alt": img.get("alt", ""),
                    "title": img.get("title", ""),
                    "align": img.get("align", ""),
                }
                if img.get("image_scales"):
                    image["image_scales"] = img["image_scales"]
                _append(blocks, new_layout, image)
            if extracted:
                block["value"] = [node for node in value if not _is_empty_slate_node(node)]
                block.pop("plaintext", None)
        new_layout.append(block_id)
    item["blocks_layout"]["items"] = new_layout


def _remove_inline_images(value: Any, extracted: list) -> Any:
    if isinstance(value, list):
        result = []
        for child in value:
            cleaned = _remove_inline_images(child, extracted)
            if cleaned is not None:
                result.append(cleaned)
        return result
    if isinstance(value, dict):
        if value.get("type") == "img":
            extracted.append(value)
            return None
        cleaned = dict(value)
        if "children" in cleaned:
            cleaned["children"] = _remove_inline_images(cleaned["children"], extracted)
        return cleaned
    return value


def _is_empty_slate_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    text = node.get("text")
    if text is not None:
        return not str(text).strip()
    children = node.get("children", [])
    return not children or all(_is_empty_slate_node(child) for child in children)


def normalize_resolveuid_url(url: str, scale: Optional[str] = None) -> str:
    """Keep internal references as ``/resolveuid/<uid>`` (host-agnostic)."""
    if not url or "resolveuid/" not in url:
        return url
    match = RESOLVEUID_REF_RE.search(url)
    if not match:
        return url
    if scale:
        return "/resolveuid/{}/@@images/image/{}".format(match.group(1), scale)
    return "/resolveuid/{}{}".format(match.group(1), url[match.end():])


def _normalize_urls(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("@type") == "image" and isinstance(value.get("url"), str):
            new_value = dict(value)
            new_value["url"] = normalize_resolveuid_url(value["url"], value.get("scale"))
            return new_value
        if value.get("type") == "img" and isinstance(value.get("url"), str):
            new_value = dict(value)
            new_value["url"] = normalize_resolveuid_url(value["url"], value.get("scale"))
            new_value["children"] = _normalize_urls(value.get("children", []))
            return new_value
        return {key: _normalize_urls(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_normalize_urls(child) for child in value]
    if isinstance(value, str):
        return normalize_resolveuid_url(value)
    return value


def normalize_blocks(item: dict) -> None:
    item["blocks"] = {uid: _normalize_urls(block) for uid, block in item["blocks"].items()}


def _float_of(tag: str) -> str:
    """Inline style wins over the class, as in the browser."""
    style = re.search(r'style="([^"]*)"', tag)
    style = (style.group(1) if style else "").lower()
    if "float: left" in style:
        return "left"
    if "float: right" in style:
        return "right"
    cls = re.search(r'class="([^"]*)"', tag)
    cls = (cls.group(1) if cls else "").lower()
    if "image-left" in cls:
        return "left"
    if "image-right" in cls:
        return "right"
    return ""


def image_alignments(html: str) -> dict:
    """Map image UID -> float ("left"/"right") for the <img> tags in ``html``.

    Classic Plone floats images with an ``image-left``/``image-right`` class or
    an inline ``float`` style; the converter only reads the style.
    """
    result: dict = {}
    for tag in re.findall(r"<img[^>]*>", html or "", re.IGNORECASE):
        uid = re.search(r'data-val="([a-f0-9]{32})"', tag) or RESOLVEUID_REF_RE.search(tag)
        align = _float_of(tag)
        if uid and align:
            result.setdefault(uid.group(1), align)
    return result


def align_images(item: dict, alignments: dict) -> int:
    """Set ``align`` on top-level image blocks; return how many changed."""
    changed = 0
    blocks = item["blocks"]
    for uid in item["blocks_layout"]["items"]:
        block = blocks.get(uid) or {}
        if block.get("@type") != "image":
            continue
        match = RESOLVEUID_REF_RE.search(block.get("url") or "")
        align = alignments.get(match.group(1)) if match else None
        if align and block.get("align") != align:
            blocks[uid] = dict(block, align=align)
            changed += 1
    return changed


# --------------------------------------------------------------------------- #
# member logo table -> teaser grid
# --------------------------------------------------------------------------- #


def build_teaser_grid(item: dict, resolve_uid: Callable) -> int:
    """Replace the first slateTable with a group of teaserGrids.

    One teaser per cell holding an image: title = the cell's text, image = the
    Image item, external link = the cell's link. Returns the number of teasers.
    """
    blocks = item["blocks"]
    table_id = next(
        (uid for uid in item["blocks_layout"]["items"]
         if blocks.get(uid, {}).get("@type") == "slateTable"),
        None,
    )
    if table_id is None:
        return 0

    teasers = []
    for row in (blocks[table_id].get("table") or {}).get("rows", []):
        for cell in row.get("cells", []):
            value = cell.get("value", [])
            found = _find_image(value)
            if not found:
                continue
            img, link = found
            match = RESOLVEUID_REF_RE.search(img.get("url") or "")
            if not match:
                continue
            teasers.append({
                "@type": "teaser",
                "id": new_uid(),
                "href": [{"@id": "/resolveuid/" + match.group(1), "image_field": "image"}],
                "title": _first_text(value) or img.get("title") or "",
                "external_link": _resolve_link(link or "", resolve_uid),
                "itemModel": {
                    "@type": "imageOnBottom",
                    "hasLink": True,
                    # logos keep their aspect ratio instead of being cropped
                    "styles": {"objectFit": "contain"},
                },
            })

    group_blocks: dict = {}
    group_layout: list = []
    for start in range(0, len(teasers), TEASER_COLUMNS):
        grid = {"@type": "teaserGrid", "columns": teasers[start:start + TEASER_COLUMNS]}
        _append(group_blocks, group_layout, grid)
    blocks[table_id] = {
        "@type": "group",
        "data": {"blocks": group_blocks, "blocks_layout": {"items": group_layout}},
    }
    return len(teasers)


def _first_text(node: Any) -> Optional[str]:
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        node = node.get("children", [])
    if isinstance(node, list):
        for child in node:
            found = _first_text(child)
            if found:
                return found
    return None


def _find_image(node: Any, link: Optional[str] = None) -> Optional[tuple]:
    """(img node, enclosing link URL) of the first image in the tree."""
    if isinstance(node, dict):
        if node.get("type") == "link":
            link = (node.get("data") or {}).get("url") or link
        if node.get("type") == "img":
            return node, link
        node = node.get("children", [])
    if isinstance(node, list):
        for child in node:
            found = _find_image(child, link)
            if found:
                return found
    return None


def _resolve_link(url: str, resolve_uid: Callable) -> str:
    """A ``resolveuid`` link becomes the target's site path (``/en/...``)."""
    match = RESOLVEUID_REF_RE.search(url)
    if not match:
        return url
    target = resolve_uid(match.group(1))
    if not target:
        return url
    path = urlparse(target).path or target
    index = path.find("/en/")
    return path[index:] if index != -1 else target


# --------------------------------------------------------------------------- #
# Slate validity
# --------------------------------------------------------------------------- #


def has_text(node: Any) -> bool:
    return isinstance(node, dict) and (
        isinstance(node.get("text"), str)
        or any(has_text(child) for child in node.get("children", []))
    )


def slate_values(blocks: Any) -> Iterator[list]:
    """The ``value`` of every Slate block, including nested blocks."""
    if isinstance(blocks, dict):
        if blocks.get("@type") == "slate" and isinstance(blocks.get("value"), list):
            yield blocks["value"]
        for child in blocks.values():
            yield from slate_values(child)
    elif isinstance(blocks, list):
        for child in blocks:
            yield from slate_values(child)


def clean_slate(item: dict) -> int:
    """Remove Slate elements without any text leaf; return how many.

    The Slate editor cannot place a cursor in such an element and the edit
    form crashes (e.g. ``<strong><img/></strong>`` once the image is moved
    out).
    """
    removed = 0

    def clean(children: list) -> list:
        nonlocal removed
        kept = []
        for child in children:
            if isinstance(child, dict) and "text" not in child:
                if not has_text(child):
                    removed += 1
                    continue
                child["children"] = clean(child.get("children", []))
            kept.append(child)
        return kept or [{"text": ""}]

    for value in slate_values(item.get("blocks") or {}):
        for top in value:
            if isinstance(top, dict) and "children" in top:
                top["children"] = clean(top["children"])
    return removed


def count_textless_slate(item: dict) -> int:
    """Slate elements without a text leaf (should be 0 after clean_slate)."""

    def count(nodes: list) -> int:
        bad = 0
        for node in nodes:
            if isinstance(node, dict) and "text" not in node:
                if not node.get("children") or not has_text(node):
                    bad += 1
                else:
                    bad += count(node["children"])
        return bad

    return sum(count(value) for value in slate_values(item.get("blocks") or {}))


# --------------------------------------------------------------------------- #
# subsite
# --------------------------------------------------------------------------- #


def subsite_item(front_page: dict, ctx: Context) -> dict:
    """The existing target subsite, carrying the site default page's blocks.

    The subsite is created by hand on the target; this item only updates its
    blocks (import with ``handle_existing_content=2``). It has no ``UID`` so
    the subsite keeps its own, and no ``review_state`` so no transition runs.

    Its parent is the language folder. Importing at the site root finds it by
    path. Importing at the subsite needs ``subsite_parent_uid``: the subsite is
    a navigation root and the importer ignores parents found by path outside
    it.
    """
    parent = {"@id": ctx.target_root.rsplit("/", 1)[0], "@type": SUBSITE_PARENT_TYPE}
    if ctx.subsite_parent_uid:
        parent["UID"] = ctx.subsite_parent_uid
    return {
        "@id": ctx.target_root,
        "id": ctx.target_root.rsplit("/", 1)[-1],
        "@type": SUBSITE_TYPE,
        "parent": parent,
        "blocks": front_page.get("blocks") or {},
        "blocks_layout": front_page.get("blocks_layout") or {"items": []},
    }

