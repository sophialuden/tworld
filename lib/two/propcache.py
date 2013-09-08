"""
Property cache: sits on top of the database and keeps known values in memory.

(Also keeps known *non*-values in memory; that is, a hit that returns nothing
is cached as a nothing.)

This also tracks mutable values, and is able to write them back to the
database if they change.

Currently, this lives in the app, but its lifespan is just the duration of one
task. A future version may hang around longer.
"""

import tornado.gen
from bson.objectid import ObjectId
import motor

# Collections that code may update. (As opposed to 'worldprop', etc,
# which may only be updated by build code.)
writable_collections = set(['instanceprop', 'iplayerprop'])

class PropCache:
    def __init__(self, app):
        # Keep a link to the owning application.
        self.app = app
        self.log = self.app.log

        self.objmap = {}  # maps id(val) to PropEntry
        self.propmap = {}  # maps tuple to PropEntry
        # propmap contains not-found entries; objmap does not.

    def final(self):
        """Shut down and clean up.

        This does not write back dirty data. Be sure to call write_all_dirty()
        before this.
        """
        ls = self.dirty_entries()
        if len(ls):
            self.log.error('propcache: finalizing while %d dirty entries!', len(ls))
            
        # Empty the maps, because PropEntry might have a backlink to this
        # PropCache someday and that would be a ref cycle.
        self.objmap.clear()
        self.propmap.clear()

        # Shut down.
        self.app = None
        self.objmap = None
        self.propmap = None

    @staticmethod
    def query_for_tuple(tup):
        (db, id1, id2, key) = tup
        if db == 'worldprop':
            return {'wid':id1, 'locid':id2, 'key':key}
        if db == 'instanceprop':
            return {'iid':id1, 'locid':id2, 'key':key}
        if db == 'wplayerprop':
            return {'wid':id1, 'uid':id2, 'key':key}
        if db == 'iplayerprop':
            return {'iid':id1, 'uid':id2, 'key':key}
        raise Exception('Unknown collection: %s' % (db,))

    @tornado.gen.coroutine
    def get(self, tup, dependencies=None):
        """Fetch a value from the database, or the cache if it's cached.

        The tup argument has four entries: ('worldprop', wid, locid, key).
        The meaning of the second and third depend on the database collection
        (the first entry). This is the same format as dependency keys.
        Currently this class understands 'instanceprop', 'worldprop',
        'iplayerprop', 'wplayerprop'.
        
        Returns a PropEntry (if found) or None (if not). The value you
        want is res.val if res is not None.

        (Note that this may return None to indicate that we checked the
        database earlier, found nothing, and cached that fact.)
        """
        if dependencies is not None:
            dependencies.add(tup)
            
        ent = self.propmap.get(tup, None)
        if ent is not None:
            if not ent.found:
                # Cached "not found" value
                return None
            return ent

        dbname = tup[0]
        query = PropCache.query_for_tuple(tup)
        res = yield motor.Op(self.app.mongodb[dbname].find_one,
                             query,
                             {'val':1})
        self.log.debug('### db get: %s %s (%s)', dbname, query, bool(res))
        if not res:
            ent = PropEntry(None, tup, query, found=False)
        else:
            val = res['val']
            ent = PropEntry(val, tup, query, found=True)
            self.objmap[ent.id] = ent
        self.propmap[tup] = ent

        if not ent.found:
            # Cached "not found" value
            return None
        return ent

    @tornado.gen.coroutine
    def set(self, tup, val):
        """Set a new object in the database (and the cache). If we had
        an object cached at this tuple, it's discarded.
        """
        ent = self.propmap.get(tup, None)
        if ent:
            if ent.found and ent.val is val:
                # It's already there (exactly the same object).
                return
            del self.propmap[tup]
            if ent.found:
                del self.objmap[ent.id]

        dbname = tup[0]
        assert dbname in writable_collections
        query = PropCache.query_for_tuple(tup)
        newval = dict(query)
        newval['val'] = val

        yield motor.Op(self.app.mongodb[dbname].update,
                       query, newval,
                       upsert=True)
        self.log.debug('### db set: %s %s', dbname, newval)

        ent = PropEntry(val, tup, query, found=True)
        self.objmap[ent.id] = ent
        self.propmap[tup] = ent
        
    @tornado.gen.coroutine
    def delete(self, tup):
        """Delete an object from the database (and the cache).
        """
        ent = self.propmap.get(tup, None)
        if ent:
            if not ent.found:
                # It's already non-there.
                return
            del self.propmap[tup]
            del self.objmap[ent.id]

        dbname = tup[0]
        assert dbname in writable_collections
        query = PropCache.query_for_tuple(tup)
        
        yield motor.Op(self.app.mongodb[dbname].remove,
                       query)
        self.log.debug('### db delete: %s %s', dbname, query)

        ent = PropEntry(None, tup, query, found=False)
        self.propmap[tup] = ent
        
    def get_by_object(self, val):
        """Check whether a value is in the cache. This is keyed by the
        *identity* of the value!
        Returns a PropEntry (if found) or None (if not).
        """
        return self.objmap.get(id(val), None)

    def dirty_entries(self):
        return [ ent for ent in self.objmap.values() if ent.dirty() ]

    @tornado.gen.coroutine
    def write_all_dirty(self):
        ls = self.dirty_entries()
        for ent in ls:
            yield self.write_dirty(ent)

    @tornado.gen.coroutine
    def write_dirty(self, ent):
        assert ent.found
        dbname = ent.tup[0]
        if dbname not in writable_collections:
            # Maybe we should update the equivalent writable entry here,
            # but we'll just skip it.
            self.log.warning('Unable to update %s entry: %s', dbname, ent.key)
            ent.origval = deepcopy(ent.val)
            return
        
        query = PropCache.query_for_tuple(ent.tup)
        newval = dict(ent.query)
        newval['val'] = ent.val

        yield motor.Op(self.app.mongodb[dbname].update,
                       query, newval,
                       upsert=True)
        self.log.debug('### db written: %s %s', dbname, newval)
        ent.origval = deepcopy(ent.val)

class PropEntry:
    """Represents a database entry, or perhaps the lack of a database entry.
    """
    
    def __init__(self, val, tup, query, found=True):
        self.val = val
        self.tup = tup  # Dependency key
        self.dbname = tup[0]  # Collection name
        self.key = tup[-1]
        self.query = query  # Query in the collection
        self.found = found  # Was a database entry found at all?
        
        if not found:
            self.mutable = False
        else:
            self.id = id(val)
            self.mutable = isinstance(val, (list, dict))
            if self.mutable:
                # Keep a copy, to check for possible changes
                self.origval = deepcopy(val)

    def __repr__(self):
        if not self.found:
            val = '(not found)'
        else:
            val = repr(self.val)
            if len(val) > 32:
                val = val[:32] + '...'
        return '<PropEntry %s: %s>' % (self.tup, val)

    def dirty(self):
        """Has this value changed since we cached it?
        (Always false for immutable and not-found values.)
        
        ### This will fail to detect changes that compare equal. That is,
        ### if an array [True] changes to [1], this will not notice the
        ### difference.
        """
        return self.mutable and (self.val != self.origval)

def deepcopy(val):
    """Return a copy of a value. For immutable values, this returns the
    value itself. For mutables, it returns a deep copy.

    This presumes that the value is DB-storable. Therefore, the only
    mutable types are list and dict. (And dict keys are always strings.)
    """
    if isinstance(val, list):
        return [ deepcopy(subval) for subval in val ]
    if isinstance(val, dict):
        return dict([ (key, deepcopy(subval)) for (key, subval) in val.items() ])
    return val

