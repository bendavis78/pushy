# Copyright (c) 2008, 2009 Andrew Wilkins <axwalk@gmail.com>
# 
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
# 
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import logging, marshal, os, struct, sys, thread, threading
from pushy.protocol.message import Message, MessageType, message_types
from pushy.protocol.proxy import Proxy, ProxyType, get_opmask
import pushy.util


# This collection should contain only immutable types. Builtin, mutable types
# such as list, set and dict need to be handled specially.
marshallable_types = (
    unicode, slice, frozenset, float, basestring, long, str, int, complex,
    bool, buffer, type(None)
)

# Message types that may received in response to a request.
response_types = (
    MessageType.response, MessageType.exception
)


class LoggingFile:
    def __init__(self, stream, log):
        self.stream = stream
        self.log = log
    def write(self, s):
        self.log.write(s)
        self.stream.write(s)
    def flush(self):
        self.log.flush()
        self.stream.flush()
    def read(self, n):
        data = self.stream.read(n)
        self.log.write(data)
        self.log.flush()
        return data


class ResponseHandler:
    def __init__(self):
        self.event       = threading.Event()
        self.message     = None
        self.syncrequest = False
        self.thread      = thread.get_ident()

    def clear(self):
        self.event.clear()
        self.message = None

    def wait(self):
        pushy.util.logger.debug("Waiting on handler")
        while not self.event.isSet():
            self.event.wait()
        message = self.message
        self.clear()
        pushy.util.logger.debug("Returning from handler: %r", message)
        return message

    def get(self):
        if self.event.isSet():
            return self.message
        return None

    def set(self, message=None):
        if message is not None:
            self.message = message
            pushy.util.logger.debug("Setting message on handler: %r", message)
        self.event.set()


class BaseConnection:
    def __init__(self, istream, ostream, initiator=True):
        self.__open           = True
        self.__istream        = istream
        self.__ostream        = ostream
        self.__initiator      = initiator
        self.__istream_lock   = threading.Lock()
        self.__ostream_lock   = threading.Lock()
        self.__request_lock   = threading.RLock()
        self.__unmarshal_lock = threading.Lock()
        self.__pid            = os.getpid() # Record pid in event of a fork

        # Define message handlers (MessageType -> method)
        self.message_handlers = {
            MessageType.response:    self.__handle_response,
            MessageType.exception:   self.__handle_exception,
            MessageType.syncrequest: self.__handle_syncrequest
        }

        # Attributes required to track responses.
        self.__thread_local      = threading.local()
        self.__response_handlers = []

        # Attributes required to track number of threads processing requests.
        # The following has to be true for the message receiving thread to be
        # allowed to attempt to receive a message:
        #     - There are no threads currently processing a request, and
        #       there are no requests pending.
        #  OR
        #     - There are threads currently processing a request, but they
        #       are all waiting on responses to syncrequests.
        self.__receiving  = False # Is someone calling self.__recv?
        self.__processing = 0  # How many requests are being processed.
        self.__waiting    = 0  # How many syncrequest responses are pending.
        self.__responses  = 0
        self.__requests   = []
        self.__processing_condition = threading.Condition(threading.Lock())

        # Uncomment the following for debugging.
        #self.__istream = LoggingFile(istream, open("%d.in"%os.getpid(),"wb"))
        #self.__ostream = LoggingFile(ostream, open("%d.out"%os.getpid(),"wb"))

        # (Client) Contains mapping of id(obj) -> proxy
        self.__proxies = {}
        # (Client) Contains mapping of id(obj) -> threading.Event, which
        # __unmarshal will use to synchronise the order of messages.
        self.__pending_proxies = {}
        # (Client) Contains mapping of id(proxy) -> id(obj)
        self.__proxy_ids = {}
        # (Server) Contains mapping of id(obj) -> obj
        self.__proxied_objects = {}


    def __del__(self):
        if self.__open:
            self.close()


    def close(self):
        pushy.util.logger.debug("Closing connection")
        try:
            if self.__open:
                self.__request_lock.acquire()
                if not self.__open:
                    return
                try:
                    # Flag the connection as closed, and wake up all request
                    # handlers. We'll then wait until there are no more
                    # response handlers waiting.
                    self.__open = False
                    self.__processing_condition.acquire()
                    try:
                        # Wake up request/response handlers.
                        self.__processing_condition.notifyAll()
                        for handler in self.__response_handlers:
                            handler.set()
                        # Wait until there are no more response handlers, and
                        # no requests being processed.
                        while len(self.__response_handlers) > 0 or \
                              self.__processing > 0:
                            self.__processing_condition.wait()
                    finally:
                        self.__processing_condition.release()

                    self.__ostream_lock.acquire()
                    try:
                        self.__ostream.close()
                        pushy.util.logger.debug("Closed ostream")
                    finally:
                        self.__ostream_lock.release()
                    self.__istream_lock.acquire()
                    try:
                        self.__istream.close()
                        pushy.util.logger.debug("Closed istream")
                    finally:
                        self.__istream_lock.release()
                finally:
                    self.__request_lock.release()
        except:
            import traceback
            traceback.print_exc()
            pushy.util.logger.debug(traceback.format_exc())
        finally:
            pushy.util.logger.debug("Closed connection")


    def serve_forever(self):
        "Serve asynchronous requests from the peer forever."
        try:
            while self.__open:
                try:
                    m = self.__waitForRequest()
                    if m is not None and self.__open:
                        self.__handle(m)
                except IOError:
                    return
        finally:
            pushy.util.logger.debug("Leaving serve_forever")


    def send_request(self, message_type, args):
        "Send a request message and wait for a response."
        self.__request_lock.acquire()
        try:
            if not self.__open:
                raise Exception, "Connection is closed"

            # Send the request. Send it as a 'syncrequest' if the request
            # is made from the handler of a request from the peer.
            if getattr(self.__thread_local, "request_count", 0) > 0:
                pushy.util.logger.debug(
                    "Converting %r to a syncrequest", message_type)
                args = (message_type.code, self.__marshal(args))
                message_type = MessageType.syncrequest

            # Create a new response handler.
            handler = ResponseHandler()

            # If the a syncrequest is made, then reduce the 'processing'
            # count, so the message receiving thread may attempt to
            # receive messages.
            if message_type == MessageType.syncrequest:
                handler.syncrequest = True
                self.__processing_condition.acquire()
                try:
                    self.__waiting += 1
                    if self.__processing == self.__waiting:
                        pushy.util.logger.debug("Notify")
                        self.__processing_condition.notify()

                    # Insert the handler just before the first handler for the
                    # current thread.
                    i = 0
                    while i < len(self.__response_handlers):
                        if self.__response_handlers[i].thread == \
                           thread.get_ident():
                            pushy.util.logger.debug("=> %d", i)
                            break
                        else:
                            i += 1
                    #i = 0
                    pushy.util.logger.debug("Inserting handler at %d", i)
                    self.__response_handlers.insert(i, handler)
                finally:
                    self.__processing_condition.release()
            else:
                handler.syncrequest = False
                self.__response_handlers.append(handler)
                pushy.util.logger.debug("Appending handler")

            # Send the message.
            self.__send_message(message_type, args)
        finally:
            self.__request_lock.release()

        # Wait for the response handler to be signalled.
        m = self.__waitForResponse(handler)
        while m.type is MessageType.syncrequest:
            self.__handle(m)
            m = self.__waitForResponse(handler)
        return self.__handle(m)


    def __send_response(self, result):
        # Allow the message receiving thread to proceed. We must do this
        # *before* sending the message, in case the other side is
        # attempting to send a message at the same time.
        self.__processing_condition.acquire()
        try:
            self.__processing -= 1
            if self.__processing == 0:
                self.__processing_condition.notify()
        finally:
            self.__processing_condition.release()

        # Now send the message.
        pushy.util.logger.debug("Sending response, type: %r", type(result))
        self.__send_message(MessageType.response, result)


    def __waitForRequest(self):
        pushy.util.logger.debug("Enter waitForRequest")
        # Wait for a request message. If a response message is received first,
        # then set the relevant response handler and wait until we're allowed
        # to read a message before proceeding.
        self.__processing_condition.acquire()
        try:
            # Wait until we're allowed to read from the input stream, or
            # another thread has enqueued a request for us.
            while (self.__open and (len(self.__requests) == 0)) and \
                   (self.__receiving or \
                    self.__responses > 0 or \
                     (self.__processing > 0 and \
                      (self.__processing > self.__waiting))):
                self.__processing_condition.wait()

            # Check if the connection is still open.
            if not self.__open:
                return None

            # Check if another thread received a request message.
            if len(self.__requests) > 0:
                self.__processing += 1
                request = self.__requests[0]
                del self.__requests[0]
                if len(self.__response_handlers) > 0:
                    self.__response_handlers[0].set()
                return request

            # Release the processing condition, and wait for a message.
            self.__receiving = True
            self.__processing_condition.release()
            try:
                m = self.__recv()
                if m.type in response_types:
                    # Notify the first response handler, and pop it off the
                    # front of the queue. If it's a response to a syncrequest,
                    # decrement the waiting count.
                    self.__responses += 1
                    self.__response_handlers[0].set(m)
                elif m.type is MessageType.syncrequest:
                    # Notify the first response handler, and increment the
                    # processing count.
                    self.__processing += 1
                    self.__response_handlers[0].set(m)
                else:
                    # We got a request, so return it. If there are any response
                    # handlers waiting, let's wake up the first one so it can
                    # wait for a message.
                    if self.__open:
                        self.__processing += 1
                    if len(self.__response_handlers) > 0:
                        self.__response_handlers[0].set()
                    return m
            finally:
                self.__processing_condition.acquire()
                self.__receiving = False
        finally:
            self.__processing_condition.release()
            pushy.util.logger.debug("Leave waitForRequest")


    def __waitForResponse(self, handler):
        pushy.util.logger.debug("Enter waitForResponse")
        self.__processing_condition.acquire()
        try:
            # Wait until we're allowed to read from the input stream, or
            # another thread has enqueued a request for us.
            m = handler.get()
            if m is not None:
                pushy.util.logger.debug("Already set")
                handler.clear()
            while (self.__open and m is None) and \
                   (self.__receiving or \
                    (handler != self.__response_handlers[0]) or \
                     (self.__processing > 0 and \
                      (self.__processing > self.__waiting))):
                self.__processing_condition.release()
                try:
                    pushy.util.logger.debug("Going to wait")
                    pushy.util.logger.debug(
                        "receiving: %r, first: %r, processing: %d, waiting: %d",
                        self.__receiving, (handler == self.__response_handlers[0]),
                        self.__processing, self.__waiting)
                    if handler != self.__response_handlers[0]:
                        self.__response_handlers[0].set()
                    m = handler.wait()
                finally:
                    self.__processing_condition.acquire()

            pushy.util.logger.debug("m = %r (%r)", m, id(m))
            pushy.util.logger.debug("handler.get() = %r", handler.get())

            # Wait until we've got a response/syncrequest.
            if m is None and self.__open:
                self.__receiving = True
                self.__processing_condition.release()
                try:
                    m = self.__recv()
                    if handler != self.__response_handlers[0]:
                        print >> open("kersplat", "w"), "we're not first"
                    while m.type not in response_types and \
                          m.type is not MessageType.syncrequest:
                        if os.getpid() != self.__pid:
                            print >> open("kersplat", "w"), "uh oh"
                        self.__requests.append(m)
                        m = self.__recv()
                        if handler != self.__response_handlers[0]:
                            print >> open("kersplat", "w"), "we're not first"
                finally:
                    self.__processing_condition.acquire()
                    self.__receiving = False

                # Update processing/waiting counts.
                if m.type in response_types:
                    del self.__response_handlers[0]
                elif m.type is MessageType.syncrequest:
                    self.__processing += 1
            elif self.__open:
                if m.type in response_types:
                    del self.__response_handlers[0]
                    self.__responses -= 1

            # Delete handler.
            if handler.syncrequest:
                self.__waiting -= 1

            # Return the message.
            if not self.__open and m is None:
                del self.__response_handlers[0]
                raise Exception, "Connection is closed"
            return m
        finally:
            self.__processing_condition.notify()
            self.__processing_condition.release()
            pushy.util.logger.debug("Leave waitForResponse")


    def __marshal(self, obj):
        # XXX perhaps we can check refcount to optimise (if 1, immutable)
        try:
            if type(obj) in marshallable_types:
                return "s" + marshal.dumps(obj, 0)
        except ValueError:
            pass

        # If it's a tuple, try to marshal each item individually.
        if type(obj) is tuple:
            payload = "t"
            try:
                for item in obj:
                    part = self.__marshal(item)
                    payload += struct.pack(">I", len(part))
                    payload += part
                return payload
            except ValueError: pass

        i = id(obj)
        if i in self.__proxied_objects:
            return "p" + marshal.dumps(i)
        elif i in self.__proxy_ids:
            # Object originates at the peer.
            return "o" + marshal.dumps(self.__proxy_ids[i])
        else:
            # Create new entry in proxy objects map:
            #    id -> (obj, refcount, opmask[, args])
            #
            # opmask is a bitmask defining whether or not the object
            # defines various methods (__add__, __iter__, etc.)
            opmask = get_opmask(obj)
            proxy_result = ProxyType.get(obj)

            if type(proxy_result) is tuple:
                obj_type, args = proxy_result
                dumps_args = \
                    (i, opmask, int(obj_type), self.__marshal(args))
            else:
                obj_type = proxy_result
                dumps_args = (i, opmask, int(obj_type))

            self.__proxied_objects[i] = obj
            return "p" + marshal.dumps(dumps_args, 0)


    def __unmarshal(self, payload):
        if payload.startswith("s"):
            # Simple type
            return marshal.loads(buffer(payload, 1))
        elif payload.startswith("t"):
            size_size = struct.calcsize(">I")
            payload = buffer(payload, 1)
            parts = []
            while len(payload) > 0:
                size = struct.unpack(">I", payload[:size_size])[0]
                payload = buffer(payload, size_size)
                parts.append(self.__unmarshal(payload[:size]))
                payload = buffer(payload, size)
            return tuple(parts)
        elif payload.startswith("p"):
            # Proxy object
            id_ = marshal.loads(buffer(payload, 1))
            if type(id_) is tuple:
                # New object: (id, opmask, object_type)
                args = None
                if len(id_) >= 4:
                    args = self.__unmarshal(id_[3])
                p = Proxy(id_[0], id_[1], id_[2], args, self,
                          self.__register_proxy)

                # Wake anyone waiting on this ID to be unmarshalled.
                self.__unmarshal_lock.acquire()
                try:
                    if id_[0] in self.__pending_proxies:
                        event = self.__pending_proxies[id_[0]]
                        del self.__pending_proxies[id_[0]]
                        event.set()
                finally:
                    self.__unmarshal_lock.release()

                return p
            else:
                # Known object: id
                if id_ not in self.__proxies:
                    self.__unmarshal_lock.acquire()
                    try:
                        if id_ not in self.__proxies:
                            event = self.__pending_proxies.get(id_, None)
                            if event is None:
                                event = threading.Event()
                                self.__pending_proxies[id_] = event
                    finally:
                        self.__unmarshal_lock.release()

                    # Wait for the event to be set.
                    if id_ not in self.__proxies:
                        event.wait()

                return self.__proxies[id_]
        elif payload.startswith("o"):
            # The object originated here.
            id_ = marshal.loads(buffer(payload, 1))
            return self.__proxied_objects[id_]
        else:
            raise ValueError, "Invalid payload prefix"


    def __register_proxy(self, proxy, remote_id):
        pushy.util.logger.debug(
            "Registering a proxy: %r -> %r", id(proxy), remote_id)
        self.__proxies[remote_id] = proxy
        self.__proxy_ids[id(proxy)] = remote_id


    def __send_message(self, message_type, args):
        m = Message(message_type, self.__marshal(args))
        pushy.util.logger.debug("Sending %r", m)
        bytes = m.pack()
        self.__ostream_lock.acquire()
        try:
            self.__ostream.write(bytes)
            self.__ostream.flush()
        finally:
            self.__ostream_lock.release()
        pushy.util.logger.debug("Sent %r [%d bytes]", message_type, len(bytes))


    def __recv(self):
        pushy.util.logger.debug("Waiting for message")
        self.__istream_lock.acquire()
        try:
            m = Message.unpack(self.__istream)
            pushy.util.logger.debug("Received %r", m.type)
            return m
        finally:
            pushy.util.logger.debug("Receive ended")
            self.__istream_lock.release()


    def __handle(self, m):
        pushy.util.logger.debug("Handling message: %r", m)

        # Track the number of requests being processed in this thread. May be
        # greater than one, if there is to-and-fro. We need to track this so
        # we know when to send a 'syncrequest' message.
        is_request = m.type not in response_types
        if is_request:
            if hasattr(self.__thread_local, "request_count"):
                self.__thread_local.request_count += 1
            else:
                self.__thread_local.request_count = 1

        try:
            try:
                args = self.__unmarshal(m.payload)
                result = self.message_handlers[m.type](m.type, args)
                if m.type not in response_types:
                    self.__send_response(result)
                return result
            except SystemExit, e:
                self.__send_response(e.code)
                raise e
            except Exception, e:
                # An exception raised while handling an exception message
                # should be sent up to the caller.
                if m.type is MessageType.exception:
                    raise e

                # Allow the message receiving thread to proceed.
                self.__processing_condition.acquire()
                try:
                    self.__processing -= 1
                    if self.__processing == 0:
                        self.__processing_condition.notify()
                finally:
                    self.__processing_condition.release()

                # Send the above three objects to the caller
                pushy.util.logger.debug(
                    "Throwing an exception", exc_info=sys.exc_info())
                self.__send_message(MessageType.exception, e)
        finally:
            if is_request:
                self.__thread_local.request_count -= 1


    def __handle_response(self, message_type, result):
        return result


    def __handle_exception(self, message_type, e):
        raise e


    def __handle_syncrequest(self, message_type, args):
        "Synchronous requests (i.e. requests in response to another request.)"
        real_message_type_code, payload = args
        real_message_type = message_types[real_message_type_code]
        pushy.util.logger.debug("Real message type: %r", real_message_type)
        return self.message_handlers[real_message_type](
                   real_message_type, self.__unmarshal(payload))

