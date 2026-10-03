# -*- coding: utf-8 -*-
from __future__ import annotations

from .export_content import ExportContent
from .export_content import fix_portal_type
from Acquisition import aq_base
from App.config import getConfiguration
from collective.exportimport import config
from collective.exportimport import epanet_transforms as transforms
from plone import api
from plone.app.contenttypes.interfaces import ICollection
from plone.restapi.interfaces import ISerializeToJson
from Products.CMFPlone.interfaces import IPloneSiteRoot
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from zope.component import getMultiAdapter

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
import urllib.request


logger = logging.getLogger(__name__)


class ExportCustomContent(ExportContent):
    """Export classic content as an import-ready Volto subsite.

    Every exported item goes through
    :func:`collective.exportimport.epanet_transforms.transform_item`: type
    mapping, URL rewrite to ``target_root``, HTML-to-blocks conversion,
    default-page merge, Collections as Documents with a listing block, News
    Item layout, inline images as image blocks with their original float,
    resolveuid normalization and Slate clean-up. The site root's default page
    becomes the target subsite's own blocks.

    The resulting JSON is imported on the target with "Update existing
    content" (``handle_existing_content=2``) at the site root; the subsite
    must exist there.

    Usage::

        @@export_custom_content?target_root=https://demo-www.eea.europa.eu/en/epanet
            &converter_url=http://localhost:8000/toblocks
            [&subsite_parent_uid=<UID of the subsite's parent on the target>]

    Report artifacts are written to ``<clienthome>/custom-export-reports/``:
    manifest.json, collections.json, unconvertible.json, MIGRATION_REPORT.md.
    """

    template = ViewPageTemplateFile("templates/export_content.pt")

    # Source site root.  When None it defaults to the current portal URL.
    OLD_ROOT = None

    # Target subsite where the content will be imported.
    # For :class:`ExportCustomContent` (``@@export_custom_content``) this is
    # intentionally unset; callers must pass ``target_root`` as a request
    # parameter so the view cannot silently rewrite URLs to the wrong subsite.
    TARGET_ROOT = None

    # eea-volto-blocks-converter endpoint.
    CONVERTER_URL = "http://localhost:8000/toblocks"

    # Set to True to also export items whose workflow state is ``private``.
    INCLUDE_PRIVATE = False

    # Private items of these types are still exported because other content
    # links to them (e.g. Link items only carry an external remoteUrl).
    PRIVATE_TYPE_EXCEPTIONS = {"Link"}

    # Path below the subsite whose logo table becomes a teaser grid, or None.
    TEASER_PAGE = None

    def update(self):
        """Read runtime configuration from request or class attributes."""
        target_root = self.request.form.get("target_root", self.TARGET_ROOT)
        if target_root:
            self.target_root = target_root.rstrip("/")
        elif getattr(self, "target_root", None):
            # A subclass may have set a default via self.target_root.
            self.target_root = self.target_root.rstrip("/")
        else:
            raise ValueError(
                "target_root is required for @@export_custom_content. "
                "Pass e.g. ?target_root=https://demo-www.eea.europa.eu/en/climate-energy"
            )

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

        self.ctx = transforms.Context(
            old_root=self.old_root,
            target_root=self.target_root,
            convert=self._html_to_blocks,
            old_site_path="/".join(portal.getPhysicalPath()),
            default_page_html=self._default_page_html,
            resolve_uid=self._resolve_uid,
            site_default_page=self._default_page_id(portal),
            teaser_page=self.TEASER_PAGE,
            subsite_parent_uid=self.request.form.get("subsite_parent_uid") or None,
        )

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
        self.report_dir = os.path.join(directory, "custom-export-reports")
        if not os.path.exists(self.report_dir):
            os.makedirs(self.report_dir)
            logger.info("Created custom export report directory %s", self.report_dir)

    def finish(self):
        """Write migration report artifacts."""
        self.write_reports()

    def global_obj_hook(self, obj):
        """Filter objects before serialization.

        Return None to skip an object.
        """
        if obj.portal_type not in transforms.TYPE_MAP:
            logger.warning(
                "Skipping unknown type %s at %s", obj.portal_type, obj.absolute_url()
            )
            return None

        if not self.include_private and obj.portal_type not in self.PRIVATE_TYPE_EXCEPTIONS:
            review_state = api.content.get_state(obj, default=None)
            if review_state == "private":
                logger.info("Skipping private item %s", obj.absolute_url())
                return None

        return obj

    def global_dict_hook(self, item, obj):
        """Turn the serialized item into its import-ready form."""
        if ICollection.providedBy(obj):
            self.collections.append(
                {key: item.get(key) for key in
                 ("@id", "title", "query", "sort_on", "sort_reversed", "limit")}
            )
        try:
            item = transforms.transform_item(item, self.ctx)
        except Exception as exc:
            path = item.get("@id", obj.absolute_url())
            msg = "Failed to transform {}: {}".format(path, exc)
            logger.exception(msg)
            self.transform_errors.append(msg)
            return None
        self._record_manifest(item, obj)
        return item

    def export_content(self):
        """Override base export to add optional pagination and list-yield support.

        This keeps the EPANET-branch extra behavior (``p``/``nrOfHits`` query
        parameters and yielding multiple items from one serialized object)
        inside :class:`ExportCustomContent` without touching the base
        :class:`ExportContent` implementation used by ``@@export_content``.
        """
        query = self.build_query()
        catalog = api.portal.get_tool("portal_catalog")
        brains = catalog.unrestrictedSearchResults(**query)
        p = int(self.request.get("p", "0") or "0")
        nrOfHits = int(self.request.get("nrOfHits", "0") or "0")
        cindex = 0
        logger.info(u"Exporting {} {}".format(len(brains), self.portal_type))

        # Override richtext serializer to export links using resolveuid/xxx
        alsoProvides = __import__(
            "zope.interface",
            fromlist=["alsoProvides"],
        ).alsoProvides
        from collective.exportimport.serializer import IRawRichTextMarker
        alsoProvides(self.request, IRawRichTextMarker)

        for index, brain in enumerate(brains, start=1):
            skip = False
            if brain.UID in self.DROP_UIDS:
                continue

            for drop in self.DROP_PATHS:
                if drop in brain.getPath():
                    skip = True

            if skip:
                continue

            if p and nrOfHits:
                startIndex = (p - 1) * nrOfHits
                endIndex = p * nrOfHits
                if cindex < startIndex:
                    cindex += 1
                    continue
                if cindex >= endIndex:
                    break
                cindex += 1

            if not index % 100:
                logger.info(u"Handled {} items...".format(index))
            try:
                obj = brain.getObject()
            except Exception:
                msg = u"Error getting brain {}".format(brain.getPath())
                self.errors.append({"path": None, "message": msg})
                logger.exception(msg, exc_info=True)
                continue
            if obj is None:
                msg = u"brain.getObject() is None {}".format(brain.getPath())
                logger.error(msg)
                self.errors.append({"path": None, "message": msg})
                continue
            obj = self.global_obj_hook(obj)
            if not obj:
                continue
            try:
                self.safe_portal_type = fix_portal_type(obj.portal_type)
                serializer = getMultiAdapter((obj, self.request), ISerializeToJson)
                if IPloneSiteRoot.providedBy(obj):
                    item = serializer()
                elif getattr(aq_base(obj), "isPrincipiaFolderish", False):
                    item = serializer(include_items=False)
                elif ICollection.providedBy(obj):
                    item = serializer(include_items=False)
                else:
                    item = serializer()
                item = self.update_export_data(item, obj)
                if not item:
                    continue

                if isinstance(item, list):
                    for i in item:
                        yield i
                else:
                    yield item
            except Exception:
                msg = u"Error exporting {}".format(obj.absolute_url())
                self.errors.append({"path": obj.absolute_url(), "message": msg})
                logger.exception(msg, exc_info=True)

    # ----------------------------------------------------------------------
    # Context callbacks: what the transforms need from the source site
    # ----------------------------------------------------------------------

    def _extract_html(self, obj):
        """Return the raw HTML of a live object's rich text field."""
        text_field = getattr(obj, "text", None)
        if hasattr(text_field, "raw"):
            # DX RichTextValue; raw keeps resolveuid links, like the export.
            return text_field.raw or ""
        if isinstance(text_field, str):
            return text_field
        return ""

    def _default_page_id(self, obj):
        """Id of a folderish object's default page, or None."""
        get_default_page = getattr(obj, "getDefaultPage", None)
        if get_default_page is not None:
            page_id = get_default_page()
        else:
            page_id = getattr(aq_base(obj), "default_page", None)
        if page_id and page_id in obj:
            return page_id
        return None

    def _default_page_html(self, item):
        """HTML of the default page of the Folder ``item`` was serialized from."""
        obj = api.content.get(UID=item.get("UID")) if item.get("UID") else None
        if obj is None:
            return None
        page_id = self._default_page_id(obj)
        if not page_id:
            return None
        logger.info("Merging default page %s into %s", page_id, obj.absolute_url())
        return self._extract_html(obj[page_id])

    def _resolve_uid(self, uid):
        """Target URL of the source object with this UID, or None."""
        obj = api.content.get(UID=uid)
        if obj is None:
            return None
        return transforms.target_url(obj.absolute_url(), self.ctx)

    def _html_to_blocks(self, html):
        """Call the blocks converter and return (blocks_dict, layout_items)."""
        if not html or not html.strip():
            return {}, []

        try:
            payload = self._post_json(self.converter_url, {"html": html})
        except Exception as exc:
            logger.warning("Converter failed: %s", exc)
            self.unconvertible.append({"html": html[:200], "reason": str(exc)})
            return {}, []
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

    # ----------------------------------------------------------------------
    # Helpers: manifest and reports
    # ----------------------------------------------------------------------

    def _record_manifest(self, item, obj):
        self.manifest.append(
            {
                "uid": item.get("UID"),
                "@id": item.get("@id"),
                "@type": item.get("@type"),
                "review_state": api.content.get_state(obj, default=None),
                "textless_slate": transforms.count_textless_slate(item),
            }
        )

    def write_reports(self):
        """Write manifest, collections, unconvertible log and Markdown summary."""
        generated = datetime.now(timezone.utc).isoformat() + "Z"
        counts = Counter(i["@type"] for i in self.manifest)
        textless = sum(i["textless_slate"] for i in self.manifest)

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
            "# Custom Content Migration Report",
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
                "- **Collections converted to Documents:** {}".format(len(self.collections)),
                "- **Unconvertible HTML fragments:** {}".format(len(self.unconvertible)),
                "- **Transform errors:** {}".format(len(self.transform_errors)),
                "- **Slate elements without text:** {}".format(textless),
                "",
            ]
        )

        if self.collections:
            lines.extend(
                [
                    "## Collections (converted to Documents with a listing block)",
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
                    "## Unconvertible HTML ({})".format(len(self.unconvertible)),
                    "",
                ]
            )
            for entry in self.unconvertible:
                lines.append("- `{}`: {}".format(entry.get("html"), entry.get("reason")))

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


class ExportEpanet(ExportCustomContent):
    """``@@export_epanet``: the EPANET site as an import-ready subsite.

    Defaults ``target_root`` to the EEA subsite and turns the member logo
    table on ``/our-group`` into a teaser grid.
    """

    TARGET_ROOT = "https://demo-www.eea.europa.eu/en/epanet"
    TEASER_PAGE = "/our-group"
