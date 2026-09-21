# -*- coding: utf-8 -*-
from Acquisition import aq_base
from collective.exportimport import _
from plone import api
from Products.Five import BrowserView
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile

import logging
import transaction
import uuid


logger = logging.getLogger(__name__)

INLINE_TYPES = {"strong", "em", "u", "s", "code", "a", "sub", "sup"}


def is_empty_slate_node(node):
    if not isinstance(node, dict):
        return False
    text = node.get("text")
    if text is not None:
        return not str(text).strip()
    children = node.get("children", [])
    if not children:
        return True
    return all(is_empty_slate_node(child) for child in children)


def has_any_text(node):
    if isinstance(node, dict):
        if node.get("text"):
            return True
        return any(has_any_text(child) for child in node.get("children", []))
    if isinstance(node, list):
        return any(has_any_text(child) for child in node)
    return False


def clean_empty_inline_nodes(value):
    """Remove inline formatting nodes that became empty after image extraction."""
    if isinstance(value, list):
        result = []
        for child in value:
            cleaned = clean_empty_inline_nodes(child)
            if cleaned is None:
                continue
            if isinstance(cleaned, list):
                result.extend(cleaned)
            else:
                result.append(cleaned)
        return result
    if isinstance(value, dict):
        node_type = value.get("type")
        if "children" in value:
            value = dict(value)
            value["children"] = clean_empty_inline_nodes(value["children"])
        if node_type in INLINE_TYPES and not has_any_text(value):
            return None
        return value
    return value


def remove_inline_images(value, extracted):
    if isinstance(value, list):
        result = []
        for child in value:
            cleaned = remove_inline_images(child, extracted)
            if cleaned is None:
                continue
            if isinstance(cleaned, list):
                result.extend(cleaned)
            else:
                result.append(cleaned)
        return result
    if isinstance(value, dict):
        if value.get("type") == "img":
            extracted.append(dict(value))
            return None
        cleaned = dict(value)
        if "children" in cleaned:
            cleaned["children"] = remove_inline_images(cleaned["children"], extracted)
        return cleaned
    return value


def make_image_block(img_info):
    url = img_info.get("url", "")
    if "/@@images" in url:
        url = url.split("/@@images", 1)[0]
    block = {
        "@type": "image",
        "url": url,
        "alt": img_info.get("alt", ""),
        "title": img_info.get("title", ""),
        "align": img_info.get("align", ""),
    }
    scale = img_info.get("scale")
    if scale:
        block["scale"] = scale
    if img_info.get("image_scales"):
        block["image_scales"] = img_info["image_scales"]
    return block


def has_existing_side_menu(node):
    """Recursively check whether content already contains an accordion side menu."""
    if not isinstance(node, dict):
        return False
    if node.get("@type") == "contextNavigation" and node.get("variation") == "accordion":
        return True
    blocks = node.get("blocks") or node.get("data", {}).get("blocks")
    layout = node.get("blocks_layout") or node.get("data", {}).get("blocks_layout")
    if not (blocks and layout and layout.get("items")):
        return False
    return any(has_existing_side_menu(blocks.get(block_id)) for block_id in layout["items"])


def extract_inline_images(blocks, layout_items):
    """Return (new_blocks, new_layout, number_of_extracted_images)."""
    if not blocks or not layout_items:
        return blocks, layout_items, 0

    new_layout = []
    new_blocks = dict(blocks)
    extracted_total = 0

    for block_id in layout_items:
        block = new_blocks.get(block_id)
        if block and block.get("@type") == "slate":
            extracted = []
            cleaned_value = remove_inline_images(block.get("value", []), extracted)
            cleaned_value = clean_empty_inline_nodes(cleaned_value)
            cleaned_value = [
                node for node in cleaned_value if not is_empty_slate_node(node)
            ]

            for img_info in extracted:
                img_block_id = str(uuid.uuid4())
                new_blocks[img_block_id] = make_image_block(img_info)
                new_layout.append(img_block_id)
                extracted_total += 1

            value_changed = cleaned_value != block.get("value")
            if extracted or value_changed:
                block = dict(block)
                block["value"] = cleaned_value
                block.pop("plaintext", None)
                if block["value"]:
                    new_blocks[block_id] = block
                    new_layout.append(block_id)
                else:
                    new_blocks.pop(block_id, None)
            else:
                new_layout.append(block_id)
        else:
            new_layout.append(block_id)

    return new_blocks, new_layout, extracted_total


def fix_inline_images(context=None, portal_types=None, dry_run=False, commit=True):
    """Extract inline Slate images into standalone image blocks.

    Searches the portal_catalog for objects of the given portal_types under
    ``context`` and mutates their ``blocks`` / ``blocks_layout`` attributes.
    """
    if portal_types is None:
        portal_types = ["Document", "News Item"]

    catalog = api.portal.get_tool("portal_catalog")
    query = {
        "portal_type": portal_types,
        "sort_on": "path",
    }
    if context is not None:
        query["path"] = "/".join(context.getPhysicalPath())

    brains = catalog(**query)
    total = len(brains)
    logger.info("Found %d item(s) to process", total)

    processed = 0
    patched = 0
    total_extracted = 0

    for index, brain in enumerate(brains, start=1):
        try:
            obj = brain.getObject()
        except Exception:
            logger.warning("Could not get object for: %s", brain.getPath(), exc_info=True)
            continue
        if obj is None:
            logger.error("brain.getObject() is None %s", brain.getPath())
            continue

        blocks = getattr(aq_base(obj), "blocks", {}) or {}
        layout = getattr(aq_base(obj), "blocks_layout", {}) or {}
        layout_items = layout.get("items", []) if isinstance(layout, dict) else []

        if not blocks or not layout_items:
            continue

        new_blocks, new_layout, extracted = extract_inline_images(blocks, layout_items)

        changed = new_blocks != blocks or new_layout != layout_items
        if not changed:
            logger.debug("No changes: %s", obj.absolute_url())
            continue

        processed += 1
        total_extracted += extracted
        logger.info(
            "%s %s: %d inline image(s) -> %d block(s) -> %d block(s)",
            "Would patch" if dry_run else "Patching",
            obj.absolute_url(),
            extracted,
            len(blocks),
            len(new_blocks),
        )

        if dry_run:
            continue

        obj.blocks = new_blocks
        obj.blocks_layout = {"items": new_layout}
        patched += 1

        if patched and not patched % 100:
            logger.info("Committed %d items so far", patched)
            if commit:
                transaction.commit()

    if commit and not dry_run:
        transaction.commit()

    logger.info(
        "Finished. Processed %d item(s), extracted %d image(s), patched %d item(s).",
        processed,
        total_extracted,
        patched,
    )
    return {
        "processed": processed,
        "extracted": total_extracted,
        "patched": patched,
    }


class FixInlineImages(BrowserView):
    """Browser view to extract inline Slate images into standalone image blocks."""

    index = ViewPageTemplateFile("templates/fix_inline_images.pt")

    def __call__(self):
        self.title = _(
            u"Extract inline Slate images into standalone image blocks"
        )
        if not self.request.form.get("form.submitted", False):
            return self.index()

        dry_run = bool(self.request.form.get("form.dry_run", False))
        commit = self.request.form.get("form.commit", "1") not in ("0", "false", "False")

        portal_types = self.request.form.get("portal_types", "Document,News Item")
        if isinstance(portal_types, str):
            portal_types = [x.strip() for x in portal_types.split(",") if x.strip()]

        results = fix_inline_images(
            context=self.context,
            portal_types=portal_types,
            dry_run=dry_run,
            commit=commit,
        )

        msg = _(
            u"Processed {processed} items, extracted {extracted} inline images, patched {patched} items"
        ).format(**results)
        if dry_run:
            msg = _(
                u"DRY RUN: {processed} items would be changed, {extracted} inline images would be extracted, {patched} items would be patched"
            ).format(**results)

        logger.info(msg)
        api.portal.show_message(msg, self.request)
        return self.index()
