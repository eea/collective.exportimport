# -*- coding: utf-8 -*-
"""News Item-only export views for the EPANET migration."""
from collective.exportimport.export_epanet import ExportCustomContent
from collective.exportimport.export_epanet import ExportEpanet


class NewsItemOnlyMixin(object):
    """Restrict an export to News Items.

    The News Item layout (title without content type, layoutSettings,
    description, divider, body) comes from the shared pipeline, so these
    views export exactly what ``@@export_epanet`` exports for News Items.
    """

    def build_query(self):
        query = super(NewsItemOnlyMixin, self).build_query()
        query["portal_type"] = ["News Item"]
        return query

    def global_obj_hook(self, obj):
        if obj.portal_type != "News Item":
            return None
        return super(NewsItemOnlyMixin, self).global_obj_hook(obj)


class ExportNewsItem(NewsItemOnlyMixin, ExportCustomContent):
    """``@@export_custom_newsItem``: News Items only; ``target_root`` required."""


class ExportEpanetNewsItem(NewsItemOnlyMixin, ExportEpanet):
    """``@@export_newsItem``: the News Items of ``@@export_epanet``."""
