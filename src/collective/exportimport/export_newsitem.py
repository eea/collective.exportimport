# -*- coding: utf-8 -*-
"""News Item-only export view for the EPANET migration."""
from collective.exportimport.export_epanet import ExportCustomContent

import uuid


class ExportNewsItem(ExportCustomContent):
    """Export only News Items, promoting inline Slate images to image blocks.

    This is a thin specialization of :class:`ExportCustomContent`.  It limits the
    catalog query to ``News Item`` objects, runs the inline-image extractor,
    and then applies the default News Item (press release) block layout around
    the existing migrated body blocks.

    Usage::

        @@export_newsItem?download_to_server=1
            &target_root=https://demo-www.eea.europa.eu/en/epanet
            &converter_url=http://volto-blocks-converter:8000/toblocks
    """

    def build_query(self):
        """Restrict the export to News Items."""
        query = super(ExportNewsItem, self).build_query()
        query["portal_type"] = ["News Item"]
        return query

    def global_obj_hook(self, obj):
        """Safety net: skip anything that is not a News Item."""
        if obj.portal_type != "News Item":
            return None
        return super(ExportNewsItem, self).global_obj_hook(obj)

    def global_dict_hook(self, item, obj):
        """Apply the parent transforms, then extract inline Slate images, then apply default blocks."""
        item = super(ExportNewsItem, self).global_dict_hook(item, obj)
        if item is None:
            return None
        item = self._extract_inline_images(item, obj)
        item = self._apply_news_item_default_blocks(item, obj)
        return item

    def _apply_news_item_default_blocks(self, item, obj):
        """Wrap existing News Item blocks with the default press-release layout.

        The default layout is:
            [existing title block]
            layoutSettings (narrow_view)
            description
            dividerBlock (hidden)
            [all existing body blocks preserved]
            listing (Our latest press releases, scoped to /en/epanet)
        """
        if item.get("@type") != "News Item":
            return item

        existing_blocks = item.get("blocks") or {}
        existing_layout = item.get("blocks_layout", {}).get("items", [])
        if not existing_layout:
            return item

        # Keep the first block only if it is the title block.
        title_id = existing_layout[0]
        if existing_blocks.get(title_id, {}).get("@type") == "title":
            first_block_id = title_id
            body_ids = existing_layout[1:]
        else:
            first_block_id = None
            body_ids = list(existing_layout)

        new_blocks = {}
        new_layout = []

        if first_block_id:
            new_blocks[first_block_id] = existing_blocks[first_block_id]
            new_layout.append(first_block_id)

        # Insert default header blocks.
        header_blocks = self._news_item_header_blocks()
        for uid, block in header_blocks:
            new_blocks[uid] = block
            new_layout.append(uid)

        # Preserve existing body blocks.
        for bid in body_ids:
            new_blocks[bid] = existing_blocks[bid]
            new_layout.append(bid)

        # Append default listing block.
        listing_uid, listing_block = self._news_item_listing_block()
        new_blocks[listing_uid] = listing_block
        new_layout.append(listing_uid)

        item["blocks"] = new_blocks
        item["blocks_layout"] = {"items": new_layout}
        return item

    def _news_item_header_blocks(self):
        """Return default header blocks copied from the demo press release template."""
        return [
            (str(uuid.uuid4()), {
                "@type": "layoutSettings",
                "block": str(uuid.uuid4()),
                "fixed": True,
                "layout_size": "narrow_view",
                "required": True,
            }),
            (str(uuid.uuid4()), {
                "@type": "description",
                "block": str(uuid.uuid4()),
                "fixed": True,
                "placeholder": "Add news item description",
                "required": True,
            }),
            (str(uuid.uuid4()), {
                "@type": "dividerBlock",
                "block": str(uuid.uuid4()),
                "hidden": True,
                "section": True,
            }),
        ]

    def _news_item_listing_block(self):
        """Return the default 'Our latest press releases' listing block."""
        return str(uuid.uuid4()), {
            "@type": "listing",
            "block": str(uuid.uuid4()),
            "headline": "Our latest press releases",
            "headlineTag": "h2",
            "itemModel": {},
            "query": [],
            "querystring": {
                "b_size": "9",
                "limit": "9",
                "query": [
                    {
                        "i": "path",
                        "o": "plone.app.querystring.operation.string.absolutePath",
                        "v": "/en/epanet::-1",
                    },
                    {
                        "i": "portal_type",
                        "o": "plone.app.querystring.operation.selection.any",
                        "v": ["News Item"],
                    },
                    {
                        "i": "review_state",
                        "o": "plone.app.querystring.operation.selection.any",
                        "v": ["published"],
                    },
                    {
                        "i": "effective",
                        "o": "plone.app.querystring.operation.date.beforeToday",
                        "v": "",
                    },
                ],
                "sort_on": "effective",
                "sort_order": "descending",
                "sort_order_boolean": True,
            },
            "slidesToScroll": 2,
            "slidesToShow": 2,
            "styles": {},
            "variation": "cardsCarousel",
        }


class ExportEpanetNewsItem(ExportNewsItem):
    """Backwards-compatible EPANET News Item export view.

    This subclass restores the original EPANET-specific ``TARGET_ROOT`` default
    so existing scripts and URLs using ``@@export_newsItem`` continue to work
    without passing ``target_root`` explicitly.
    """

    TARGET_ROOT = "https://demo-www.eea.europa.eu/en/epanet"

