# -*- coding: utf-8 -*-
from __future__ import annotations

from .export_content import ExportContent
from App.config import getConfiguration
from collective.exportimport import config
from plone import api
from plone.app.contenttypes.interfaces import ICollection
from plone.restapi.interfaces import ISerializeToJson
from plone.uuid.interfaces import IUUID
from Products.CMFPlone.interfaces import IPloneSiteRoot
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from zope.component import getMultiAdapter

import json
import logging
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlparse
import urllib.request


logger = logging.getLogger(__name__)

RESOLVEUID_REF_RE = re.compile(r"resolveuid/([a-f0-9]{32})")


class ExportEpanet(ExportContent):
    """Export and transform EPANET classic content for import into a Volto subsite.

    The view is a thin wrapper around :class:`ExportContent`.  It reuses the
    existing serialization pipeline and applies EPANET-specific transformations
    in the ``global_dict_hook``:

    * type mapping (Folder -> Document)
    * field filtering and language normalization
    * URL rewriting so items land under the target subsite
    * HTML-to-Volto-blocks conversion via eea-volto-blocks-converter
    * default-page body merge into the parent Folder-turned-Document
    * resolveuid link normalization inside blocks
    * collection extraction for manual rebuild as listing blocks

    Usage::

        @@export_epanet?download_to_server=1
            &target_root=https://demo-www.eea.europa.eu/en/epanet
            &converter_url=http://localhost:8000/toblocks

    Report artifacts are written next to the downloaded JSON::

        <clienthome>/epanet-export-reports/
            manifest.json
            collections.json
            unconvertible.json
            MIGRATION_REPORT.md
    """

    template = ViewPageTemplateFile("templates/export_content.pt")

    # Source site root.  When None it defaults to the current portal URL.
    OLD_ROOT = None

    # Target subsite where the content will be imported.
    TARGET_ROOT = "https://demo-www.eea.europa.eu/en/epanet"

    # eea-volto-blocks-converter endpoint.
    CONVERTER_URL = "http://localhost:8000/toblocks"

    # Parent type used for top-level items in the transformed export.
    SUBSITE_TYPE = "Subsite"

    # Set to True to also export items whose workflow state is ``private``.
    INCLUDE_PRIVATE = False

    # Types that receive Volto blocks.
    BLOCKS_TYPES = {"Document", "News Item"}

    # Private items of these types are still exported because other content
    # links to them (e.g. Link items only carry an external remoteUrl).
    PRIVATE_TYPE_EXCEPTIONS = {"Link"}

    # Map classic types to the target Dexterity types.
    TYPE_MAP = {
        "Folder": "Document",
        "Document": "Document",
        "News Item": "News Item",
        "File": "File",
        "Image": "Image",
        "Link": "Link",
    }

    # Fields that are safe / useful to keep.  Everything else is dropped unless
    # it is unknown (kept with a debug log so it can be reviewed).
    KEEP_FIELDS = {
        "@id",
        "@type",
        "UID",
        "allow_discussion",
        "changeNote",
        "contributors",
        "created",
        "creators",
        "description",
        "effective",
        "exclude_from_nav",
        "expires",
        "id",
        "image",
        "image_caption",
        "language",
        "modified",
        "parent",
        "remoteUrl",
        "review_state",
        "rights",
        "subjects",
        "table_of_contents",
        "text",
        "title",
        "workflow_history",
        "file",
    }

    # Serializer-only noise or source-only fields that should never be imported.
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

    def update(self):
        """Read runtime configuration from request or class attributes."""
        self.target_root = (
            self.request.form.get("target_root", self.TARGET_ROOT) or self.TARGET_ROOT
        )
        self.target_root = self.target_root.rstrip("/")

        self.converter_url = (
            self.request.form.get("converter_url", self.CONVERTER_URL)
            or self.CONVERTER_URL
        )

        portal = api.portal.get()
        self.old_root = self.request.form.get("old_root", self.OLD_ROOT)
        if not self.old_root:
            self.old_root = portal.absolute_url()
        self.old_root = self.old_root.rstrip("/")

        include_private = self.request.form.get("include_private", self.INCLUDE_PRIVATE)
        if isinstance(include_private, str):
            self.include_private = include_private.lower() in ("true", "1", "on", "yes")
        else:
            self.include_private = bool(include_private)

        # Runtime state used for reports.
        self.collections = []
        self.unconvertible = []
        self.manifest = []
        self.transform_errors = []

    def start(self):
        """Create the directory that will hold report artifacts."""
        directory = config.CENTRAL_DIRECTORY
        if not directory:
            cfg = getConfiguration()
            directory = cfg.clienthome
        self.report_dir = os.path.join(directory, "epanet-export-reports")
        if not os.path.exists(self.report_dir):
            os.makedirs(self.report_dir)
            logger.info("Created EPANET export report directory %s", self.report_dir)

    def finish(self):
        """Write migration report artifacts."""
        self.write_reports()

    def global_obj_hook(self, obj):
        """Filter objects before serialization.

        Return None to skip an object.
        """
        if obj.portal_type not in self.TYPE_MAP:
            logger.warning(
                "Skipping unknown type %s at %s", obj.portal_type, obj.absolute_url()
            )
            return None

        if ICollection.providedBy(obj):
            logger.info("Extracting collection %s", obj.absolute_url())
            self.collections.append(self._serialize_collection(obj))
            return None

        if not self.include_private and obj.portal_type not in self.PRIVATE_TYPE_EXCEPTIONS:
            review_state = api.content.get_state(obj, default=None)
            if review_state == "private":
                logger.info("Skipping private item %s", obj.absolute_url())
                return None

        return obj

    def global_dict_hook(self, item, obj):
        """Apply EPANET-specific transformations to the serialized item."""
        try:
            item = self._ensure_review_state(item, obj)
            item = self._filter_fields(item, obj)
            item = self._map_type(item, obj)
            item = self._normalize_language(item, obj)
            item = self._build_blocks(item, obj)
            item = self._rewrite_urls(item, obj)
            item = self._merge_default_page(item, obj)
            item = self._normalize_blocks(item, obj)
            item = self._record_manifest(item, obj)
        except Exception as exc:
            path = item.get("@id", obj.absolute_url())
            msg = "Failed to transform {}: {}".format(path, exc)
            logger.exception(msg)
            self.transform_errors.append(msg)
            return None
        return item

    # ----------------------------------------------------------------------
    # Helpers: object-level inspection
    # ----------------------------------------------------------------------

    def _ensure_review_state(self, item, obj):
        """Keep the workflow state so the importer can auto-publish."""
        if "review_state" not in item or item.get("review_state") is None:
            item["review_state"] = api.content.get_state(obj, default=None)
        return item

    def _serialize_collection(self, obj):
        """Return the data needed to rebuild a Collection as a listing block."""
        serializer = getMultiAdapter((obj, self.request), ISerializeToJson)
        data = serializer(include_items=False)
        return {
            "@id": data.get("@id"),
            "title": data.get("title"),
            "query": data.get("query", []),
            "sort_on": data.get("sort_on"),
            "sort_reversed": data.get("sort_reversed"),
            "limit": data.get("limit"),
        }

    def _extract_html(self, item_or_obj):
        """Return the HTML payload from a serialized item or live object."""
        if isinstance(item_or_obj, dict):
            text_field = item_or_obj.get("text")
        else:
            text_field = getattr(item_or_obj, "text", None)
            # DX RichTextValue
            if hasattr(text_field, "output"):
                return text_field.output
        if isinstance(text_field, dict):
            return text_field.get("data", "")
        if isinstance(text_field, str):
            return text_field
        return ""

    # ----------------------------------------------------------------------
    # Helpers: generic dict transformations
    # ----------------------------------------------------------------------

    def _filter_fields(self, item, obj):
        """Drop serializer noise and keep only importable fields."""
        new_item = {}
        for key, value in item.items():
            if key in self.KEEP_FIELDS:
                new_item[key] = value
            elif key in self.DROP_FIELDS:
                continue
            else:
                logger.debug(
                    "Keeping unknown field %s for %s", key, item.get("@id")
                )
                new_item[key] = value
        return new_item

    def _map_type(self, item, obj):
        """Map classic types to their target Dexterity types."""
        old_type = item.get("@type")
        new_type = self.TYPE_MAP.get(old_type)
        if new_type is None:
            raise ValueError("Unknown type {}".format(old_type))
        item["@type"] = new_type
        return item

    def _normalize_language(self, item, obj):
        """Normalize EPANET language values to the target language."""
        lang = item.get("language")
        if not lang or lang.lower() in ("", "en-gb", "en_gb"):
            item["language"] = "en"
        return item

    def _rewrite_urls(self, item, obj):
        """Rewrite @id and parent @id so content lands under the target subsite."""
        item["@id"] = self._target_url(item["@id"])

        parent = item.get("parent")
        if parent:
            parent = dict(parent)
            parent["@id"] = self._target_url(parent["@id"])
            if IPloneSiteRoot.providedBy(obj.__parent__):
                parent["@type"] = self.SUBSITE_TYPE
                parent.pop("UID", None)
            item["parent"] = parent
        return item

    def _target_url(self, old_url):
        rel = self._relative_path(old_url, self.old_root)
        return urljoin(self.target_root + "/", rel.lstrip("/"))

    def _relative_path(self, url, root):
        root_parsed = urlparse(root.rstrip("/") + "/")
        url_parsed = urlparse(url)
        root_path = root_parsed.path.rstrip("/")
        url_path = url_parsed.path
        if url_path.startswith(root_path + "/"):
            return url_path[len(root_path):]
        if url_path == root_path:
            return "/"
        return url_path

    # ----------------------------------------------------------------------
    # Helpers: blocks
    # ----------------------------------------------------------------------

    def _build_blocks(self, item, obj):
        """Convert the legacy ``text`` field to Volto blocks."""
        if item.get("@type") not in self.BLOCKS_TYPES:
            item.pop("text", None)
            return item

        html = self._extract_html(item)
        blocks, layout = self._build_blocks_from_html(html, obj)
        if blocks:
            item["blocks"] = blocks
            item["blocks_layout"] = {"items": layout}
        item.pop("text", None)
        return item

    def _build_blocks_from_html(self, html, obj):
        """Build a complete Volto blocks structure from HTML."""
        blocks = {}
        layout = []

        title_uid = str(uuid.uuid4())
        blocks[title_uid] = {"@type": "title"}
        layout.append(title_uid)

        if html and html.strip():
            try:
                body_blocks, body_layout = self._html_to_blocks(html)
                blocks.update(body_blocks)
                layout.extend(body_layout)
            except Exception as exc:
                path = obj.absolute_url()
                logger.warning("Converter failed for %s: %s", path, exc)
                self.unconvertible.append(
                    {"path": path, "reason": str(exc), "type": obj.portal_type}
                )

        return blocks, layout

    def _html_to_blocks(self, html):
        """Call the blocks converter and return (blocks_dict, layout_items)."""
        if not html or not html.strip():
            return {}, []

        payload = self._post_json(self.converter_url, {"html": html})
        blocks_list = payload.get("data", [])

        blocks = {}
        layout = []
        for entry in blocks_list:
            # Converter returns [[uid, block], ...]
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                uid, block = entry
            elif isinstance(entry, dict) and "uid" in entry and "block" in entry:
                uid, block = entry["uid"], entry["block"]
            else:
                logger.warning("Unexpected converter entry shape: %r", entry)
                continue
            blocks[uid] = block
            layout.append(uid)
        return blocks, layout

    def _post_json(self, url, payload, timeout=60.0):
        """POST JSON data and return the parsed JSON response."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _merge_default_page(self, item, obj):
        """Copy non-title body blocks from a default page onto its Folder."""
        if not getattr(obj, "isPrincipiaFolderish", False):
            return item

        default_page_id = obj.getDefaultPage()
        if not default_page_id:
            return item

        try:
            default_page = obj[default_page_id]
        except KeyError:
            return item

        html = self._extract_html(default_page)
        if not html or not html.strip():
            return item

        try:
            body_blocks, body_layout = self._html_to_blocks(html)
        except Exception:
            return item

        existing_blocks = item.setdefault("blocks", {})
        existing_layout = item.setdefault("blocks_layout", {"items": []})["items"]

        for uid in body_layout:
            block = body_blocks[uid]
            # Skip the default page's title block; the Folder already has one.
            if block.get("@type") == "title":
                continue
            existing_blocks[uid] = block
            existing_layout.append(uid)

        logger.info(
            "Merged default page %s into %s", default_page_id, item.get("@id")
        )
        return item

    _INLINE_SLATE_TYPES = {"strong", "em", "u", "s", "code", "a", "sub", "sup"}

    def _extract_inline_images(self, item, obj):
        """Promote inline Slate images to standalone image blocks.

        The blocks converter turns inline <img> tags into Slate inline images
        (``type: img`` nodes).  For a cleaner Volto layout, extract them into
        standalone ``image`` blocks placed before the Slate block they came
        from.
        """
        blocks = item.get("blocks")
        layout = item.get("blocks_layout", {}).get("items")
        if not blocks or not layout:
            return item

        new_layout = []
        new_blocks = dict(blocks)

        for block_id in layout:
            block = new_blocks.get(block_id)
            if block and block.get("@type") == "slate":
                extracted = []
                cleaned_value = self._remove_inline_images(
                    block.get("value", []), extracted
                )
                cleaned_value = self._clean_empty_inline_nodes(cleaned_value)

                for img_info in extracted:
                    img_block_id = str(uuid.uuid4())
                    img_block = {
                        "@type": "image",
                        "url": img_info["url"],
                        "alt": img_info.get("alt", ""),
                        "title": img_info.get("title", ""),
                        "align": img_info.get("align", ""),
                    }
                    if img_info.get("image_scales"):
                        img_block["image_scales"] = img_info["image_scales"]
                    new_blocks[img_block_id] = img_block
                    new_layout.append(img_block_id)

                if extracted:
                    block["value"] = [
                        node
                        for node in cleaned_value
                        if not self._is_empty_slate_node(node)
                    ]
                    block.pop("plaintext", None)

            new_layout.append(block_id)

        item["blocks"] = new_blocks
        item["blocks_layout"]["items"] = new_layout
        return item

    def _clean_empty_inline_nodes(self, value):
        """Remove inline formatting nodes that became empty after image extraction."""
        if isinstance(value, list):
            result = []
            for child in value:
                cleaned = self._clean_empty_inline_nodes(child)
                if cleaned is None:
                    continue
                if isinstance(cleaned, list):
                    result.extend(cleaned)
                else:
                    result.append(cleaned)
            return result

        if isinstance(value, dict):
            node_type = value.get("type")
            children = value.get("children")
            if children is not None:
                value = dict(value)
                value["children"] = self._clean_empty_inline_nodes(children)
            if node_type in self._INLINE_SLATE_TYPES and not self._has_any_text(value):
                return None
            return value

        return value

    def _has_any_text(self, node):
        """Return True if a Slate node contains non-empty text."""
        if isinstance(node, dict):
            if node.get("text"):
                return True
            return any(
                self._has_any_text(child) for child in node.get("children", [])
            )
        if isinstance(node, list):
            return any(self._has_any_text(child) for child in node)
        return False

    def _remove_inline_images(self, value, extracted):
        """Recursively walk a Slate value and extract ``type: img`` nodes.

        Returns the cleaned value tree; removed image nodes are appended to
        ``extracted`` as dicts with url/alt/title/scale/align/image_scales.
        """
        if isinstance(value, list):
            result = []
            for child in value:
                cleaned = self._remove_inline_images(child, extracted)
                if cleaned is None:
                    continue
                if isinstance(cleaned, list):
                    result.extend(cleaned)
                else:
                    result.append(cleaned)
            return result

        if isinstance(value, dict):
            if value.get("type") == "img":
                extracted.append(
                    {
                        "url": value.get("url"),
                        "alt": value.get("alt", ""),
                        "title": value.get("title", ""),
                        "scale": value.get("scale"),
                        "align": value.get("align", ""),
                        "image_scales": value.get("image_scales"),
                    }
                )
                return None

            cleaned = dict(value)
            if "children" in cleaned:
                cleaned["children"] = self._remove_inline_images(
                    cleaned["children"], extracted
                )
            return cleaned

        return value

    def _is_empty_slate_node(self, node):
        """Return True if a Slate node has no visible text."""
        if not isinstance(node, dict):
            return False
        text = node.get("text")
        if text is not None:
            return not str(text).strip()
        children = node.get("children", [])
        if not children:
            return True
        return all(self._is_empty_slate_node(child) for child in children)

    # ----------------------------------------------------------------------
    # Helpers: resolveuid normalization
    # ----------------------------------------------------------------------

    def _normalize_blocks(self, item, obj):
        """Normalize resolveuid URLs inside Volto blocks."""
        blocks = item.get("blocks")
        if not blocks:
            return item
        item["blocks"] = self._normalize_block_urls(blocks)
        return item

    def _normalize_resolveuid_url(self, url, scale=None):
        """Keep internal references as ``/resolveuid/<uid>`` with optional scale."""
        if not url or "resolveuid/" not in url:
            return url
        match = RESOLVEUID_REF_RE.search(url)
        if not match:
            return url
        uid = match.group(1)
        if scale:
            return "/resolveuid/{}/@@images/image/{}".format(uid, scale)
        suffix = url[match.end():]
        return "/resolveuid/{}{}".format(uid, suffix)

    def _normalize_block_urls(self, value):
        """Recursively normalize resolveuid URLs inside block structures."""
        if isinstance(value, dict):
            if value.get("@type") == "image" and isinstance(value.get("url"), str):
                new_value = dict(value)
                new_value["url"] = self._normalize_resolveuid_url(
                    value["url"], value.get("scale")
                )
                return new_value

            if value.get("type") == "img" and isinstance(value.get("url"), str):
                new_value = dict(value)
                new_value["url"] = self._normalize_resolveuid_url(
                    value["url"], value.get("scale")
                )
                new_value["children"] = self._normalize_block_urls(
                    value.get("children", [])
                )
                return new_value

            return {k: self._normalize_block_urls(v) for k, v in value.items()}

        if isinstance(value, list):
            return [self._normalize_block_urls(v) for v in value]

        if isinstance(value, str):
            return self._normalize_resolveuid_url(value)

        return value

    # ----------------------------------------------------------------------
    # Helpers: manifest and reports
    # ----------------------------------------------------------------------

    def _record_manifest(self, item, obj):
        self.manifest.append(
            {
                "uid": item.get("UID"),
                "@id": item.get("@id"),
                "@type": item.get("@type"),
                "review_state": item.get("review_state"),
            }
        )
        return item

    def write_reports(self):
        """Write manifest, collections, unconvertible log and Markdown summary."""
        generated = datetime.now(timezone.utc).isoformat() + "Z"
        counts = Counter(i["@type"] for i in self.manifest)

        manifest_path = os.path.join(self.report_dir, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(
                {
                    "generated": generated,
                    "old_root": self.old_root,
                    "target_root": self.target_root,
                    "converter_url": self.converter_url,
                    "include_private": self.include_private,
                    "total": len(self.manifest),
                    "counts": dict(counts),
                    "items": self.manifest,
                },
                f,
                indent=2,
            )
        logger.info("Wrote %s", manifest_path)

        collections_path = os.path.join(self.report_dir, "collections.json")
        with open(collections_path, "w") as f:
            json.dump(self.collections, f, indent=2)
        logger.info("Wrote %s", collections_path)

        unconvertible_path = os.path.join(self.report_dir, "unconvertible.json")
        with open(unconvertible_path, "w") as f:
            json.dump(self.unconvertible, f, indent=2)
        logger.info("Wrote %s", unconvertible_path)

        report_path = os.path.join(self.report_dir, "MIGRATION_REPORT.md")
        lines = [
            "# EPANET Migration Report",
            "",
            "**Generated:** {}".format(generated),
            "",
            "## Configuration",
            "",
            "- Old root: `{}`".format(self.old_root),
            "- Target root: `{}`".format(self.target_root),
            "- Converter URL: `{}`".format(self.converter_url),
            "- Include private items: `{}`".format(self.include_private),
            "",
            "## Counts",
            "",
            "| Type | Transformed |",
            "|---|---|",
        ]
        for t in sorted(counts):
            lines.append("| {} | {} |".format(t, counts[t]))
        lines.extend(
            [
                "",
                "- **Total transformed items:** {}".format(len(self.manifest)),
                "- **Collections extracted:** {}".format(len(self.collections)),
                "- **Unconvertible items:** {}".format(len(self.unconvertible)),
                "- **Transform errors:** {}".format(len(self.transform_errors)),
                "",
            ]
        )

        if self.collections:
            lines.extend(
                [
                    "## Collections (manual rebuild as listing blocks)",
                    "",
                ]
            )
            for c in self.collections:
                lines.append("- `{}`".format(c.get("@id")))
                lines.append("  - query: `{}`".format(json.dumps(c.get("query", []))))
                lines.append("  - sort_on: `{}`".format(c.get("sort_on")))
                lines.append("  - sort_reversed: `{}`".format(c.get("sort_reversed")))
                lines.append("  - limit: `{}`".format(c.get("limit")))

        if self.unconvertible:
            lines.extend(
                [
                    "",
                    "## Unconvertible items ({})".format(len(self.unconvertible)),
                    "",
                ]
            )
            for entry in self.unconvertible:
                lines.append(
                    "- `{}` ({}): {}".format(
                        entry.get("path"), entry.get("type"), entry.get("reason")
                    )
                )

        if self.transform_errors:
            lines.extend(
                [
                    "",
                    "## Transform errors ({})".format(len(self.transform_errors)),
                    "",
                ]
            )
            for err in self.transform_errors:
                lines.append("- {}".format(err))

        lines.append("")
        with open(report_path, "w") as f:
            f.write("\n".join(lines))
        logger.info("Wrote %s", report_path)


