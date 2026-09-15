# -*- coding: utf-8 -*-
"""News Item-only export view for the EPANET migration."""
from collective.exportimport.export_epanet import ExportEpanet


class ExportNewsItem(ExportEpanet):
    """Export only News Items, promoting inline Slate images to image blocks.

    This is a thin specialization of :class:`ExportEpanet`.  It limits the
    catalog query to ``News Item`` objects and runs the inline-image extractor
    on every exported item.

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
        """Apply the parent transforms, then extract inline Slate images."""
        item = super(ExportNewsItem, self).global_dict_hook(item, obj)
        if item is None:
            return None
        item = self._extract_inline_images(item, obj)
        return item
