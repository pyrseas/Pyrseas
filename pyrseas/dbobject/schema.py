# -*- coding: utf-8 -*-
"""
    pyrseas.dbobject.schema
    ~~~~~~~~~~~~~~~~~~~~~~~

    This defines two classes, Schema and SchemaDict, derived from
    DbObject and DbObjectDict, respectively.
"""
import os

from pyrseas.yamlutil import yamldump
from pyrseas.dbobject import DbObjectDict, DbObject
from pyrseas.dbobject import quote_id, split_schema_obj
from pyrseas.dbobject import commentable, ownable, grantable
from pyrseas.dbobject.dbtype import BaseType, Composite, Domain, Enum
from pyrseas.dbobject.table import Table, Sequence, View, MaterializedView
from pyrseas.dbobject.privileges import privileges_from_map


class Schema(DbObject):
    """A database schema definition, i.e., a named collection of tables,
    views, triggers and other schema objects."""

    keylist = ['name']
    objtype = 'SCHEMA'

    @property
    def allprivs(self):
        return 'UC'

    def extern_dir(self, root='.'):
        """Return the path to a directory to hold the schema objects.

        :return: directory path
        """
        (dir, ext) = os.path.splitext(os.path.join(root,
                                                   self.extern_filename()))
        return dir

    def to_map(self, dbschemas, opts, metadata_dir):
        """Convert tables, etc., dictionaries to a YAML-suitable format

        :param dbschemas: dictionary of schemas
        :param opts: options to include/exclude schemas/tables, etc.
        :return: dictionary
        """
        if self.name == 'pyrseas':
            return {}
        no_owner = opts.no_owner
        no_privs = opts.no_privs
        schbase = {} if no_owner else {'owner': self.owner}
        if not no_privs and hasattr(self, 'privileges'):
            schbase.update({'privileges': self.map_privs()})
        if hasattr(self, 'description'):
            schbase.update(description=self.description)

        schobjs = []
        seltbls = getattr(opts, 'tables', [])
        if hasattr(self, 'tables'):
            for objkey in list(self.tables.keys()):
                if not seltbls or objkey in seltbls:
                    obj = self.tables[objkey]
                    schobjs.append((obj, obj.to_map(dbschemas, opts)))

        def mapper(objtypes):
            if hasattr(self, objtypes):
                schemadict = getattr(self, objtypes)
                for objkey in list(schemadict.keys()):
                    if objtypes == 'sequences' or (
                            not seltbls or objkey in seltbls):
                        obj = schemadict[objkey]
                        schobjs.append((obj, obj.to_map(opts)))

        for objtypes in ['ftables', 'sequences', 'views', 'matviews']:
            mapper(objtypes)

        def mapper2(objtypes):
            if hasattr(self, objtypes):
                schemadict = getattr(self, objtypes)
                for objkey in list(schemadict.keys()):
                    obj = schemadict[objkey]
                    schobjs.append((obj, obj.to_map(no_owner)))

        if hasattr(opts, 'tables') and not opts.tables or \
                not hasattr(opts, 'tables'):
            for objtypes in ['conversions', 'domains',
                             'operators', 'operclasses', 'operfams',
                             'tsconfigs', 'tsdicts', 'tsparsers', 'tstempls',
                             'types', 'collations']:
                mapper2(objtypes)
            if hasattr(self, 'functions'):
                for objkey in list(self.functions.keys()):
                    obj = self.functions[objkey]
                    schobjs.append((obj, obj.to_map(no_owner, no_privs)))

        # special case for pg_catalog schema
        if self.name == 'pg_catalog' and not schobjs:
            return {}

        if opts.multiple_files:
            dir = self.extern_dir(metadata_dir)
            if not os.path.exists(dir):
                os.mkdir(dir)
            filemap = {}
            for obj, objmap in schobjs:
                if objmap is not None:
                    extkey = obj.extern_key()
                    filepath = os.path.join(dir, obj.extern_filename())
                    with open(filepath, 'a') as f:
                        f.write(yamldump({extkey: objmap}))
                    outobj = {extkey: filepath}
                filemap.update(outobj)
            # always write the schema YAML file
            filepath = os.path.join(metadata_dir, self.extern_filename())
            extkey = self.extern_key()
            with open(filepath, 'a') as f:
                f.write(yamldump({extkey: schbase}))
            filemap.update(schema=filepath)
            return {extkey: filemap}

        schmap = {obj.extern_key(): objmap for obj, objmap in schobjs
                  if objmap is not None}
        schmap.update(schbase)
        return {self.extern_key(): schmap}

    @commentable
    @grantable
    @ownable
    def create(self):
        """Return SQL statements to CREATE the schema

        :return: SQL statements
        """
        return ["CREATE SCHEMA %s" % quote_id(self.name)]


PREFIXES = {'domain ': 'types', 'type': 'types', 'table ': 'tables',
            'view ': 'tables', 'sequence ': 'tables',
            'materialized view ': 'tables',
            'function ': 'functions', 'aggregate ': 'functions',
            'operator family ': 'operfams', 'operator class ': 'operclasses',
            'conversion ': 'conversions', 'text search dictionary ': 'tsdicts',
            'text search template ': 'tstempls',
            'text search parser ': 'tsparsers',
            'text search configuration ': 'tsconfigs',
            'foreign table ': 'ftables', 'collation ': 'collations'}
SCHOBJS1 = ['types', 'tables', 'ftables']
SCHOBJS2 = ['collations', 'conversions', 'functions', 'operators',
            'operclasses', 'operfams', 'tsconfigs', 'tsdicts', 'tsparsers',
            'tstempls']


class SchemaDict(DbObjectDict):
    "The collection of schemas in a database.  Minimally, the 'public' schema."

    cls = Schema
    query = \
        """SELECT nspname AS name, rolname AS owner,
                  array_to_string(nspacl, ',') AS privileges,
                  obj_description(n.oid, 'pg_namespace') AS description
           FROM pg_namespace n
                JOIN pg_roles r ON (r.oid = nspowner)
           WHERE nspname NOT IN ('information_schema', 'pg_toast')
                 AND nspname NOT LIKE 'pg_temp\_%'
                 AND nspname NOT LIKE 'pg_toast_temp\_%'
           ORDER BY nspname"""

    def from_map(self, inmap, newdb):
        """Initialize the dictionary of schemas by converting the input map

        :param inmap: the input YAML map defining the schemas
        :param newdb: collection of dictionaries defining the database

        Starts the recursive analysis of the input map and
        construction of the internal collection of dictionaries
        describing the database objects.
        """
        for key in list(inmap.keys()):
            (objtype, spc, sch) = key.partition(' ')
            if spc != ' ' or objtype != 'schema':
                raise KeyError("Unrecognized object type: %s" % key)
            schema = self[sch] = Schema(name=sch)
            inschema = inmap[key]
            objdict = {}
            for key in sorted(inschema.keys()):
                mapped = False
                for prefix in list(PREFIXES.keys()):
                    if key.startswith(prefix):
                        otype = PREFIXES[prefix]
                        if otype not in objdict:
                            objdict[otype] = {}
                        objdict[otype].update({key: inschema[key]})
                        mapped = True
                        break
                # Needs separate processing because it overlaps
                # operator classes and operator families
                if not mapped and key.startswith('operator '):
                    otype = 'operators'
                    if otype not in objdict:
                        objdict[otype] = {}
                    objdict[otype].update({key: inschema[key]})
                    mapped = True
                elif key in ['oldname', 'owner', 'description']:
                    setattr(schema, key, inschema[key])
                    mapped = True
                elif key == 'privileges':
                    schema.privileges = privileges_from_map(
                        inschema[key], schema.allprivs, schema.owner)
                    mapped = True
                if not mapped and key != 'schema':
                    raise KeyError("Expected typed object, found '%s'" % key)

            for objtype in SCHOBJS1:
                if objtype in objdict:
                    subobjs = getattr(newdb, objtype)
                    subobjs.from_map(schema, objdict[objtype], newdb)
            for objtype in SCHOBJS2:
                if objtype in objdict:
                    subobjs = getattr(newdb, objtype)
                    subobjs.from_map(schema, objdict[objtype])

    def link_refs(self, dbtypes, dbtables, dbfunctions, dbopers, dbopfams,
                  dbopcls, dbconvs, dbtsconfigs, dbtsdicts, dbtspars,
                  dbtstmpls, dbftables, dbcolls):
        """Connect types, tables and functions to their respective schemas

        :param dbtypes: dictionary of types and domains
        :param dbtables: dictionary of tables, sequences and views
        :param dbfunctions: dictionary of functions
        :param dbopers: dictionary of operators
        :param dbopfams: dictionary of operator families
        :param dbopcls: dictionary of operator classes
        :param dbconvs: dictionary of conversions
        :param dbtsconfigs: dictionary of text search configurations
        :param dbtsdicts: dictionary of text search dictionaries
        :param dbtspars: dictionary of text search parsers
        :param dbtstmpls: dictionary of text search templates
        :param dbftables: dictionary of foreign tables
        :param dbcolls: dictionary of collations

        Fills in the `domains` dictionary for each schema by
        traversing the `dbtypes` dictionary.  Fills in the `tables`,
        `sequences`, `views` dictionaries for each schema by
        traversing the `dbtables` dictionary. Fills in the `functions`
        dictionary by traversing the `dbfunctions` dictionary.
        """
        def link_one(sch, objtype, objkeys, obj):
            schema = self[sch]
            if not hasattr(schema, objtype):
                setattr(schema, objtype, {})
            objdict = getattr(schema, objtype)
            objdict.update({objkeys: obj})

        for (sch, typ) in list(dbtypes.keys()):
            dbtype = dbtypes[(sch, typ)]
            if isinstance(dbtype, Domain):
                link_one(sch, 'domains', typ, dbtype)
            elif isinstance(dbtype, Enum) or isinstance(dbtype, Composite) \
                    or isinstance(dbtype, BaseType):
                link_one(sch, 'types', typ, dbtype)
        for (sch, tbl) in list(dbtables.keys()):
            table = dbtables[(sch, tbl)]
            if isinstance(table, Table):
                link_one(sch, 'tables', tbl, table)
            elif isinstance(table, Sequence):
                link_one(sch, 'sequences', tbl, table)
            elif isinstance(table, MaterializedView):
                link_one(sch, 'matviews', tbl, table)
            elif isinstance(table, View):
                link_one(sch, 'views', tbl, table)
        for (sch, fnc, arg) in list(dbfunctions.keys()):
            func = dbfunctions[(sch, fnc, arg)]
            link_one(sch, 'functions', (fnc, arg), func)
            if hasattr(func, 'returns'):
                rettype = func.returns
                if rettype.upper().startswith("SETOF "):
                    rettype = rettype[6:]
                (retsch, rettyp) = split_schema_obj(rettype, sch)
                if (retsch, rettyp) in list(dbtables.keys()):
                    deptbl = dbtables[(retsch, rettyp)]
                    if not hasattr(func, 'dependent_table'):
                        func.dependent_table = deptbl
                    if not hasattr(deptbl, 'dependent_funcs'):
                        deptbl.dependent_funcs = []
                    deptbl.dependent_funcs.append(func)
        for (sch, opr, lft, rgt) in list(dbopers.keys()):
            oper = dbopers[(sch, opr, lft, rgt)]
            link_one(sch, 'operators', (opr, lft, rgt), oper)
        for (sch, opc, idx) in list(dbopcls.keys()):
            opcl = dbopcls[(sch, opc, idx)]
            link_one(sch, 'operclasses', (opc, idx), opcl)
        for (sch, opf, idx) in list(dbopfams.keys()):
            opfam = dbopfams[(sch, opf, idx)]
            link_one(sch, 'operfams', (opf, idx), opfam)
        for (sch, cnv) in list(dbconvs.keys()):
            conv = dbconvs[(sch, cnv)]
            link_one(sch, 'conversions', cnv, conv)
        for (sch, tsc) in list(dbtsconfigs.keys()):
            tscfg = dbtsconfigs[(sch, tsc)]
            link_one(sch, 'tsconfigs', tsc, tscfg)
        for (sch, tsd) in list(dbtsdicts.keys()):
            tsdict = dbtsdicts[(sch, tsd)]
            link_one(sch, 'tsdicts', tsd, tsdict)
        for (sch, tsp) in list(dbtspars.keys()):
            tspar = dbtspars[(sch, tsp)]
            link_one(sch, 'tsparsers', tsp, tspar)
        for (sch, tst) in list(dbtstmpls.keys()):
            tstmpl = dbtstmpls[(sch, tst)]
            link_one(sch, 'tstempls', tst, tstmpl)
        for (sch, ftb) in list(dbftables.keys()):
            ftbl = dbftables[(sch, ftb)]
            link_one(sch, 'ftables', ftb, ftbl)
        for (sch, cll) in list(dbcolls.keys()):
            coll = dbcolls[(sch, cll)]
            link_one(sch, 'collations', cll, coll)

    def to_map(self, opts, metadata_dir=None):
        """Convert the schema dictionary to a regular dictionary

        :param opts: options to include/exclude schemas/tables, etc.
        :return: dictionary

        Invokes the `to_map` method of each schema to construct a
        dictionary of schemas.
        """
        schemas = {}
        selschs = getattr(opts, 'schemas', [])
        for sch in list(self.keys()):
            if not selschs or sch in selschs:
                if hasattr(opts, 'excl_schemas') and opts.excl_schemas \
                        and sch in opts.excl_schemas:
                    continue
                schemas.update(self[sch].to_map(self, opts, metadata_dir))

        return schemas

    def diff_map(self, inschemas):
        """Generate SQL to transform existing schemas

        :param input_map: a YAML map defining the new schemas
        :return: list of SQL statements

        Compares the existing schema definitions, as fetched from the
        catalogs, to the input map and generates SQL statements to
        transform the schemas accordingly.
        """
        stmts = []
        # check input schemas
        for sch in list(inschemas.keys()):
            insch = inschemas[sch]
            # does it exist in the database?
            if sch in self:
                stmts.append(self[sch].diff_map(insch))
            else:
                # check for possible RENAME
                if hasattr(insch, 'oldname'):
                    oldname = insch.oldname
                    try:
                        stmts.append(self[oldname].rename(insch.name))
                        del self[oldname]
                    except KeyError as exc:
                        exc.args = ("Previous name '%s' for schema '%s' "
                                    "not found" % (oldname, insch.name), )
                        raise
                else:
                    # create new schema
                    stmts.append(insch.create())
        # check database schemas
        for sch in list(self.keys()):
            # if missing and not 'public', drop it
            if sch not in ['public', 'pg_catalog'] and sch not in inschemas:
                self[sch].dropped = True
        return stmts

    def _drop(self):
        """Actually drop the schemas

        :return: SQL statements
        """
        stmts = []
        for sch in list(self.keys()):
            if sch != 'public' and hasattr(self[sch], 'dropped'):
                stmts.append(self[sch].drop())
        return stmts
