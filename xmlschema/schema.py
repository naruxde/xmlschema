# -*- coding: utf-8 -*-
#
# Copyright (c), 2016, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
This module contains XMLSchema class creator for xmlschema package.
"""
import logging
import os.path

from .core import (
    XML_NAMESPACE_PATH, XSI_NAMESPACE_PATH, XLINK_NAMESPACE_PATH,
    HFP_NAMESPACE_PATH, etree_get_namespaces
)
from .exceptions import (
    XMLSchemaTypeError, XMLSchemaParseError, XMLSchemaValidationError,
    XMLSchemaDecodeError, XMLSchemaURLError
)
from .utils import URIDict, get_namespace, listify_update
from .xsdbase import (
    check_tag, get_xsi_schema_location, get_xsi_no_namespace_schema_location,
    XSD_SCHEMA_TAG, build_xsd_attributes, build_xsd_attribute_groups,
    build_xsd_simple_types, build_xsd_complex_types, build_xsd_groups,
    build_xsd_elements, iterfind_xsd_import,
    iterfind_xsd_include, iterfind_xsd_redefine, load_xsd_attribute_groups,
    load_xsd_attributes, load_xsd_groups, load_xsd_complex_types, load_xsd_simple_types,
    load_xsd_elements, XSD_ATTRIBUTE_TAG, XSD_COMPLEX_TYPE_TAG, XSD_ELEMENT_TAG,
    XSD_SIMPLE_TYPE_TAG, XSD_ATTRIBUTE_GROUP_TAG, XSD_GROUP_TAG, get_xsd_attribute
)
from .resources import open_resource, load_xml_resource
from .facets import XSD_v1_0_FACETS
from .builtins import XSD_BUILTIN_TYPES
from .factories import (
    xsd_simple_type_factory, xsd_restriction_factory, xsd_attribute_factory,
    xsd_attribute_group_factory, xsd_complex_type_factory,
    xsd_element_factory, xsd_group_factory
)

logger = logging.getLogger(__name__)


DEFAULT_OPTIONS = {
    'simple_type_factory': xsd_simple_type_factory,
    'attribute_factory': xsd_attribute_factory,
    'attribute_group_factory': xsd_attribute_group_factory,
    'complex_type_factory': xsd_complex_type_factory,
    'group_factory': xsd_group_factory,
    'element_factory': xsd_element_factory,
    'restriction_factory': xsd_restriction_factory
}
"""Default options for building XSD schema elements."""

SCHEMAS_DIR = os.path.join(os.path.dirname(__file__), 'schemas/')


class XsdGlobals(object):
    """
    Mediator class for related XML schema instances. It stores global declarations
    defined from registered schemas. Registering a schema to add it's declarations
    to the global maps.
    """
    def __init__(self, validator):
        self.validator = validator
        self.namespaces = URIDict()     # Registered schemas by namespace URI
        self.resources = URIDict()      # Registered schemas by resource URI

        self.types = {}             # Global types
        self.attributes = {}        # Global attributes
        self.attribute_groups = {}  # Attribute groups
        self.groups = {}            # Model groups
        self.elements = {}          # Global elements
        self.base_elements = {}     # Global elements + global groups expansion

        self.types.update(validator.BUILTIN_TYPES)

    def copy(self):
        obj = XsdGlobals(self.validator)
        obj.namespaces.update(self.namespaces)
        obj.resources.update(self.resources)
        obj.types.update(self.types)
        obj.attributes.update(self.attributes)
        obj.attribute_groups.update(self.attribute_groups)
        obj.groups.update(self.groups)
        obj.elements.update(self.elements)
        obj.base_elements.update(self.base_elements)
        return obj
    __copy__ = copy

    def register(self, schema):
        if schema.uri:
            if schema.uri not in self.resources:
                self.resources[schema.uri] = schema
            elif self.resources[schema.uri] != schema:
                return

        try:
            ns_schemas = self.namespaces[schema.target_namespace]
        except KeyError:
            self.namespaces[schema.target_namespace] = [schema]
        else:
            if schema in ns_schemas:
                return
            if not any([schema.uri == obj.uri for obj in ns_schemas]):
                ns_schemas.append(schema)

    def get_globals(self, map_name, namespace):
        return {
            k: v for k, v in getattr(self, map_name).items()
            if namespace == get_namespace(k)
        }

    def iter_schemas(self):
        for ns_schemas in self.namespaces.values():
            for schema in ns_schemas:
                yield schema

    def clear(self, remove_schemas=False):
        self.types.clear()
        self.attributes.clear()
        self.attribute_groups.clear()
        self.groups.clear()
        self.elements.clear()
        self.base_elements.clear()

        self.types.update(self.validator.BUILTIN_TYPES)
        for schema in self.iter_schemas():
            schema.built = False

        if remove_schemas:
            self.namespaces = URIDict()
            self.resources = URIDict()

    def build(self):
        kwargs = self.validator.OPTIONS.copy()

        # Load and build global declarations
        load_xsd_simple_types(self.types, self.iter_schemas())
        build_xsd_simple_types(self.types, XSD_SIMPLE_TYPE_TAG, **kwargs)
        load_xsd_attributes(self.attributes, self.iter_schemas())
        build_xsd_attributes(self.attributes, XSD_ATTRIBUTE_TAG, **kwargs)
        load_xsd_attribute_groups(self.attribute_groups, self.iter_schemas())
        build_xsd_attribute_groups(self.attribute_groups, XSD_ATTRIBUTE_GROUP_TAG, **kwargs)
        load_xsd_complex_types(self.types, self.iter_schemas())
        build_xsd_complex_types(self.types, XSD_COMPLEX_TYPE_TAG, **kwargs)
        load_xsd_elements(self.elements, self.iter_schemas())
        build_xsd_elements(self.elements, XSD_ELEMENT_TAG, **kwargs)
        load_xsd_groups(self.groups, self.iter_schemas())
        build_xsd_groups(self.groups, XSD_GROUP_TAG, **kwargs)

        # Build all local declarations
        build_xsd_groups(self.groups, XSD_GROUP_TAG, parse_local_groups=True, **kwargs)
        build_xsd_complex_types(self.types, XSD_COMPLEX_TYPE_TAG, parse_local_groups=True, **kwargs)
        build_xsd_elements(self.elements, XSD_ELEMENT_TAG, parse_local_groups=True, **kwargs)

        # Update base_elements
        self.base_elements.update(self.elements)
        for v in self.groups.values():
            self.base_elements.update({e.name: e for e in v.iter_elements()})

        for schema in self.iter_schemas():
            schema.built = True


def create_validator(version, meta_schema, base_schemas=None, facets=None,
                     builtin_types=None, **options):

    meta_schema = os.path.join(SCHEMAS_DIR, meta_schema)
    if base_schemas is None:
        base_schemas = {}
    else:
        base_schemas = {k: os.path.join(SCHEMAS_DIR, v) for k, v in base_schemas.items()}

    validator_options = dict(DEFAULT_OPTIONS.items())
    for opt in validator_options:
        if opt in options:
            validator_options[opt] = options[opt]

    class XMLSchemaValidator(object):
        """
        Class to wrap an XML Schema for components lookups and conversion.
        """
        VERSION = version
        META_SCHEMA = None
        BUILTIN_TYPES = builtin_types
        FACETS = facets or ()
        OPTIONS = validator_options

        def __init__(self, source, namespace=None, check_schema=False, global_maps=None):
            """
            Initialize an XML schema instance.

            :param source: This could be a string containing the schema, an URI
            that reference to a schema definition, a path to a file containing
            the schema or a file-like object containing the schema.
            """
            try:
                self.root, self.text, self.uri = load_xml_resource(source, element_only=False)
            except (XMLSchemaParseError, XMLSchemaTypeError, OSError, IOError) as err:
                raise type(err)('cannot create schema: %s' % err)

            check_tag(self.root, XSD_SCHEMA_TAG)
            self.built = False
            self.element_form = self.root.attrib.get('elementFormDefault', 'unqualified')
            self.attribute_form = self.root.attrib.get('attributeFormDefault', 'unqualified')

            # Determine the targetNamespace
            self.target_namespace = self.root.attrib.get('targetNamespace', '')
            if namespace is not None and self.target_namespace != namespace:
                if self.target_namespace:
                    raise XMLSchemaParseError(
                        "wrong namespace (%r instead of %r) for XSD resource %r." %
                        (self.target_namespace, namespace, self.uri)
                    )
                else:
                    self.target_namespace = namespace

            # Get schema location hints
            try:
                schema_location = get_xsi_schema_location(self.root).split()
            except AttributeError:
                self.schema_location = URIDict()
            else:
                self.schema_location = URIDict()
                listify_update(self.schema_location, dict(zip(schema_location[0::2], schema_location[1::2])))
            self.no_namespace_schema_location = get_xsi_no_namespace_schema_location(self.root)

            if global_maps is None:
                try:
                    self.maps = self.META_SCHEMA.maps.copy()
                except AttributeError:
                    self.maps = XsdGlobals(XMLSchemaValidator)
                else:
                    if self.target_namespace in self.maps.namespaces:
                        self.maps.clear()
            elif isinstance(global_maps, XsdGlobals):
                self.maps = global_maps
            else:
                raise XMLSchemaTypeError("'global_maps' argument must be a %r instance." % XsdGlobals)
            self.maps.register(self)

            # Extract namespaces from schema and include subschemas
            self.namespaces = {'xml': XML_NAMESPACE_PATH}  # the XML namespace is implicit
            namespaces = etree_get_namespaces(self.text)
            self.namespaces.update(namespaces)
            if '' not in self.namespaces:
                # For default local names are mapped to targetNamespace
                self.namespaces[''] = self.target_namespace

            if self.META_SCHEMA is not None:
                self.include_schemas(self.root, check_schema)
                self.import_schemas(self.root, check_schema)
                self.redefine_schemas(self.root, check_schema)

                if check_schema:
                    self.check_schema(self.root)

                # Builds the XSD objects only if the instance is
                # the creator of the XSD globals maps.
                if global_maps is None:
                    self.maps.build()
            else:
                # If the META_SCHEMA is not instantiated do not import
                # other namespaces and do not build maps.
                self.include_schemas(self.root)
                self.redefine_schemas(self.root, check_schema)

        def __repr__(self):
            return u"<%s '%s' at %#x>" % (self.__class__.__name__, self.target_namespace, id(self))

        @property
        def target_prefix(self):
            for prefix, namespace in self.namespaces.items():
                if namespace == self.target_namespace:
                    return prefix
            return ''

        @property
        def types(self):
            return self.maps.get_globals('types', self.target_namespace)

        @property
        def attributes(self):
            return self.maps.get_globals('attributes', self.target_namespace)

        @property
        def attribute_groups(self):
            return self.maps.get_globals('attribute_groups', self.target_namespace)

        @property
        def groups(self):
            return self.maps.get_globals('groups', self.target_namespace)

        @property
        def elements(self):
            return self.maps.get_globals('elements', self.target_namespace)

        @classmethod
        def create_schema(cls, *args, **kwargs):
            return cls(*args, **kwargs)

        @classmethod
        def check_schema(cls, schema):
            for error in cls.META_SCHEMA.iter_errors(schema):
                raise error

        def get_locations(self, namespace):
            if not namespace:
                return self.no_namespace_schema_location

            try:
                locations = self.schema_location[namespace]
            except KeyError:
                return None
            else:
                if isinstance(locations, list):
                    return ' '.join(locations)
                else:
                    return locations

        def import_schemas(self, elements, check_schema=False):
            for elem in iterfind_xsd_import(elements, namespaces=self.namespaces):
                namespace = elem.attrib.get('namespace', '').strip()
                if namespace in self.maps.namespaces:
                    continue

                locations = elem.attrib.get('schemaLocation', self.get_locations(namespace))
                if locations:
                    try:
                        schema_res, schema_uri = open_resource(locations, self.uri)
                        schema_res.close()
                    except XMLSchemaURLError as err:
                        raise XMLSchemaURLError(
                            reason="cannot import namespace %r: %s" % (namespace, err.reason)
                        )

                    try:
                        self.create_schema(
                            schema_uri, namespace or self.target_namespace, check_schema, self.maps
                        )
                    except (XMLSchemaParseError, XMLSchemaTypeError, OSError, IOError) as err:
                        raise type(err)('cannot import namespace %r: %s' % (namespace, err))

        def include_schemas(self, elements, check_schema=False):
            for elem in iterfind_xsd_include(elements, namespaces=self.namespaces):
                location = get_xsd_attribute(elem, 'schemaLocation')
                try:
                    schema_res, schema_uri = open_resource(location, self.uri)
                    schema_res.close()
                except XMLSchemaURLError as err:
                    raise XMLSchemaURLError(
                        reason="cannot include %r: %s" % (location, err.reason)
                    )

                if schema_uri not in self.maps.resources:
                    try:
                        self.create_schema(schema_uri, self.target_namespace, check_schema, self.maps)
                    except (XMLSchemaParseError, XMLSchemaTypeError, OSError, IOError) as err:
                        raise type(err)('cannot include %r: %s' % (schema_uri, err))

        def redefine_schemas(self, elements, check_schema=False):
            for elem in iterfind_xsd_redefine(elements, namespaces=self.namespaces):
                location = get_xsd_attribute(elem, 'schemaLocation')
                try:
                    schema_res, schema_uri = open_resource(location, self.uri)
                    schema_res.close()
                except XMLSchemaURLError as err:
                    raise XMLSchemaURLError(
                        reason="cannot redefine %r: %s" % (location, err.reason)
                    )

                if schema_uri not in self.maps.resources:
                    try:
                        self.create_schema(schema_uri, self.target_namespace, check_schema, self.maps)
                    except (XMLSchemaParseError, XMLSchemaTypeError, OSError, IOError) as err:
                        raise type(err)('cannot redefine %r: %s' % (schema_uri, err))

        def validate(self, *args, **kwargs):
            for error in self.iter_errors(*args, **kwargs):
                raise error

        def is_valid(self, xml_document):
            error = next(self.iter_errors(xml_document), None)
            return error is None

        def iter_errors(self, xml_document):
            for chunk in self.iter_decode(xml_document):
                if isinstance(chunk, (XMLSchemaDecodeError, XMLSchemaValidationError)):
                    yield chunk

        def iter_decode(self, xml_document):
            xml_root = load_xml_resource(xml_document)
            try:
                xsd_element = self.maps.elements[xml_root.tag]
            except KeyError:
                yield XMLSchemaValidationError(
                    self, xml_root.tag, "not a global element of the schema of the schema!", xml_root
                )
            else:
                for obj in xsd_element.iter_decode(xml_root):
                    yield obj

        def to_dict(self, xml_document):
            xml_root = load_xml_resource(xml_document)
            return self.maps.elements[xml_root.tag].decode(xml_root)

    # Create the meta schema
    if meta_schema is not None:
        meta_schema = XMLSchemaValidator(meta_schema)
        for k, v in list(base_schemas.items()):
            XMLSchemaValidator(v, global_maps=meta_schema.maps)

        XMLSchemaValidator.META_SCHEMA = meta_schema
        meta_schema.maps.build()

    if version is not None:
        XMLSchemaValidator.__name__ = 'XMLSchema_{}'.format(version.replace(".", "_"))

    return XMLSchemaValidator


# Define classes for generic XML schemas
XMLSchema_v1_0 = create_validator(
    version='1.0',
    meta_schema='XSD_1.0/XMLSchema.xsd',
    base_schemas={
        XML_NAMESPACE_PATH: 'xml_minimal.xsd',
        HFP_NAMESPACE_PATH: 'XMLSchema-hasFacetAndProperty_minimal.xsd',
        XSI_NAMESPACE_PATH: 'XMLSchema-instance_minimal.xsd',
        XLINK_NAMESPACE_PATH: 'xlink.xsd'
    },
    facets=XSD_v1_0_FACETS,
    builtin_types=XSD_BUILTIN_TYPES
)
XMLSchema = XMLSchema_v1_0


def validate(xml_document, schema=None, cls=None, *args, **kwargs):
    if cls is None:
        cls = XMLSchema
    xml_root, xml_text, xml_uri = load_xml_resource(xml_document, element_only=False)
    if schema is None:
        schema = open_resource(get_xsi_schema_location(xml_root), xml_uri)

    cls(schema, check_schema=True, *args, **kwargs).validate(xml_root)


def to_dict(xml_document, schema=None, cls=None, *args, **kwargs):
    if cls is None:
        cls = XMLSchema
    xml_root, xml_text, xml_uri = load_xml_resource(xml_document, element_only=False)
    if schema is None:
        schema = open_resource(get_xsi_schema_location(xml_root), xml_uri)

    cls(schema, *args, **kwargs).validate(xml_root)
    return cls(schema, check_schema=True, *args, **kwargs).to_dict(xml_root)
