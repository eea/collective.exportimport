# -*- coding: utf-8 -*-
"""Integration tests for @@export_epanet (blocks converter faked)."""
from collective.exportimport.interfaces import IMigrationMarker
from collective.exportimport.testing import COLLECTIVE_EXPORTIMPORT_INTEGRATION_TESTING
from DateTime import DateTime
from plone import api
from plone.app.testing import login
from plone.app.testing import logout
from plone.app.testing import SITE_OWNER_NAME
from plone.app.textfield.value import RichTextValue
from zope.interface import alsoProvides

import json
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

    def test_subsite_parent_uid(self):
        self.request.form.update({"target_root": TARGET})
        view = api.content.get_view("export_epanet", self.portal, self.request)
        view.update()
        # /en on www.eea.europa.eu and demo-www
        self.assertEqual(view.ctx.subsite_parent_uid, "4b5a784a7bd543b39d8a4feb2ab8a4d7")

        self.request.form["subsite_parent_uid"] = "f" * 32
        view.update()
        self.assertEqual(view.ctx.subsite_parent_uid, "f" * 32)

    def test_form_keeps_the_query_string(self):
        query = "target_root=https%3A//www.example.org/en/epanet&subsite_parent_uid=abc"
        self.request.environ["QUERY_STRING"] = query
        self.request.form.update({"target_root": TARGET})
        view = api.content.get_view("export_epanet", self.portal, self.request)
        html = view()
        self.assertIn('action="{}?{}"'.format(self.request.URL, query.replace("&", "&amp;")), html)

    def test_import_indexes_the_state_and_keeps_effective(self):
        """Export, delete, import: the catalog knows the published state (the
        navigation lists published items only) and effective is unchanged."""
        api.content.create(container=self.portal, type="Folder", id="target", title="Target")
        target = self.portal.absolute_url() + "/target"
        dated = api.content.create(container=self.portal, type="Document", id="about", title="About")
        dated.setEffectiveDate(DateTime("2019-09-04T12:57:00+00:00"))
        undated = api.content.create(container=self.portal, type="Document", id="contact", title="Contact")
        for obj in (dated, undated):
            api.content.transition(obj=obj, to_state="published")
        # published, but without an effective date (as EPANET's front page)
        undated.setEffectiveDate(None)
        undated.reindexObject()

        self.request.form.update({"target_root": target})
        view = api.content.get_view("export_epanet", self.portal, self.request)
        view._post_json = fake_converter
        view.portal_type = ["Document"]
        view.path = "/".join(self.portal.getPhysicalPath())
        view.depth = -1
        view.migration = True
        view.include_revisions = False
        view.errors = []
        view.update()
        data = json.loads(json.dumps(list(view.export_content())))
        self.assertNotIn("review_state", [i for i in data if i["id"] == "contact"][0])
        api.content.delete(objects=[dated, undated])

        # what @@import_content does per item; its do_import also commits
        importer = api.content.get_view("import_content", self.portal, self.request)
        importer.portal = self.portal
        importer.limit = None
        importer.commit = None
        importer.import_to_current_folder = False
        importer.handle_existing_content = 0
        importer.import_old_revisions = False
        alsoProvides(self.request, IMigrationMarker)
        importer.import_new_content(data)

        catalog = api.portal.get_tool("portal_catalog")
        about = self.portal["target"]["about"]
        self.assertEqual(api.content.get_state(about), "published")
        self.assertTrue(catalog(UID=about.UID(), review_state="published"))
        self.assertEqual(
            about.effective().timeTime(), DateTime("2019-09-04T12:57:00+00:00").timeTime()
        )
        contact = self.portal["target"]["contact"]
        self.assertEqual(api.content.get_state(contact), "published")
        self.assertEqual(contact.EffectiveDate(), "None")

        # anonymous users see it without updateRoleMappings or reindexing
        logout()
        self.assertTrue(catalog(UID=about.UID(), review_state="published"))
        self.assertTrue(api.user.has_permission("View", obj=about))

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
        meetings = api.content.create(
            container=self.portal, type="Folder", id="meetings", title="Meetings"
        )
        meeting_query = [{"i": "portal_type",
                          "o": "plone.app.querystring.operation.selection.any",
                          "v": ["News Item"]}]
        api.content.create(
            container=meetings, type="Collection", id="meetings-1", title="Meetings",
            text=html("<p>All plenary meetings</p>"), query=meeting_query,
            sort_on="effective", sort_reversed=True,
        )
        meetings.setDefaultPage("meetings-1")
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
        self.portal["news"].setEffectiveDate(DateTime("2019-09-04T12:57:00+00:00"))
        for obj_id in ("group", "meetings", "news", "reports"):
            api.content.transition(obj=self.portal[obj_id], to_state="published")
        api.content.transition(obj=meetings["meetings-1"], to_state="published")
        api.content.transition(obj=folder["members"], to_state="published")

        items = self.export(
            ["Image", "Folder", "Document", "News Item", "Collection", "Link"]
        )

        self.assertNotIn(TARGET + "/draft", items)  # private
        self.assertIn(TARGET + "/agency", items)  # private Link: a link target
        self.assertEqual(items[TARGET + "/agency"]["review_state"], "private")
        self.assertEqual(items[TARGET + "/news"]["review_state"], "published")

        group = items[TARGET + "/group"]
        self.assertEqual(group["@type"], "Document")
        self.assertEqual(self.block_types(group), ["title", "slate"])
        self.assertEqual(group["parent"]["@type"], "Subsite")

        # a Collection as default page: its text and its listing
        folder_view = items[TARGET + "/meetings"]
        self.assertEqual(self.block_types(folder_view), ["title", "slate", "listing"])
        listing = folder_view["blocks"][folder_view["blocks_layout"]["items"][-1]]
        self.assertEqual(listing["querystring"]["query"], meeting_query)
        self.assertEqual(listing["querystring"]["sort_order"], "descending")

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
