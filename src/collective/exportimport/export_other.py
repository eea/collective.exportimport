# -*- coding: utf-8 -*-
from Acquisition import aq_base
from App.config import getConfiguration
from collective.exportimport import _
from collective.exportimport import config
from collective.exportimport.export_content import ExportContent
from OFS.interfaces import IOrderedContainer
from operator import itemgetter
from plone import api
from plone.app.discussion.interfaces import IConversation
from plone.app.portlets.interfaces import IPortletTypeInterface
from plone.app.redirector.interfaces import IRedirectionStorage
from plone.app.textfield.value import RichTextValue
from plone.app.uuid.utils import uuidToObject
from plone.portlets.constants import CONTENT_TYPE_CATEGORY
from plone.portlets.constants import CONTEXT_CATEGORY
from plone.portlets.constants import GROUP_CATEGORY
from plone.portlets.constants import USER_CATEGORY
from plone.portlets.interfaces import ILocalPortletAssignmentManager
from plone.portlets.interfaces import IPortletAssignmentMapping
from plone.portlets.interfaces import IPortletAssignmentSettings
from plone.portlets.interfaces import IPortletManager
from plone.restapi.interfaces import ISerializeToJson
from plone.restapi.serializer.converters import json_compatible
from plone.uuid.interfaces import IUUID
from Products.CMFCore.interfaces import IContentish
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone.interfaces import IPloneSiteRoot
from Products.CMFPlone.utils import isExpired
from Products.Five import BrowserView
from zope.component import getAdapter
from zope.component import getMultiAdapter
from zope.component import getUtilitiesFor
from zope.component import getUtility
from zope.component import queryMultiAdapter, queryAdapter
from zope.component import queryUtility
from zope.interface import providedBy
from uuid import uuid4

import json
import logging
import os
import pkg_resources
import six
import base64
import uuid
import requests
import re
import sys
import copy

try:
    from eea.versions.interfaces import IGetVersions
except ImportError:
    IGetVersions = None

try:
    from eea.workflow.interfaces import IObjectArchived
except ImportError:
    IObjectArchived = None

try:
    from eea.geotags.interfaces import IGeoTags
except ImportError:
    IGeoTags = None

try:
    from eea.app.visualization.interfaces import IVisualizationConfig
except ImportError:
    IVisualizationConfig = None

try:
    from eea.reports.relations.interfaces import IGroupRelations
except ImportError:
    IGroupRelations = None

if (sys.getdefaultencoding() != 'utf-8'):
    reload(sys)
    sys.setdefaultencoding('utf-8')

SLATE_CONVERTER = "http://volto-convertor:8000/html"
BLOCKS_CONVERTER = "http://volto-convertor:8000/toblocks"

blocks = {}
blocks_layout = {}

try:
    pkg_resources.get_distribution("Products.Archetypes")
except pkg_resources.DistributionNotFound:
    HAS_AT = False
else:
    HAS_AT = True

try:
    pkg_resources.get_distribution("zc.relation")
except pkg_resources.DistributionNotFound:
    HAS_DX = False
else:
    HAS_DX = True

try:
    pkg_resources.get_distribution("z3c.relationfield")
except pkg_resources.DistributionNotFound:
    RelationValue = None
else:
    from z3c.relationfield import RelationValue

try:
    pam_version = pkg_resources.get_distribution("plone.app.multilingual")
    if pam_version.version < "2.0.0":
        IS_PAM_1 = True
    else:
        IS_PAM_1 = False
except pkg_resources.DistributionNotFound:
    IS_PAM_1 = False


logger = logging.getLogger(__name__)

PORTAL_PLACEHOLDER = "<Portal>"

with open(os.path.dirname(__file__) + '/resources/topics.json') as file:
    topics = json.load(file)

with open(os.path.dirname(__file__) + '/resources/geo_coverage.json') as file:
    geo_coverage = json.load(file)

with open(os.path.dirname(__file__) + '/resources/related_items.json') as file:
    related_items = json.load(file)

with open(os.path.dirname(__file__) + '/resources/images_ids.json') as file:
    with_images_ids = json.load(file)

with open(os.path.dirname(__file__) + '/resources/locations.json') as file:
    with_locations = json.load(file)

with open(os.path.dirname(__file__) + '/resources/missing-topics.json') as file:
    with_topics = json.load(file)


def make_uid():
    return str(uuid4())


def make_group_block(title, blocks):
    _blocks = {}
    _blocks_layout = {
        "items": []
    }

    blocks[:0] = [
        [make_uid(), {
            "@type": "slate",
            "value": [
                {
                    "type": "h3",
                    "children": [
                        {
                            "text": title
                        }
                    ]
                }
            ],
            "plaintext": title
        }]
    ]

    blocks.append([
        make_uid(), {
            "@type": "dividerBlock",
            "hidden": True,
            "spacing": "s",
            "styles": {}
        }
    ])

    for block in blocks:
        _blocks[block[0]] = block[1]
        _blocks_layout["items"].append(block[0])

    data = {
        "@type": "group",
        "data": {"blocks": _blocks, "blocks_layout": _blocks_layout},
        "styles": {},
        "title": title
    }
    return [make_uid(), data]


class BaseExport(BrowserView):
    """Just DRY"""

    def download(self, data):
        filename = self.request.form.get("filename")
        if not filename:
            filename = u"{}.json".format(self.__name__)
        if not data:
            msg = _(u"No data to export for {}").format(self.__name__)
            logger.info(msg)
            api.portal.show_message(msg, self.request)
            return self.request.response.redirect(self.request["ACTUAL_URL"])

        if self.download_to_server:
            directory = config.CENTRAL_DIRECTORY
            if directory:
                if not os.path.exists(directory):
                    os.makedirs(directory)
                    logger.info(
                        "Created central export/import directory %s",
                        directory)
            else:
                cfg = getConfiguration()
                directory = cfg.clienthome
            filepath = os.path.join(directory, filename)
            with open(filepath, "w") as f:
                json.dump(data, f, sort_keys=True, indent=4)
            msg = _(u"Exported to {}").format(filepath)
            logger.info(msg)
            api.portal.show_message(msg, self.request)
            return self.request.response.redirect(self.request["ACTUAL_URL"])

        else:
            data = json.dumps(data, sort_keys=True, indent=4)
            data = safe_bytes(data)
            response = self.request.response
            response.setHeader("content-type", "application/json")
            response.setHeader("content-length", len(data))
            response.setHeader(
                "content-disposition",
                'attachment; filename="{0}"'.format(filename),
            )
            return response.write(data)


class ExportRelations(BaseExport):
    """Export all relations"""

    def __call__(
            self, download_to_server=False, debug=False,
            include_linkintegrity=False):
        self.title = _(u"Export relations")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()
        logger.info(u"Exporting relations...")
        data = self.get_all_references(debug, include_linkintegrity)
        logger.info(u"Exported %s relations", len(data))
        self.download(data)

    def get_all_references(self, debug=False, include_linkintegrity=False):
        results = []

        if HAS_AT:
            from Products.Archetypes.config import REFERENCE_CATALOG

            # Archetypes
            # Get all data from the reference_catalog if it exists
            reference_catalog = getToolByName(
                self.context, REFERENCE_CATALOG, None)
            if reference_catalog is not None:
                ref_catalog = reference_catalog._catalog
                for rid in ref_catalog.data:
                    rel = ref_catalog[rid]
                    if (
                        not include_linkintegrity
                        and rel.relationship == "isReferencing"
                    ):
                        continue
                    source = uuidToObject(rel.sourceUID)
                    target = uuidToObject(rel.targetUID)
                    if not source or not target:
                        continue
                    item = {
                        "from_uuid": rel.sourceUID,
                        "to_uuid": rel.targetUID,
                        "relationship": rel.relationship,
                    }
                    if debug:
                        item["from_path"] = source.absolute_url_path()
                        item["to_path"] = target.absolute_url_path()
                    item = self.reference_hook(item)
                    if item is None:
                        continue
                    results.append(item)

        if HAS_DX:
            from zc.relation.interfaces import ICatalog

            # Dexterity
            # Get all data from zc.relation (relation_catalog)
            relation_catalog = queryUtility(ICatalog)
            if relation_catalog:
                portal_catalog = getToolByName(self.context, "portal_catalog")
                for rel in relation_catalog.findRelations():
                    if (
                        not include_linkintegrity
                        and rel.from_attribute == "isReferencing"
                    ):
                        continue
                    try:
                        rel_from_path_and_rel_to_path = rel.from_path and rel.to_path
                    except ValueError:
                        logger.exception(
                            "Cannot export relation %s, skipping", rel)
                        continue
                    if rel_from_path_and_rel_to_path:
                        from_brain = portal_catalog(
                            path=dict(query=rel.from_path, depth=0)
                        )
                        to_brain = portal_catalog(
                            path=dict(query=rel.to_path, depth=0))
                        if len(from_brain) > 0 and len(to_brain) > 0:
                            item = {
                                "from_uuid": from_brain[0].UID,
                                "to_uuid": to_brain[0].UID,
                                "relationship": rel.from_attribute,
                            }
                            if debug:
                                item["from_path"] = from_brain[0].getPath()
                                item["to_path"] = to_brain[0].getPath()
                            item = self.reference_hook(item)
                            if item is None:
                                continue
                            results.append(item)

        return results

    def reference_hook(self, item):
        return item


class ExportMembers(BaseExport):
    """Export plone groups and members"""

    MEMBER_PROPERTIES = [
        "email",
        "listed",
        "login_time",
        "last_login_time",
        "fullname",
        "home_page",
        "location",
        "description",
    ]

    AUTO_GROUPS = ["AuthenticatedUsers"]
    AUTO_ROLES = ["Authenticated"]

    def __init__(self, context, request):
        super(ExportMembers, self).__init__(context, request)
        self.pms = api.portal.get_tool("portal_membership")
        self.title = _(u"Export members, groups and roles")
        self.group_roles = {}

    def __call__(self, download_to_server=False):
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        data = {}
        logger.info(u"Exporting groups and users...")
        data["groups"] = self.export_groups()
        data["members"] = [i for i in self.export_members()]
        msg = u"Exported {} groups and {} members".format(
            len(data["groups"]), len(data["members"])
        )
        logger.info(msg)
        self.download(data)

    def export_groups(self):
        data = []
        for group in api.group.get_groups():
            if group.id in self.AUTO_GROUPS:
                continue
            item = {"groupid": group.id}
            item["roles"] = [i for i in api.group.get_roles(
                group=group) if i not in self.AUTO_ROLES]
            item["groups"] = [
                i.id
                for i in api.group.get_groups(user=group)
                if i.id not in self.AUTO_GROUPS
            ]
            for prop in group.getProperties():
                item[prop] = json_compatible(group.getProperty(prop))
            # export all principals (incl. groups and ldap-users)
            plone_group = group.getGroup()
            item["principals"] = plone_group.getMemberIds()
            data.append(item)
        return data

    def export_members(self):
        pg = api.portal.get_tool("portal_groups")
        acl = api.portal.get_tool("acl_users")
        gids = set([item["id"] for item in acl.searchGroups()])
        for gid in gids:
            self.group_roles[gid] = pg.getGroupById(gid).getRoles()
        return self._getUsersInfos()

    def _getUsersInfos(self):
        """Generator filled with the members data."""
        acl = api.portal.get_tool("acl_users")
        for user in acl.searchUsers():
            if not user["pluginid"] == "mutable_properties":
                yield self._getUserData(user["userid"])

    def _getUserPassword(self, userId):
        acl = api.portal.get_tool("acl_users")
        users = acl.source_users
        passwords = users._user_passwords
        password = passwords.get(userId, "")
        if six.PY3 and isinstance(password, bytes):
            # bytes are not json serializable.
            # Happens at least in the tests.
            password = password.decode("utf-8")
        return password

    def _getUserData(self, userId):
        member = self.pms.getMemberById(userId)
        groups = []
        group_ids = [i for i in member.getGroups()
                     if i not in self.AUTO_GROUPS]
        # Drop groups in which the user is a transitive member
        for group_id in group_ids:
            group = api.group.get(group_id)
            plone_group = group.getGroup()
            if userId in plone_group.getMemberIds():
                groups.append(group_id)
        group_roles = []
        for gid in groups:
            group_roles.extend(self.group_roles.get(gid, []))
        roles = [
            role
            for role in member.getRoles()
            if role not in group_roles and role not in self.AUTO_ROLES
        ]
        # userid, password, roles
        props = {
            # TODO: We should have userid and username (login name).
            "username": userId,
            "password": self._getUserPassword(userId),
            "roles": json_compatible(roles),
            "groups": json_compatible(groups),
        }
        if member is not None:
            # TODO: Add support for any additional member-properties.
            for prop in self.MEMBER_PROPERTIES:
                props[prop] = json_compatible(member.getProperty(prop))
        return props


class ExportTranslations(BaseExport):

    DROP_PATH = []

    def __call__(self, download_to_server=False):
        self.title = _(u"Export translations")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting translations...")
        data = self.all_translations()
        logger.info(u"Exported %s groups of translations", len(data))
        self.download(data)

    def all_translations(self):  # noqa: C901
        results = []

        # Archetypes with LinguaPlone
        if HAS_AT:
            try:
                pkg_resources.get_distribution("Products.LinguaPlone")
            except pkg_resources.DistributionNotFound:
                HAS_LINGUAPLONE = False
            else:
                HAS_LINGUAPLONE = True

            if HAS_LINGUAPLONE:
                from Products.Archetypes.config import REFERENCE_CATALOG

                # Get all data from the reference_catalog if it exists
                reference_catalog = getToolByName(
                    self.context, REFERENCE_CATALOG, None)
                if reference_catalog is not None:
                    for ref in reference_catalog(relationship="translationOf"):
                        source = api.content.get(UID=ref.sourceUID)
                        if not source:
                            continue
                        item = {}
                        translations = source.getTranslations()
                        for lang in translations:
                            if not lang:
                                continue
                            uuid = IUUID(translations[lang][0], None)
                            if uuid:
                                item[lang] = uuid
                        if len(item) < 2:
                            continue
                        results.append(item)

        # Archetypes and Dexterity with plone.app.multilingual
        portal_catalog = api.portal.get_tool("portal_catalog")
        if "TranslationGroup" not in portal_catalog.indexes():
            logger.debug(
                u"No index TranslationGroup (p.a.multilingual not installed)")
            return results

        for uid in portal_catalog.uniqueValuesFor("TranslationGroup"):
            query = {"TranslationGroup": uid}
            if IS_PAM_1:
                query.update({"Language": "all"})
            brains = portal_catalog(query)

            if len(brains) < 2:
                # logger.info(u'Skipping...{} {}'.format(uid, brains))
                continue
            item = {}
            skip = False
            for brain in brains:
                for path in self.DROP_PATH:
                    if path in brain.getPath():
                        skip = True
                if not skip and brain.Language in item:
                    logger.info(
                        u"Duplicate language for {}: {}".format(
                            uid, [i.getPath() for i in brains]
                        )
                    )
                item[brain.Language] = brain.UID

            if not skip:
                results.append(item)
        return results


class ExportLocalRoles(BaseExport):
    """Export all local roles"""

    def __call__(self, download_to_server=False):
        self.title = _(u"Export local roles")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting local roles...")
        data = self.all_localroles()
        logger.info(u"Exported local roles for %s items", len(data))
        self.download(data)

    def all_localroles(self):
        self.results = []

        portal = api.portal.get()
        portal.ZopeFindAndApply(portal, search_sub=True,
                                apply_func=self.get_localroles)

        self.get_root_localroles()

        return self.results

    def get_localroles(self, obj, path):
        uid = IUUID(obj, None)
        if not uid:
            return
        self._get_localroles(obj, uid)

    def _get_localroles(self, obj, uid):
        localroles = None
        block = None
        obj = aq_base(obj)
        if getattr(obj, "__ac_local_roles__", None) is not None:
            localroles = obj.__ac_local_roles__
        if getattr(obj, "__ac_local_roles_block__", False):
            block = obj.__ac_local_roles_block__
        if localroles or block:
            item = {"uuid": uid}
            if localroles:
                item["localroles"] = localroles
            if block:
                item["block"] = 1
            item = self.item_hook(item)
            if item is None:
                return
            self.results.append(item)

    def get_root_localroles(self):
        site = api.portal.get()
        self._get_localroles(site, PORTAL_PLACEHOLDER)

    def item_hook(self, item):
        return item


class ExportOrdering(BaseExport):
    """Export all local roles"""

    def __call__(self, download_to_server=False):
        self.title = _(u"Export ordering")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting positions in parent...")
        data = self.all_orders()
        logger.info(u"Exported %s positions in parent", len(data))
        self.download(data)

    def all_orders(self):
        results = []

        def get_position_in_parent(obj, path):
            uid = IUUID(obj, None)
            if not uid:
                return
            parent = obj.__parent__
            ordered = IOrderedContainer(parent, None)
            if ordered is not None:
                order = ordered.getObjectPosition(obj.getId())
                if order is not None:
                    results.append({"uuid": uid, "order": order})
            return

        portal = api.portal.get()
        portal.ZopeFindAndApply(
            portal, search_sub=True, apply_func=get_position_in_parent
        )
        return sorted(results, key=itemgetter("order"))


class ExportDefaultPages(BaseExport):
    """Export all default_page settings."""

    def __call__(self, download_to_server=False):
        self.title = _(u"Export default pages")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting default pages...")
        data = self.all_default_pages()
        logger.info(u"Exported %s default pages", len(data))
        self.download(data)

    def all_default_pages(self):
        results = []
        catalog = api.portal.get_tool("portal_catalog")
        for brain in catalog.unrestrictedSearchResults(
            is_folderish=True, sort_on="path"
        ):
            try:
                obj = brain.getObject()
            except Exception:
                logger.info(u"Error getting obj for %s",
                            brain.getURL(), exc_info=True)
                continue
            if obj is None:
                logger.error(u"brain.getObject() is None %s", brain.getPath())
                continue
            if IPloneSiteRoot.providedBy(obj):
                # Site root is handled below (in Plone 6 it is returned by a catalog search)
                continue

            try:
                data = self.get_default_page_info(obj)
            except Exception:
                logger.info(
                    u"Error exporting default_page for %s",
                    obj.absolute_url(),
                    exc_info=True,
                )
                continue

            if data:
                results.append(data)

        # handle portal
        portal = api.portal.get()
        try:
            data = self.get_default_page_info(portal)
            if data:
                data["uuid"] = config.SITE_ROOT
                results.append(data)
        except Exception:
            logger.info(u"Error exporting default_page for portal",
                        exc_info=True)

        return results

    def get_default_page_info(self, obj):
        uid = IUUID(obj, None)

        # We use a simplified method to only get index_html
        # and the property default_page on the object.
        # We don't care about other cases
        # 1. obj is folderish, check for a index_html in it
        if "index_html" in obj:
            default_page = "index_html"
        else:
            # 2. Check attribute 'default_page'
            default_page = getattr(aq_base(obj), "default_page", [])

        if default_page and default_page in obj:
            default_page_obj = obj.get(default_page)
            if default_page_obj:
                default_page_uid = IUUID(default_page_obj, None)
                return {
                    "uuid": uid,
                    "default_page": default_page,
                    "default_page_uuid": default_page_uid,
                }


class ExportDiscussion(BaseExport):
    def __call__(self, download_to_server=False):
        self.title = _(u"Export comments")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting discussions...")
        data = self.all_discussions()
        logger.info(u"Exported %s discussions", len(data))
        self.download(data)

    def all_discussions(self):
        results = []
        for brain in api.content.find(
            object_provides=IContentish.__identifier__,
            sort_on="path",
            context=self.context,
        ):
            try:
                obj = brain.getObject()
                if obj is None:
                    logger.error(u"brain.getObject() is None %s",
                                 brain.getPath())
                    continue
                conversation = IConversation(obj, None)
                if not conversation:
                    continue
                serializer = getMultiAdapter(
                    (conversation, self.request), ISerializeToJson
                )
                output = serializer()
                if output:
                    results.append(
                        {"uuid": IUUID(obj),
                         "conversation": output})
            except Exception:
                logger.info("Error exporting comments for %s",
                            brain.getURL(), exc_info=True)
                continue
        return results


class ExportPortlets(BaseExport):
    def __call__(self, download_to_server=False):
        self.title = _(u"Export portlets")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting portlets...")
        data = self.all_portlets()
        logger.info(u"Exported info for %s items with portlets", len(data))
        self.download(data)

    def all_portlets(self):
        self.results = []
        portal = api.portal.get()
        portal.ZopeFindAndApply(
            self.context, search_sub=True, apply_func=self.get_portlets
        )
        self.get_root_portlets()
        return self.results

    def get_portlets(self, obj, path):
        uid = IUUID(obj, None)
        if not uid:
            return
        self._get_portlets(obj, uid)

    def _get_portlets(self, obj, uid):
        portlets = export_local_portlets(obj)
        blacklist = export_portlets_blacklist(obj)
        portlets = self.local_portlets_hook(portlets)
        blacklist = self.portlets_blacklist_hook(blacklist)
        obj_results = {}
        if portlets:
            obj_results["portlets"] = portlets
        if blacklist:
            obj_results["blacklist_status"] = blacklist
        if obj_results:
            obj_results["@id"] = obj.absolute_url()
            obj_results["uuid"] = uid
            self.results.append(obj_results)
        return

    def get_root_portlets(self):
        site = api.portal.get()
        self._get_portlets(site, PORTAL_PLACEHOLDER)
        return

    def local_portlets_hook(self, portlets):
        return portlets

    def portlets_blacklist_hook(self, blacklist):
        return blacklist


def export_local_portlets(obj):
    """Serialize portlets for one content object
    Code mostly taken from https://github.com/plone/plone.restapi/pull/669
    """
    portlets_schemata = {
        iface: name for name, iface in getUtilitiesFor(IPortletTypeInterface)
    }
    items = {}
    for manager_name, manager in getUtilitiesFor(IPortletManager):
        mapping = queryMultiAdapter((obj, manager), IPortletAssignmentMapping)
        if mapping is None:
            continue
        mapping = mapping.__of__(obj)
        for name, assignment in mapping.items():
            portlet_type = None
            schema = None
            for schema in providedBy(assignment).flattened():
                portlet_type = portlets_schemata.get(schema, None)
                if portlet_type is not None:
                    break
            if portlet_type is None:
                continue
            assignment = assignment.__of__(mapping)
            settings = IPortletAssignmentSettings(assignment)
            if manager_name not in items:
                items[manager_name] = []
            values = {}
            for name in schema.names(all=True):
                value = getattr(assignment, name, None)
                if RelationValue is not None and isinstance(
                        value, RelationValue):
                    value = value.to_object.UID()
                elif isinstance(value, RichTextValue):
                    value = {
                        "data": json_compatible(value.raw),
                        "content-type": json_compatible(value.mimeType),
                        "encoding": json_compatible(value.encoding),
                    }
                value = json_compatible(value)
                values[name] = value
            items[manager_name].append(
                {
                    "type": portlet_type,
                    "visible": settings.get("visible", True),
                    "assignment": values,
                }
            )
    return items


def export_portlets_blacklist(obj):
    results = []
    for manager_name, manager in getUtilitiesFor(IPortletManager):
        assignable = queryMultiAdapter(
            (obj, manager),
            ILocalPortletAssignmentManager)
        if assignable is None:
            continue
        for category in (
            USER_CATEGORY,
            GROUP_CATEGORY,
            CONTENT_TYPE_CATEGORY,
            CONTEXT_CATEGORY,
        ):
            obj_results = {}
            status = assignable.getBlacklistStatus(category)
            if status is True:
                obj_results["status"] = u"block"
            elif status is False:
                obj_results["status"] = u"show"

            if obj_results:
                obj_results["manager"] = manager_name
                obj_results["category"] = category
                results.append(obj_results)
    return results


def safe_bytes(value, encoding="utf-8"):
    """Convert text to bytes of the specified encoding."""
    if isinstance(value, six.text_type):
        value = value.encode(encoding)
    return value


def export_plone_redirects():
    """Plone generates redirects after moving an item

    Visiting myfolder/myitem will automatically redirect to otherfolder/myitem
    after item got moved from myfolder/myitem to otherfolder/myitem
    """

    storage = getUtility(IRedirectionStorage)
    redirects = {}
    for key, value in storage._paths.items():
        if isinstance(value, tuple) and len(value) == 3:
            value = value[0]
        redirects[key] = value

    return redirects


class ExportRedirects(BaseExport):
    def __call__(self, download_to_server=False):
        self.title = _(u"Export redirects")
        self.download_to_server = download_to_server
        if not self.request.form.get("form.submitted", False):
            return self.index()

        logger.info(u"Exporting redirects...")
        data = export_plone_redirects()
        logger.info(u"Exported %s redirects", len(data))
        self.download(data)


def findBlockPaths(blocks, field='@type', value='', paths=None):
    if paths is None:
        paths = []
    ok = False
    for blockId in blocks:
        block = blocks[blockId]
        if block.get(field) == value:
            paths.append(blockId)
            ok = True
            break
        childrenBlocks = None
        if block.get("data", {}).get("blocks", {}):
            childrenBlocks = block.get("data", {}).get("blocks", {})
            nestedPaths = [blockId, "data", "blocks"]
        elif block.get("blocks", {}):
            childrenBlocks = block.get("blocks", {})
            nestedPaths = [blockId, "blocks"]
        if childrenBlocks:
            [p, o] = findBlockPaths(childrenBlocks, field, value, nestedPaths)
            if o:
                paths.extend(p)
                ok = True
                break

    if ok:
        return [paths, True]

    return [[], False]


def getBlockByPaths(blocks, paths):
    block = blocks
    for path in paths:
        block = block.get(path, {})
    return block


def updateBlockByPaths(blocks, paths, data=None):
    if data is None:
        data = {}
    # Traverse the dictionary up to the second-to-last key
    value = blocks
    for key in paths[:-1]:
        value = value[key]
    # Update the value for the last key, keeping the old value
    if isinstance(value[paths[-1]], dict) and isinstance(data, dict):
        value[paths[-1]].update(data)
    else:
        value[paths[-1]] = data
    # if "@marker" in value[paths[-1]]:
    #     del value[paths[-1]]["@marker"]


def getBlock(blocks, field="@type", value=""):
    [paths, found] = findBlockPaths(blocks, field, value)
    if found:
        return getBlockByPaths(blocks, paths)
    return None


def updateBlock(blocks, field="@type", value="", data=None):
    if data is None:
        data = {}
    [paths, found] = findBlockPaths(blocks, field, value)
    if found:
        updateBlockByPaths(blocks, paths, data)
    return blocks


def appendBlock(blocks, field="@type", value="", id="", data=None):
    if data is None:
        data = {}
    [paths, found] = findBlockPaths(blocks, field, value)

    if not found:
        return

    block = getBlockByPaths(blocks, paths)

    d_blocks = block.get("data", {}).get("blocks", None)
    nd_blocks = block.get("blocks", None)
    d_blocks_ids = block.get(
        "data", {}).get(
        "blocks_layout", {}).get(
        "items", None)
    nd_blocks_ids = block.get("blocks_layout", {}).get("items", "None")

    childrenBlocks = d_blocks if d_blocks is not None else nd_blocks
    childrenIds = d_blocks_ids if d_blocks_ids is not None else nd_blocks_ids

    if d_blocks is not None and d_blocks_ids is not None:
        childrenBlocks[id] = data
        childrenIds.append(id)
        updateBlockByPaths(blocks, paths, {
            "data": {
                "blocks": childrenBlocks,
                "blocks_layout": {
                    "items": childrenIds
                }
            }
        })
    elif nd_blocks is not None and nd_blocks_ids is not None:
        childrenBlocks[id] = data
        childrenIds.append(id)
        updateBlockByPaths(blocks, paths, {
            "blocks": childrenBlocks,
            "blocks_layout": {
                "items": childrenIds
            }
        })

    return blocks


class ExportEEAContent(ExportContent):
    QUERY = {}
    PORTAL_TYPE = []
    DISSALLOWED_FIELDS = [
        "arcgis_url",
        "body",  # handled by migrate_more_info
        "constrainTypesMode",
        "coverImage",
        # "contact",  # handled by migrate_more_info
        "dataLink",
        # "dataOwner",  # handled by migrate_more_info
        "dataSource",
        "dataTitle",
        "dataWarning",
        "disableProgressTrailViewlet",
        "eeaManagementPlan",  # handled by migrate_more_info
        "external",
        "externalRelations",  # handled by migrate_more_info
        "forcedisableautolinks",
        "figureType",
        "geographicCoverage",
        "inheritedprovenance",
        "introduction",  # handled by migrate_introduction
        "image",  # handled by migrate_image
        "layout",
        "location",  # handled by migrate_geo_coverage
        "methodology",  # handled by migrate_more_info
        "moreInfo",  # handled by migrate_more_info
        "pdfMaxBreadth",
        "pdfMaxDepth",
        "pdfMaxItems",
        "pdfStatic",
        "pdfTheme",
        "provenances",  # handled by migrate_data_provenance
        "processor",  # handled by migrate_more_info
        "spreadsheet",
        "quickUpload",
        "temporalCoverage",  # handled by migrate_temporal_coverage
        "themes",  # handled by migrate_topics
        "tocExclude",
        "tocdepth",
        "units",  # handled by migrate_more_info
        "workflow_history",
        "@components",
        "next_item",
        "prev_item",
        "management_plan",
        "@components",
        "items",
        "next_item"
    ]
    MIGRATE_MORE_INFO = True

    type = None
    blocks = None
    blocks_layout = None
    catalog = None

    images_ids = with_images_ids
    locations = with_locations
    topics = with_topics
    parsed_ids = {}

    folder_path = "/www/en/analysis/maps-and-charts"

    def update(self):
        """Use this to override stuff before the export starts
        (e.g. force a specific language in the request)."""
        self.portal_type = self.PORTAL_TYPE

    def load_blocks(self, item):
        if not self.type:
            return item
        if self.type in blocks:
            item["blocks"] = copy.deepcopy(blocks[self.type])
        else:
            try:
                with open(os.path.dirname(__file__) + '/resources/%s/blocks.json' % self.type) as file:
                    item["blocks"] = json.load(file)
                    blocks[self.type] = copy.deepcopy(item["blocks"])
            except Exception:
                pass
        if self.type in blocks_layout:
            item["blocks_layout"] = copy.deepcopy(blocks_layout[self.type])
        else:
            try:
                with open(os.path.dirname(__file__) + '/resources/%s/blocks_layout.json' % self.type) as file:
                    item["blocks_layout"] = json.load(file)
                    blocks_layout[self.type] = copy.deepcopy(
                        item["blocks_layout"])
            except Exception:
                pass
        return item

    def getOrganisationName(self, url):
        """ Return an organisation based on its URL """
        if not url:
            return None

        brains = self.catalog.searchResults({'portal_type': 'Organisation',
                                             'getUrl': url})
        if brains:
            return brains[0]
        return None

    def getImage(self, file):
        if not file or file.get("content-type") != 'image/svg+xml':
            return file
        data = file.get("data", None)
        if not data:
            return file
        data = base64.b64decode(data)

        if data.find('xmlns') > -1:
            return file

        data = data.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"')

        file["data"] = base64.b64encode(data)

        return file

    def global_dict_hook(self, item, obj):

        self.catalog = getToolByName(self, "portal_catalog")

        item = json.dumps(item).replace('\\r\\n', '\\n')

        # Regex pattern to match resolveuid and extract the ID
        pattern = re.compile(
            r'src=\"resolveuid\/([a-zA-Z0-9]{32})|src=\\"resolveuid\/([a-zA-Z0-9]{32})')

        # Find all matches
        matches = pattern.findall(item)

        # Save all found ids
        for match in matches:
            self.images_ids.append(match[1])

        # Regex pattern to match hrefs starting with ./ or ../
        pattern1 = re.compile(r'href=\"(\.\/|\.\.\/)+([^"]*)')
        pattern2 = re.compile(r'href=\\"(\.\/|\.\.\/)+([^"]*)')

        # Function to replace the matched pattern
        def replace_href(match):
            # Extract the path after the ./ or ../
            path = match.group(2)
            # Return the replacement string
            return 'href=\\"https://www.eea.europa.eu/%s' % path

        item = pattern1.sub(replace_href, item)
        item = pattern2.sub(replace_href, item)

        # Regex pattern to match hrefs starting with resolveuid/
        pattern1 = re.compile(r'href=\"resolveuid\/([^"]+)')
        pattern2 = re.compile(r'href=\\"resolveuid\/([^"]+)')

        # Function to replace the matched pattern
        def replace_href(match):
            # Extract the unique part after resolveuid/
            unique_id = match.group(1)
            # Return the replacement string
            return 'href=\\"https://www.eea.europa.eu/resolveuid/%s' % unique_id

        # Use re.sub with the replacement function
        item = pattern1.sub(replace_href, item)
        item = pattern2.sub(replace_href, item)

        item = json.loads(item)

        if item["id"] in self.parsed_ids:
            parts = item["@id"].split('/')
            [parentId, id] = parts[-2:]
            # item["@id"] = '/'.join(parts[:-2]) + '/%s-%s' % (id, parentId)
            item["id"] = '%s-%s' % (id, parentId)
        else:
            self.parsed_ids[item["id"]] = True

        item["@id"] = "%s/%s" % (self.folder_path, item["id"])
        item["parent"]["@id"] = self.folder_path
        item["parent"]["UID"] = None
        item["original_content_type"] = item["@type"]

        if self.type:
            item["@type"] = self.type

        item = self.load_blocks(item)

        item["versionId"] = IGetVersions(
            obj).versionId if IGetVersions else None
        item["relatedItems_unmapped"] = []
        item["relatedItems_backward"] = []

        item = self.migrate_related_items(item, obj)
        item = self.migrate_image(item, 'image')
        item = self.migrate_temporal_coverage(item, "temporalCoverage")
        item = self.migrate_topics(item, "themes")
        item = self.migrate_data_provenance(item, "provenances")
        item = self.migrate_other_organisations(item)
        item = self.migrate_introduction(item, "introduction")
        item = self.migrate_geo_coverage(item, obj)
        item = self.migrate_more_info(item)

        if "rights" in item and item["rights"]:
            item["rights"] = item["rights"].replace("\n", " ")

        for field in self.DISSALLOWED_FIELDS:
            if field in item:
                del item[field]

        return item

    def migrate_related_items(self, item, obj):
        relatedItems = obj.getRelatedItems()

        for macro in obj.unrestrictedTraverse('@@eea.relations.macro').backward():
            if len(macro) < 2:
                continue
            for relatedItem in macro[1]:
                if IObjectArchived and IObjectArchived.providedBy(relatedItem):
                    continue
                if isExpired(relatedItem):
                    continue
                if IGetVersions and not IGetVersions(relatedItem).isLatest():
                    continue
                if api.content.get_state(
                        obj=relatedItem, default="unknown") != "published":
                    continue
                item["relatedItems_backward"].append(relatedItem.UID())

        if not relatedItems:
            return item

        if "data_provenance" not in item or not item["data_provenance"] or "data" not in item["data_provenance"]:
            item["data_provenance"] = {
                "data": []
            }

        for relatedItem in relatedItems:
            if IObjectArchived and IObjectArchived.providedBy(relatedItem):
                continue
            if isExpired(relatedItem):
                continue
            if IGetVersions and not IGetVersions(relatedItem).isLatest():
                continue
            if api.content.get_state(
                    obj=relatedItem, default="unknown") != "published":
                continue
            ok = True
            data = {
                "@id": str(uuid.uuid4()),
                "title": relatedItem.Title(),
            }
            if relatedItem.meta_type not in ['Data', 'ExternalDataSpec']:
                item["relatedItems_unmapped"].append(relatedItem.UID())
                continue
            if relatedItem.meta_type == 'Data':
                versionId = IGetVersions(relatedItem).versionId
                if versionId not in related_items:
                    print("related item %s not found" % versionId)
                    continue
                data["link"] = "/en/datahub/datahubitem-view/%s" % related_items[versionId]
            for data_provenance in item["data_provenance"]["data"]:
                if data_provenance["title"] == relatedItem.Title():
                    ok = False
                    break
            if not ok:
                continue
            item["data_provenance"]["data"].append(data)
        return item

    def migrate_image(self, item, field):
        if field in item:
            item["preview_image"] = self.getImage(item[field])
        return item

    def migrate_temporal_coverage(self, item, field):
        if field in item:
            temporals = item[field]
            item["temporal_coverage"] = {
                "temporal": []
            }
            for temporal in temporals:
                if not temporal > 0:
                    continue
                item["temporal_coverage"]["temporal"].append({
                    "label": temporal,
                    "value": temporal
                })
        return item

    def migrate_topics(self, item, field):
        if field in item:
            item["topics"] = []
            for topic in item[field]:
                if topic in topics:
                    item["topics"].append(topics[topic])
                # else:
                #     self.topics.append(topic)
            # Store missing topic in a list
        return item

    def migrate_data_provenance(self, item, field):
        if "data_provenance" not in item or not item["data_provenance"] or "data" not in item["data_provenance"]:
            item["data_provenance"] = {
                "data": []
            }

        if field in item:
            for provenance in item[field]:
                ok = True

                for data_provenance in item["data_provenance"]["data"]:
                    if data_provenance["title"] == provenance.get(
                            "title", None):
                        ok = False
                        break

                if not ok:
                    continue

                organisation = self.getOrganisationName(
                    provenance.get("owner", None))

                item["data_provenance"]["data"].append({
                    "@id": str(uuid.uuid4()),
                    "link": provenance.get("link", None),
                    "title": provenance.get("title", None),
                    "organisation": organisation.Title
                    if organisation else provenance.get("owner", None), })
        return item

    def migrate_other_organisations(self, item):
        item["other_organisations"] = []

        if 'processor' in item and isinstance(
                item['processor'],
                list) and len(
                item['processor']):
            for url in item['processor']:
                organisation = self.getOrganisationName(url)
                title = organisation.Title if organisation else url
                item["other_organisations"].append(title)

        return item

    def migrate_introduction(self, item, field):
        if field in item:
            item["text"] = item.get(field, None)
            if item["text"]:
                item["text"]["encoding"] = "utf8"

        return item

    def migrate_geo_coverage(self, item, obj):

        item["geo_coverage"] = {
            "geolocation": []
        }

        geo = getAdapter(obj, IGeoTags) if IGeoTags else None

        if not geo:
            return item

        for feature in geo.getFeatures():
            other = feature['properties'].get('other', {})
            title = feature["properties"]["title"]
            if other.has_key('geonameId'):
                id = 'geo-' + str(other['geonameId'])
                item["geo_coverage"]["geolocation"].append(
                    {
                        "label": title,
                        "value": id,
                    })
            elif title in geo_coverage:
                item["geo_coverage"]["geolocation"].append(geo_coverage[title])
            else:
                self.locations.append(title)
                logger.warn(u"No geonameId found for tag %s", title)

        return item

    def migrate_more_info(self, item):
        if not self.MIGRATE_MORE_INFO:
            return item

        if "blocks" not in item or not item.get("blocks"):
            return item

        blocks = []

        # Migrate "methodology" field
        html = self.get_html(item, 'methodology')
        if html:
            blocks.append(make_group_block(
                "Methodology", self.convert_to_blocks(html)))

        # Migrate "units" field
        html = self.get_html(item, 'units')
        if html:
            blocks.append(make_group_block(
                "Units", self.convert_to_blocks(html)))

        # Migrate "dataOwner" field
        # if 'dataOwner' in item and isinstance(
        #         item['dataOwner'],
        #         list) and len(
        #         item['dataOwner']):
        #     html = ''
        #     for url in item['dataOwner']:
        #         organisation = self.getOrganisationName(url)
        #         if not organisation:
        #             continue
        #         title = organisation.Title if organisation else url
        #         html += "<p><a href='%s' target='_blank'>%s</a></p>" % (
        #             url, title)
        #     if html:
        #         blocks.append(make_group_block(
        #             "Owners", self.convert_to_blocks(html)))

        # Migrate "processor" field
        # if 'processor' in item and isinstance(
        #         item['processor'],
        #         list) and len(
        #         item['processor']):
        #     html = ''
        #     for url in item['processor']:
        #         organisation = self.getOrganisationName(url)
        #         if not organisation:
        #             continue
        #         title = organisation.Title if organisation else url
        #         html += "<p><a href='%s' target='_blank'>%s</a></p>" % (
        #             url, title)
        #     if html:
        #         blocks.append(make_group_block(
        #             "Processors", self.convert_to_blocks(html)))

        # Migrate "eeaManagementPlan" field
        # if isinstance(
        #         item.get('eeaManagementPlan'),
        #         list):
        #     html = "year: %s, code: %s" % (
        #         item["eeaManagementPlan"][0]
        #         if len(item["eeaManagementPlan"]) > 0 else "",
        #         item["eeaManagementPlan"][1]
        #         if len(item["eeaManagementPlan"]) > 1 else "")
        # if html:
        #     blocks.append(make_group_block(
        #         "EEA management plan code", self.convert_to_blocks(html)))

        # Migrate "contact" field
        html = self.get_html(item, 'contact')
        if html:
            contacts = html.replace("\n\r", "\n").split("\n")
            html = ''
            for contact in contacts:
                if not contact:
                    continue
                html += "<p>%s</p>" % (contact)

            if html:
                c_blocks = {}
                c_blocks_layout = {
                    "items": []
                }
                for b in self.convert_to_blocks(html):
                    if b[1].get("@type") == 'slate' and not b[1].get("plaintext"):
                        continue
                    c_blocks[b[0]] = b[1]
                    c_blocks_layout["items"].append(b[0])

                if len(c_blocks_layout["items"]) > 0:
                    c_uid1 = make_uid()
                    c_uid2 = make_uid()
                    c_blocks[c_uid1] = {
                        "@type": "slate",
                        "value": [{"type": "h3", "children": [{"text": "Contact references at EEA"}]}],
                        "plaintext": "Contact references at EEA"
                    }
                    c_blocks[c_uid2] = {
                        "@type": "dividerBlock",
                        "hidden": True,
                        "spacing": "s",
                        "styles": {}
                    }
                    c_blocks_layout["items"].insert(0, c_uid1)
                    c_blocks_layout["items"].append(c_uid2)

                updateBlock(item["blocks"], "@marker", "contact_references_at_eea", {
                    "data": {
                        "blocks": c_blocks,
                        "blocks_layout": c_blocks_layout
                    }
                })

        # Migrate "externalRelations" field
        if 'externalRelations' in item and isinstance(
                item['externalRelations'],
                list) and len(
                item['externalRelations']):
            html = ''
            for url in item['externalRelations']:
                html += "<p><a href='%s' target='_blank'>%s</a></p>" % (
                    url, url)
            if html:
                blocks.append(make_group_block(
                    "External links, non EEA websites", self.convert_to_blocks(html)))

        # Migrate "moreInfo" field
        html = self.get_html(item, 'body') + self.get_html(item, 'moreInfo')
        if html:
            result = self.convert_to_blocks(html)
            if len(blocks) > 0:
                blocks.append(make_group_block(
                    "Additional information", result))
            else:
                [blocks.append(block) for block in result]

        for b in blocks:
            block_id = b[0]
            block = b[1]
            appendBlock(item["blocks"], "@marker",
                        "more_info_tab", block_id, block)
        appendBlock(
            item["blocks"],
            "@marker", "more_info_tab", make_uid(), {
                "@type": "slate",
                "value": [{"type": "p", "children": [{"text": ""}]}],
                "plaintext": ""
            })

        # for block_id in item["blocks"]:
        #     if block_id and item["blocks"][block_id].get('title') == 'Metadata section':
        #         tabs_block_id = item["blocks"][block_id]['data'][
        #             'blocks_layout']['items'][0]
        #         tabs_blocks = item["blocks"][block_id]['data']['blocks'][
        #             tabs_block_id]['data']['blocks']

        #         for _tab_block_id in tabs_blocks:
        #             if _tab_block_id and tabs_blocks[_tab_block_id].get(
        #                     'title') == 'More info':
        #                 for b in blocks:
        #                     _block_id = b[0]
        #                     _block = b[1]
        #                     item["blocks"][block_id]['data']['blocks'][
        #                         tabs_block_id]['data']['blocks'][
        #                         _tab_block_id]['blocks'][_block_id] = _block
        #                     item["blocks"][block_id]['data']['blocks'][tabs_block_id]['data']['blocks'][_tab_block_id]['blocks_layout']['items'].append(
        #                         _block_id)

        return item

    def get_html(self, item, field):
        value = item.get(field)
        if isinstance(value, basestring):
            return value
        if value and value.get("content-type") == 'text/html':
            return value.get("data", "")
        return ""

    def convert_to_blocks(self, text):
        data = {"html": text}
        headers = {"Content-type": "application/json",
                   "Accept": "application/json"}

        req = requests.post(
            BLOCKS_CONVERTER, data=json.dumps(data), headers=headers)
        if req.status_code != 200:
            import pdb
            pdb.set_trace()
            logger.debug(req.text)
            # raise ValueError

        blocks = req.json()["data"]
        return blocks

    def text_to_slate(self, text):
        data = {"html": text}
        headers = {"Content-type": "application/json",
                   "Accept": "application/json"}
        if not text:
            return [
                {
                    "type": "p",
                    "children": [
                        {
                            "text": ""
                        }
                    ]
                }
            ]
        res = requests.post(
            SLATE_CONVERTER, data=json.dumps(data), headers=headers)
        slate = res.json()["data"]
        return slate

    def finish(self):
        locations = list(set(self.locations))
        images_ids = list(set(self.images_ids))
        topics = list(set(self.topics))
        print("===> Locations <===")
        print(locations)
        f = open(os.path.dirname(__file__) + '/resources/locations.json', "w")
        f.write(json.dumps(locations))
        f.close()
        print("===> Images uids <===")
        print(images_ids)
        f = open(os.path.dirname(__file__) + '/resources/images_ids.json', "w")
        f.write(json.dumps(images_ids))
        f.close()
        print("===> Topics <===")
        print(topics)
        f = open(os.path.dirname(
            __file__) + '/resources/missing-topics.json', "w")
        f.write(json.dumps(topics))
        f.close()


class ExportInfographic(ExportEEAContent):
    QUERY = {
        "Infographic": {
            "review_state": "published",
        }
    }
    PORTAL_TYPE = ["Infographic"]
    type = "infographic"

    def global_dict_hook(self, item, obj):
        """Use this to modify or skip the serialized data.
        Return None if you want to skip this particular object.
        """
        if IObjectArchived.providedBy(obj):
            return None
        item = super(ExportInfographic, self).global_dict_hook(item, obj)

        return item


class ExportDashboard(ExportEEAContent):
    QUERY = {
        "Dashboard": {
            "review_state": "published",
        }
    }
    PORTAL_TYPE = ["Dashboard"]
    type = 'tableau_visualization'

    def global_dict_hook(self, item, obj):
        """Use this to modify or skip the serialized data.
        Return None if you want to skip this particular object.
        """
        item = super(ExportDashboard, self).global_dict_hook(item, obj)

        return item


class ExportGisMapApplication(ExportEEAContent):
    QUERY = {
        "GIS Application": {
            "review_state": "published",
        }
    }
    PORTAL_TYPE = ["GIS Application"]
    type = 'map_interactive'
    with_image_override = []

    def global_dict_hook(self, item, obj):
        """Use this to modify or skip the serialized data.
        Return None if you want to skip this particular object.
        """
        arcgis_url = item.get("arcgis_url", None)
        item["maps"] = {
            "dataprotection": {},
            "url": arcgis_url,
            "useScreenHeight": True
        }

        if 'appid' in arcgis_url and 'embed' not in arcgis_url:
            self.with_image_override.append(item.get("UID"))

        item = super(ExportGisMapApplication, self).global_dict_hook(item, obj)

        return item

    def finish(self):
        with_image_override = list(set(self.with_image_override))
        print("===> With image override <===")
        print(with_image_override)
        f = open(os.path.dirname(
            __file__) + '/resources/with-image-override.json', "w")
        f.write(json.dumps(with_image_override))
        f.close()
        return super(ExportGisMapApplication, self).finish()


class ExportDavizFigure(ExportEEAContent):
    QUERY = {
        "DavizVisualization": {
            "review_state": "published",
        }
    }
    PORTAL_TYPE = ["DavizVisualization"]
    type = 'chart_static'

    multipleCharts = 0

    def global_dict_hook(self, item, obj):
        """Use this to modify or skip the serialized data.
        Return None if you want to skip this particular object.
        """
        items = []
        images = []
        default_image = 0

        item = super(ExportDavizFigure, self).global_dict_hook(item, obj)

        accessor = queryAdapter(
            obj, IVisualizationConfig) if IVisualizationConfig else None

        chartsConfig = accessor.view("googlechart.googlecharts")[
            "chartsconfig"]

        charts = chartsConfig.get('charts', [])
        notes = chartsConfig.get('notes', [])

        for chart in charts:
            config = json.loads(chart["config"])
            id = chart.get("id")
            type = config.get('chartType', None)
            if type == 'Table':
                continue
            images.append({
                "id": chart.get("id"),
                "title": chart.get("name"),
            })
            cIndex = len(images) - 1
            if config.get("isDefaultVisualization", False):
                default_image = cIndex
            for note in notes:
                if id in note.get("charts", []):
                    images[cIndex]["note"] = note.get("text", "")

        if default_image > 0:
            tmp = images[0]
            images[0] = images[default_image]
            images[default_image] = tmp

        csv = queryMultiAdapter((obj, self.request), name='download.csv')

        if csv:
            csv = csv(
                attachment=False).encode('utf-8')
            item["file"] = {
                "data": base64.b64encode(csv),
                "filename": obj.getId() + '.csv',
                "content_type": "text/csv",
                "encoding": "base64"
            }

        if len(images) > 0 and images[0]:
            image = None
            imageObj = None
            imageId = images[0].get("id")
            if imageId:
                imageObj = obj.get(
                    imageId + '.svg') or obj.get(imageId + '.png')
            if imageObj:
                try:
                    serializer = getMultiAdapter(
                        (imageObj, self.request), ISerializeToJson)
                    image = serializer()
                except Exception:
                    print("Error getting image for {}".format(
                        item['@id'] + "-" + imageId))
            if image:
                newItem = item.copy()
                newItem["preview_image"] = self.getImage(
                    image.get("image", None) or image.get("file", None)
                )
                if newItem["preview_image"] and "filename" in newItem["preview_image"]:
                    newItem["preview_image"]["filename"] = image.get(
                        "id", None)
                # Get figure note
                if images[0].get("note"):
                    newItem["figure_notes"] = self.text_to_slate(
                        images[0].get("note"))
                items.append(newItem)

        if len(images) > 1:
            itemTitle = item.get("title", "")
            itemId = item.get("id", "")
            for index, img in enumerate(images[1:]):
                image = None
                imageObj = None
                imageId = img.get("id")
                if imageId:
                    imageObj = obj.get(
                        imageId + '.svg') or obj.get(imageId + '.png')
                if imageObj:
                    try:
                        serializer = getMultiAdapter(
                            (imageObj, self.request),
                            ISerializeToJson)
                        image = serializer()
                    except Exception:
                        print("Error getting image for {}".format(
                            item['@id'] + "-" + imageId))
                if image:
                    imageTitle = img.get('title', "")
                    newItem = item.copy()
                    newItem["@id"] = item["@id"] + "-" + imageId
                    newItem["id"] = itemId + "-" + imageId
                    newItem["UID"] = image.get("UID", None) or item.get(
                        "UID", None)
                    newItem["title"] = itemTitle + " - " + imageTitle
                    newItem["preview_image"] = self.getImage(
                        image.get("image", None) or image.get("file", None)
                    )
                    if newItem["preview_image"] and "filename" in newItem["preview_image"]:
                        newItem["preview_image"]["filename"] = image.get(
                            "id", None)
                    # Get figure note
                    if img.get("note"):
                        newItem["figure_notes"] = self.text_to_slate(
                            img.get("note"))
                    items.append(newItem)
            if len(items) >= 1:
                self.multipleCharts += 1
                for item in items:
                    item["relatedItems"] = [
                        _item["UID"]
                        for _item in items
                        if _item["@id"] != item["@id"]
                    ]
        return items if len(items) > 0 else item

    def finish(self):
        print("===> Exported %s daviz figures with multiple charts <===" %
              self.multipleCharts)
        return super(ExportDavizFigure, self).finish()


class ExportEEAFigure(ExportEEAContent):
    QUERY = {
        # "EEAFigure": {
        #     "review_state": "published",
        # }
    }
    PORTAL_TYPE = ["EEAFigure"]

    def global_dict_hook(self, item, obj):
        """Use this to modify or skip the serialized data.
        Return None if you want to skip this particular object.
        """
        figure_type = item.get("figureType", "")

        if figure_type == 'map':
            self.type = 'map_static'

        if figure_type == 'graph':
            self.type = 'chart_static'

        item = super(ExportEEAFigure, self).global_dict_hook(item, obj)

        figure = obj.unrestrictedTraverse(
            "@@getSingleEEAFigureFile").singlefigure()
        image = figure.unrestrictedTraverse("image_large") if figure else None
        imageB64 = base64.b64encode(image.__call__()) if image else None

        if imageB64:
            item["preview_image"] = {
                "encoding": "base64",
                "content-type": "image/png",
                "data": imageB64
            }

        portal_workflow = getToolByName(
            self.context, "portal_workflow", None)

        children = []

        print(item["UID"])
        for o in obj.contentItems():
            if o[1].meta_type != 'EEAFigureFile' and portal_workflow.getInfoFor(
                    o[1], 'review_state') != 'published':
                continue
            if IObjectArchived and IObjectArchived.providedBy(o[1]):
                continue
            if isExpired(o[1]):
                continue
            if IGetVersions and not IGetVersions(o[1]).isLatest():
                continue
            if o[1].getLanguage() != 'en':
                continue
            if o[1].meta_type not in ['EEAFigureFile', 'DataFileLink']:
                continue
            serializer = getMultiAdapter(
                (o[1], self.request), ISerializeToJson)
            child = serializer()
            if "relatedItems" in child:
                del child["relatedItems"]
            child["review_state"] = "published"
            child["@id"] = "%s/%s/%s" % (self.folder_path,
                                         item["id"], child["id"])
            child["parent"]["@id"] = item["@id"]
            child["parent"]["UID"] = item.get("UID")
            child["@type"] = 'File' if child.get("file") else 'Link'
            if child.get("category"):
                child["subjects"] = [child.get("category")]
            for field in self.DISSALLOWED_FIELDS:
                if field in child:
                    del child[field]
            children.append(child)

        if len(children) > 0:
            return [item] + children

        return item


class ExportReport(ExportEEAContent):
    # TODO: update @id and uid
    QUERY = {
        "Report": {
            "review_state": "published",
        }
    }
    PORTAL_TYPE = ["Report"]
    type = "report"
    statistics = {}
    data = {}

    MIGRATE_MORE_INFO = False

    def migrate_serial_title(self, item):
        serialTitle = item.get("serial_title")

        if not serialTitle:
            return item

        length = len(serialTitle)

        x1 = ''
        x2 = ''

        if length > 0:
            x1 = str(serialTitle[0])
        if length > 1:
            x2 = str(serialTitle[1])
        if length > 2:
            x2 += ('/' + str(serialTitle[2])) if serialTitle[2] else ''
        if length > 3:
            x2 += ('/' + str(serialTitle[3])) if serialTitle[3] else ''

        serialTitle = x1 + ' ' + x2 if x1 and x2 else x1

        updateBlock(item["blocks"],
                    "@marker", "serial_title_title",
                    {"subtitle": serialTitle})
        updateBlock(
            item["blocks"],
            "@marker", "serial_title_slate",
            {"plaintext": serialTitle,
             "value": self.text_to_slate(serialTitle)})

        return item

    def migrate_order_id_isbn(self, item):
        order_id = item.get("order_id")
        isbn = item.get("isbn")

        order_id_isbn = (
            (("EN PDF: " + order_id + " - ") if order_id else "")
            + (("ISBN: " + isbn) if isbn else "")) or ""

        updateBlock(item["blocks"],
                    "@marker", "order_id_isbn_slate",
                    {"plaintext": order_id_isbn,
                     "value": self.text_to_slate(order_id_isbn)})

        return item

    def global_dict_hook(self, item, obj):
        if len(getAdapter(obj, IGroupRelations).forward()) > 0:
            return None

        if obj.getDefaultPage():
            return None

        item = super(ExportReport, self).global_dict_hook(item, obj)

        file = {
            "@id": item["@id"] + "/%s" % item["id"],
            "@type": "File",
            "UID": str(uuid4()).replace('-', ''),
            "id": item["id"],
            "title": item["title"],
            "file": item["file"]
        }

        item = self.migrate_serial_title(item)
        item = self.migrate_order_id_isbn(item)

        updateBlock(item["blocks"],
                    "@marker", "file_call_to_action",
                    {"download": True, "href": file["@id"]})

        children = self.getChildren(obj)
        folderContents = self.getFolderContents(item, children)

        cover = None
        for i in folderContents:
            if i["id"] == 'cover':
                cover = i
                break
        if cover:
            item["preview_image"] = cover.get("image") or cover.get("file")

        return [item, file] + folderContents

    def getChildren(self, obj):
        objects = []

        portal_workflow = getToolByName(
            self.context, "portal_workflow", None)

        for o in obj.contentItems():
            if portal_workflow.getInfoFor(
                    o[1], 'review_state') != 'published':
                continue
            if IObjectArchived and IObjectArchived.providedBy(o[1]):
                continue
            if isExpired(o[1]):
                continue
            if IGetVersions and not IGetVersions(o[1]).isLatest():
                continue
            if o[1].getLanguage() != 'en':
                continue
            if o[1].meta_type not in ['Folder', 'ATBlob']:
                continue
            if o[1].meta_type != 'Folder':
                objects.append(o[1])
            else:
                objects.append(o[1])
                objects + self.getChildren(o[1])

        return objects

    def getFolderContents(self, item, objects):

        # content = {}

        for index, o in enumerate(objects):
            serializer = getMultiAdapter((o, self.request), ISerializeToJson)
            objects[index] = serializer()
            objType = objects[index]["@type"]
            # if objType not in content:
            #     content[objType] = 0
            # content[objType] += 1
            if objType == 'Folder':
                objects[index]["@type"] = 'Document'

        # self.data[item["@id"]] = content

        # keys = list(content.keys())

        # hasFiles = 'File' in keys
        # hasDocuments = 'Document' in keys
        # hasFolders = 'Folder' in keys
        # hasCollection = 'Collection' in keys
        # hasLink = 'Link' in keys
        # hasImages = 'Image' in keys

        # if hasDocuments and len(keys) == 1:
        #     self.statistics["Contains only 'Documents'"] = self.statistics.get(
        #         "Contains only 'Documents'", 0) + 1
        # elif hasFiles and len(keys) == 1:
        #     self.statistics["Contains only 'Files'"] = self.statistics.get(
        #         "Contains only 'Files'", 0) + 1
        # elif not hasFiles:
        #     self.statistics["Doesn't contains 'Files'"] = self.statistics.get(
        #         "Doesn't contains 'Files'", 0) + 1
        # elif hasFiles and hasDocuments and len(keys) == 2:
        #     self.statistics["Contains only 'Files' and 'Documents'"] = self.statistics.get(
        #         "Contains only 'Files' and 'Documents'", 0) + 1
        # elif hasFiles and len(keys) > 1 and (hasDocuments or hasFolders or hasCollection or hasLink or hasImages):
        #     self.statistics["Contains 'Files' but also documents, folders, collections, links or images"] = self.statistics.get(
        #         "Contains 'Files' but also documents, folders, collections, links or images", 0) + 1
        # elif len(keys) == 0:
        #     self.statistics["Doesn't contain any content"] = self.statistics.get(
        #         "Doesn't contain any content", 0) + 1
        # elif True:
        #     self.statistics["Exception"] = self.statistics.get(
        #         "Exception", 0) + 1

        return objects

    # def finish(self):
    #     import pprint
    #     pp = pprint.PrettyPrinter(indent=4)
    #     pp.pprint(self.statistics)
    #     f = open(os.path.dirname(__file__) + '/resources/reports.json', "w")
    #     f.write(json.dumps(self.data, indent=4))


class ExportImage(ExportEEAContent):
    QUERY = {
        "Image": {
            "UID": with_images_ids
        }
    }
    PORTAL_TYPE = ["Image"]
    type = "Image"

    def global_dict_hook(self, item, obj):
        return item


# TODO: append empty slate to the end of more info tab
# TODO: https://staging.eea.europa.eu/en/sandbox/migration-of-maps-and-graphs-to-new-plone-6/different-types-of-emissions-from-vehicles -> why do we see more info tab?
# TODO: https://staging.eea.europa.eu/en/sandbox/migration-of-maps-and-graphs-to-new-plone-6/emissions-trading-viewer-1 -> more info blocks_layout empty??
