# -*- coding: utf-8 -*-
"""Integration tests for @@export_epanet (blocks converter faked)."""
from collective.exportimport.testing import COLLECTIVE_EXPORTIMPORT_INTEGRATION_TESTING
from plone import api
from plone.app.testing import login
from plone.app.testing import SITE_OWNER_NAME
from plone.app.textfield.value import RichTextValue

import re
import unittest
import uuid


TARGET = "https://www.example.org/en/epanet"


def fake_converter(url, payload, timeout=60.0):
    """One Slate paragraph per HTML; <img> tags become inline images."""
    html = payload["html"]
    children = [
        {"type": "img", "url": "resolveuid/" + uid, "children": [{"text": ""}]}
        for uid in re.findall(r'data-val="([a-f0-9]{32})"', html)
    ]
    children.append({"text": re.sub(r"<[^>]+>", "", html)})
    block = {"@type": "slate", "value": [{"type": "p", "children": children}]}
    return {"data": [[str(uuid.uuid4()), block]]}


def html(value):
    return RichTextValue(value, "text/html", "text/x-html-safe")


class TestExportEpanet(unittest.TestCase):

    layer = COLLECTIVE_EXPORTIMPORT_INTEGRATION_TESTING

    def setUp(self):
        self.portal = self.layer["portal"]
        self.request = self.layer["request"]
        login(self.layer["app"], SITE_OWNER_NAME)

    def export(self, portal_types):
        self.request.form.update({"target_root": TARGET})
        view = api.content.get_view("export_epanet", self.portal, self.request)
        view._post_json = fake_converter
        view.portal_type = portal_types
        view.path = "/".join(self.portal.getPhysicalPath())
        view.depth = -1
        view.migration = True
        view.include_revisions = False
        view.errors = []
        view.update()
        items = list(view.export_content())
        self.assertEqual(view.errors, [])
        self.assertEqual(view.transform_errors, [])
        return {item["@id"]: item for item in items}

    def block_types(self, item):
        return [item["blocks"][uid]["@type"] for uid in item["blocks_layout"]["items"]]

    def test_export(self):
        image = api.content.create(
            container=self.portal, type="Image", id="logo", title="Logo"
        )
        folder = api.content.create(
            container=self.portal, type="Folder", id="group", title="Group"
        )
        api.content.create(
            container=folder, type="Document", id="members", title="Members",
            text=html("<p>Our members</p>"),
        )
        folder.setDefaultPage("members")
        api.content.create(
            container=self.portal, type="News Item", id="news", title="News",
            text=html(
                '<p><img class="image-richtext image-right" data-val="{0}" '
                'src="resolveuid/{0}/@@images/image/large" />Plenary</p>'.format(image.UID())
            ),
        )
        api.content.create(
            container=self.portal, type="Collection", id="reports", title="Reports",
            query=[{"i": "portal_type",
                    "o": "plone.app.querystring.operation.selection.any",
                    "v": ["File"]}],
        )
        api.content.create(container=self.portal, type="Document", id="draft", title="Draft")
        api.content.create(
            container=self.portal, type="Link", id="agency", title="Agency",
            remoteUrl="https://agency.example.org",
        )
        for obj_id in ("group", "news", "reports"):
            api.content.transition(obj=self.portal[obj_id], to_state="published")
        api.content.transition(obj=folder["members"], to_state="published")

        items = self.export(
            ["Image", "Folder", "Document", "News Item", "Collection", "Link"]
        )

        self.assertNotIn(TARGET + "/draft", items)  # private
        self.assertIn(TARGET + "/agency", items)  # private Link: a link target
        for item in items.values():
            self.assertNotIn("review_state", item)

        group = items[TARGET + "/group"]
        self.assertEqual(group["@type"], "Document")
        self.assertEqual(self.block_types(group), ["title", "slate"])
        self.assertEqual(group["parent"]["@type"], "Subsite")

        news = items[TARGET + "/news"]
        self.assertEqual(
            self.block_types(news),
            ["title", "layoutSettings", "description", "dividerBlock", "image", "slate"],
        )
        picture = news["blocks"][news["blocks_layout"]["items"][4]]
        self.assertEqual(picture["url"], "/resolveuid/" + image.UID())
        self.assertEqual(picture["align"], "right")

        reports = items[TARGET + "/reports"]
        self.assertEqual(reports["@type"], "Document")
        self.assertEqual(self.block_types(reports), ["title", "listing"])
