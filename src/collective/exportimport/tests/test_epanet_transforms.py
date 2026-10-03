# -*- coding: utf-8 -*-
"""Tests for the @@export_epanet transforms (no Plone site needed)."""
from collective.exportimport import epanet_transforms as transforms

import unittest


OLD = "https://epanet.example.org"
TARGET = "https://www.example.org/en/epanet"
UID_A = "a" * 32
UID_B = "b" * 32


def slate(*children):
    return {"@type": "slate", "value": [{"type": "p", "children": list(children)}]}


def inline_img(uid):
    return {"type": "img", "url": "../../resolveuid/{}/@@images/image/large".format(uid),
            "children": [{"text": ""}]}


class FakeConverter(object):
    """Returns canned blocks per HTML snippet."""

    def __init__(self, mapping):
        self.mapping = mapping

    def __call__(self, html):
        blocks = self.mapping.get(html, [])
        layout = ["b{}".format(n) for n in range(len(blocks))]
        return dict(zip(layout, blocks)), layout


def item(**fields):
    data = {
        "@id": OLD + "/page",
        "@type": "Document",
        "id": "page",
        "UID": "c" * 32,
        "language": "en-gb",
        "parent": {"@id": OLD, "@type": "Plone Site", "UID": None},
        "review_state": "published",
    }
    data.update(fields)
    return data


def context(convert=None, **kwargs):
    return transforms.Context(
        old_root=OLD,
        target_root=TARGET,
        convert=convert or FakeConverter({}),
        old_site_path="/epanet",
        **kwargs
    )


def types(result):
    return [result["blocks"][uid]["@type"] for uid in result["blocks_layout"]["items"]]


class TestTransformItem(unittest.TestCase):

    def test_fields_urls_and_review_state(self):
        result = transforms.transform_item(
            item(layout="document_view", text={"data": ""}), context()
        )
        self.assertEqual(result["@id"], TARGET + "/page")
        self.assertEqual(result["language"], "en")
        # the importer must not run a transition: it would reset effective
        self.assertNotIn("review_state", result)
        self.assertNotIn("layout", result)
        self.assertNotIn("text", result)
        # top-level items hang below the existing subsite, found by path
        self.assertEqual(result["parent"]["@id"].rstrip("/"), TARGET)
        self.assertEqual(result["parent"]["@type"], "Subsite")
        self.assertNotIn("UID", result["parent"])

    def test_nested_parent_keeps_its_uid(self):
        result = transforms.transform_item(
            item(**{"@id": OLD + "/folder/page",
                    "parent": {"@id": OLD + "/folder", "@type": "Folder", "UID": UID_A}}),
            context(),
        )
        self.assertEqual(result["parent"]["@id"], TARGET + "/folder")
        self.assertEqual(result["parent"]["UID"], UID_A)

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            transforms.transform_item(item(**{"@type": "Event"}), context())

    def test_news_item_layout(self):
        html = "<p>Body</p>"
        convert = FakeConverter({html: [slate({"text": "Body"})]})
        result = transforms.transform_item(
            item(**{"@type": "News Item", "text": {"data": html}}), context(convert)
        )
        self.assertEqual(
            types(result),
            ["title", "layoutSettings", "description", "dividerBlock", "slate"],
        )
        title = result["blocks"][result["blocks_layout"]["items"][0]]
        self.assertTrue(title["hideContentType"])

    def test_document_description_block(self):
        result = transforms.transform_item(item(description="Intro"), context())
        self.assertEqual(types(result), ["title", "description"])

    def test_collection_becomes_document_with_listing(self):
        result = transforms.transform_item(
            item(**{
                "@type": "Collection",
                "query": [
                    {"i": "path", "o": "plone.app.querystring.operation.string.absolutePath",
                     "v": "/epanet/reports-letters::1"},
                    {"i": "portal_type", "o": "plone.app.querystring.operation.selection.any",
                     "v": ["File"]},
                ],
                "sort_on": "effective",
                "sort_reversed": True,
                "limit": 50,
            }),
            context(),
        )
        self.assertEqual(result["@type"], "Document")
        listing = result["blocks"][result["blocks_layout"]["items"][-1]]
        self.assertEqual(listing["@type"], "listing")
        query = listing["querystring"]["query"]
        # a site path, not a URL: absolutePath prefixes the portal path
        self.assertEqual(query[0]["v"], "/en/epanet/reports-letters::1")
        self.assertEqual(query[1]["v"], ["File"])
        self.assertEqual(listing["querystring"]["sort_order"], "descending")
        self.assertEqual(listing["querystring"]["limit"], "50")

    def test_folder_gets_its_default_page_body(self):
        html = "<p>Landing</p>"
        convert = FakeConverter({html: [{"@type": "title"}, slate({"text": "Landing"})]})
        result = transforms.transform_item(
            item(**{"@type": "Folder"}),
            context(convert, default_page_html=lambda i: html),
        )
        self.assertEqual(result["@type"], "Document")
        self.assertEqual(types(result), ["title", "slate"])


class TestImages(unittest.TestCase):

    def run_item(self, html, blocks):
        convert = FakeConverter({html: blocks})
        return transforms.transform_item(item(text={"data": html}), context(convert))

    def test_inline_image_becomes_aligned_image_block(self):
        html = ('<p><img class="image-richtext image-right" '
                'src="../../resolveuid/{0}/@@images/image/large" data-val="{0}" />Text</p>'
                ).format(UID_A)
        result = self.run_item(html, [slate(inline_img(UID_A), {"text": "Text"})])
        self.assertEqual(types(result), ["title", "image", "slate"])
        image = result["blocks"][result["blocks_layout"]["items"][1]]
        self.assertEqual(image["url"], "/resolveuid/{}/@@images/image/large".format(UID_A))
        self.assertEqual(image["align"], "right")

    def test_inline_style_wins_over_class(self):
        html = ('<img class="image-left" style="float: right;" '
                'src="resolveuid/{0}" data-val="{0}" />').format(UID_A)
        self.assertEqual(transforms.image_alignments(html), {UID_A: "right"})

    def test_image_inline_class_has_no_float(self):
        html = '<img class="image-inline" src="resolveuid/{0}" />'.format(UID_A)
        self.assertEqual(transforms.image_alignments(html), {})

    def test_empty_inline_wrapper_is_removed(self):
        # <strong><img/></strong>: once the image is moved out the <strong> is
        # empty, and the Slate editor crashes on it.
        html = '<p><strong><img src="resolveuid/{0}" /></strong>Text</p>'.format(UID_A)
        blocks = [slate({"type": "strong", "children": [inline_img(UID_A)]}, {"text": "Text"})]
        result = self.run_item(html, blocks)
        paragraph = result["blocks"][result["blocks_layout"]["items"][-1]]["value"][0]
        self.assertEqual(paragraph["children"], [{"text": "Text"}])
        self.assertEqual(transforms.count_textless_slate(result), 0)

    def test_clean_slate_keeps_a_text_leaf(self):
        data = {"blocks": {"x": slate({"type": "strong", "children": []})}}
        self.assertEqual(transforms.clean_slate(data), 1)
        self.assertEqual(data["blocks"]["x"]["value"][0]["children"], [{"text": ""}])


class TestTeaserGrid(unittest.TestCase):

    def test_logo_table_becomes_teaser_grids(self):
        def cell(n):
            return {"value": [{"type": "p", "children": [
                {"type": "link", "data": {"url": "../resolveuid/" + UID_B},
                 "children": [inline_img("{:032x}".format(n))]},
                {"text": "Agency {}".format(n)},
            ]}]}

        table = {"@type": "slateTable",
                 "table": {"rows": [{"cells": [cell(n) for n in range(1, 6)]}]}}
        convert = FakeConverter({"<table/>": [table]})
        result = transforms.transform_item(
            item(**{"@id": OLD + "/our-group", "id": "our-group", "text": {"data": "<table/>"}}),
            context(convert, teaser_page="/our-group",
                    resolve_uid={UID_B: TARGET + "/our-group/agency-link"}.get),
        )
        self.assertEqual(types(result), ["title", "group"])
        group = result["blocks"][result["blocks_layout"]["items"][1]]["data"]
        grids = [group["blocks"][uid] for uid in group["blocks_layout"]["items"]]
        self.assertEqual([len(g["columns"]) for g in grids], [4, 1])
        teaser = grids[0]["columns"][0]
        self.assertEqual(teaser["title"], "Agency 1")
        self.assertEqual(teaser["href"][0]["@id"], "/resolveuid/{:032x}".format(1))
        self.assertEqual(teaser["external_link"], "/en/epanet/our-group/agency-link")

    def test_other_pages_keep_their_tables(self):
        table = {"@type": "slateTable", "table": {"rows": []}}
        convert = FakeConverter({"<table/>": [table]})
        result = transforms.transform_item(
            item(text={"data": "<table/>"}), context(convert, teaser_page="/our-group")
        )
        self.assertEqual(types(result), ["title", "slateTable"])


class TestSubsite(unittest.TestCase):

    def front_page(self, **kwargs):
        html = "<p>Welcome</p>"
        convert = FakeConverter({html: [slate({"text": "Welcome"})]})
        return transforms.transform_item(
            item(**{"@id": OLD + "/front-page", "id": "front-page", "text": {"data": html}}),
            context(convert, site_default_page="front-page", **kwargs),
        )

    def test_site_default_page_updates_the_subsite(self):
        result = self.front_page()
        self.assertEqual(result["@id"], TARGET)
        self.assertEqual(result["id"], "epanet")
        self.assertEqual(result["@type"], "Subsite")
        self.assertEqual(result["parent"], {"@id": "https://www.example.org/en", "@type": "LRF"})
        # keep the subsite's own UID and workflow state
        self.assertNotIn("UID", result)
        self.assertNotIn("review_state", result)
        self.assertEqual(types(result), ["title", "slate"])

    def test_subsite_parent_uid(self):
        result = self.front_page(subsite_parent_uid=UID_A)
        self.assertEqual(result["parent"]["UID"], UID_A)

    def test_same_id_below_the_root_is_a_normal_page(self):
        result = transforms.transform_item(
            item(**{"@id": OLD + "/folder/front-page", "id": "front-page",
                    "parent": {"@id": OLD + "/folder", "@type": "Folder", "UID": UID_A}}),
            context(site_default_page="front-page"),
        )
        self.assertEqual(result["@type"], "Document")
