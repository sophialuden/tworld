"""
This table manages the websocket connections from clients.

The table contains Connection objects. A connection is mostly a wrapper
for an active PlayWebSocketHandler, and has a nonzero connection ID as a
key. (The connection ID is requested by the handler before it adds itself
to the table. We use simple incrementing integers for the ID. There's no
need to track across tweb sessions, because if tweb crashes, we lose all
the websockets anyway...)

A Connection is "available" once it has been sent to the tworld (and we
got an ack back). If tworld crashes, all connections become unavailable
until it returns (and then we have to ack them again).
"""

import datetime

import tweblib.handlers

class ConnectionTable(object):
    def __init__(self, app):
        # Keep a link to the owning application.
        self.app = app
        self.table = {}
        self.counter = 1

    def generate_connid(self):
        res = self.counter
        self.counter += 1
        return res

    def as_dict(self):
        return dict(self.table)

    def add(self, handler, uid, email, sessionid):
        """Add the handler to the table, as a new Connection. It will
        initially be unavailable. Returns the Connection.
        """
        assert isinstance(handler, tweblib.handlers.PlayWebSocketHandler)
        assert handler.twconnid, 'handler.twconnid is not positive'
        conn = Connection(handler, uid, email, sessionid)
        self.table[conn.connid] = conn
        return conn

    def find(self, connid):
        """Return the connection with the given connid. Throws an exception
        if not found.
        """
        return self.table[connid]

    def remove(self, handler):
        if not handler.twconnid:
            return
        conn = self.table.get(handler.twconnid, None)
        if not conn:
            return
        assert handler.twconnid == conn.connid, 'Connection ID did not match at remove!'
        conn.handler = None
        conn.uid = None
        conn.available = False
        del self.table[handler.twconnid]
        
class Connection(object):
    def __init__(self, handler, uid, email, sessionid):
        self.handler = handler
        self.connid = handler.twconnid
        self.uid = uid
        self.sessionid = sessionid
        self.email = email
        self.starttime = datetime.datetime.now()
        self.lastmsgtime = self.starttime   # last user activity
        self.available = False

    def __repr__(self):
        return '<Connection %d>' % (self.connid,)

    def uptime(self):
        """Return how long the connection has been open. But trim off the
        microseconds, because that's silly.
        """
        delta = datetime.datetime.now() - self.starttime
        return datetime.timedelta(seconds=int(delta.total_seconds()))

    def idletime(self):
        """Return how long it's been since the last activity from the
        player.
        """
        delta = datetime.datetime.now() - self.lastmsgtime
        return datetime.timedelta(seconds=int(delta.total_seconds()))

    def close(self, errmsg=None):
        """Close the connection. Optionally send an error message through
        first.
        """
        if self.handler:
            if (errmsg):
                self.handler.write_tw_error(errmsg)
            self.handler.close()
        
    def write_tw_error(self, msg):
        """Write a JSON error-reporting command through the socket.
        """
        if self.handler:
            self.handler.write_tw_error(msg)
