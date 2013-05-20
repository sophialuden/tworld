
import sys
import datetime
import logging
import signal

import tornado.ioloop
import tornado.gen

import motor

import two.webconn
import two.playconn
import two.mongomgr
import two.commands
import two.task
import twcommon.misc
import twcommon.autoreload
from twcommon import wcproto

class Tworld(object):
    def __init__(self, opts):
        self.opts = opts
        self.log = logging.getLogger('tworld')

        self.all_commands = two.commands.define_commands()
        
        # This will be self.mongomgr.mongo[mongo_database], when that's
        # available.
        self.mongodb = None

        self.webconns = two.webconn.WebConnectionTable(self)
        self.playconns = two.playconn.PlayerConnectionTable(self)
        self.mongomgr = two.mongomgr.MongoMgr(self)

        # The command queue.
        self.queue = []
        self.commandbusy = False

        # Miscellaneous.
        self.caughtinterrupt = False
        self.shuttingdown = False

        # When the IOLoop starts, we'll set up periodic tasks.
        tornado.ioloop.IOLoop.instance().add_callback(self.init_timers)
        
    def init_timers(self):
        self.ioloop = tornado.ioloop.IOLoop.current()
        self.webconns.listen()
        self.mongomgr.init_timers()

        # Catch SIGINT (ctrl-C) with our own signal handler.
        signal.signal(signal.SIGINT, self.interrupt_handler)

        # This periodic command kicks disconnected players to the void.
        # (Every three minutes, plus an uneven fraction of a second.)
        def func():
            self.queue_command({'cmd':'checkdisconnected'})
        res = tornado.ioloop.PeriodicCallback(func, 180100)
        res.start()

    def shutdown(self, reason=None):
        """This is called when an orderly shutdown is requested. (Either
        an admin request, or by the interrupt handler.) It should only
        be called as part of its own command queue event (shutdownprocess).

        We set the shuttingdown flag, which means the command queue is
        frozen. Then we close all the sockets. Then we wait a second, to
        allow the sockets to finish closing. (IOStream doesn't seem to
        have an async close, seriously, wtf.) Then we exit the process.
        """
        self.shuttingdown = True
        self.mongomgr.close()
        self.webconns.close()
        self.log.info('Waiting 1 second for sockets to drain...')
        if reason == 'autoreload':
            def finalshutdown():
                self.log.info('Autoreloading for real.')
                twcommon.autoreload.autoreload()
        else:
            def finalshutdown():
                self.log.info('Shutting down for real.')
                sys.exit(0)
        self.ioloop.add_timeout(datetime.timedelta(seconds=1.0),
                                finalshutdown)

    def interrupt_handler(self, signum, stackframe):
        """This is called when Python catches a SIGINT (ctrl-C) signal.
        (It replaces the usual behavior of raising KeyboardInterrupt.)

        We don't want to interrupt a command (in the command queue). So
        we queue up a special command which will shut down the process.
        But in case that doesn't fly -- say, if the queue is jammed up --
        we shut down immediately on the second interrupt.
        """
        if self.caughtinterrupt:
            self.log.error('Interrupt! Shutting down immediately!')
            raise KeyboardInterrupt()
        self.log.warning('Interrupt! Queueing shutdown!')
        self.caughtinterrupt = True
        self.queue_command({'cmd':'shutdownprocess'})

    def autoreload_handler(self):
        self.log.warning('Queueing autoreload shutdown!')
        self.queue_command({'cmd':'shutdownprocess', 'restarting':'autoreload'})

    def schedule_command(self, obj, delay):
        """Schedule a command to be queued, delay seconds in the future.
        This only handles commands internal to tworld (connid 0, twwcid 0).
        
        This does *not* put the scheduled command in the database. It
        is therefore unreliable; if tworld shuts down before the command
        runs, it will be lost.
        """
        self.ioloop.add_timeout(datetime.timedelta(seconds=delay),
                                lambda:self.queue_command(obj))

    def queue_command(self, obj, connid=0, twwcid=0):
        if self.shuttingdown:
            self.log.warning('Not queueing command, because server is shutting down')
            return
        if type(obj) is dict:
            obj = wcproto.namespace_wrapper(obj)
        # If this command was caused by a message from tweb, twwcid is
        # its ID number. We will rarely need this.
        self.queue.append( (obj, connid, twwcid, twcommon.misc.now()) )
        
        if not self.commandbusy:
            self.ioloop.add_callback(self.pop_queue)

    @tornado.gen.coroutine
    def pop_queue(self):
        if self.commandbusy:
            self.log.warning('pop_queue called when already busy!')
            return

        if not self.queue:
            self.log.warning('pop_queue called when already empty!')
            return

        (cmdobj, connid, twwcid, queuetime) = self.queue.pop(0)

        task = two.task.Task(self, cmdobj, connid, twwcid, queuetime)
        self.commandbusy = True

        # Handle the command.
        try:
            yield task.handle()
        except Exception as ex:
            self.log.error('Error handling task: %s', cmdobj, exc_info=True)

        # Resolve all changes resulting from the command. We do this
        # in a separate try block, so that if the command died partway,
        # we still display the partial effects.
        if task.is_writable():
            try:
                yield task.resolve()
            except Exception as ex:
                self.log.error('Error resolving task: %s', cmdobj, exc_info=True)

        starttime = task.starttime
        endtime = twcommon.misc.now()
        self.log.info('Finished command in %.3f ms (queued for %.3f ms)',
                      (endtime-starttime).total_seconds() * 1000,
                      (starttime-queuetime).total_seconds() * 1000)

        self.commandbusy = False
        task.close()

        # Keep popping, if the queue is nonempty.
        if self.queue:
            self.ioloop.add_callback(self.pop_queue)

