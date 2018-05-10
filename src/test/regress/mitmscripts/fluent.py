import re
import threading
import queue

from mitmproxy import ctx
from mitmproxy.utils import strutils
from mitmproxy.proxy.protocol import TlsLayer, RawTCPLayer

'''
Use with a command line like this:

mitmdump --rawtcp -p 9702 --mode reverse:localhost:9700 -s fluent.py

You need one more parameter at the end: --set slug='script'
Where script can be of the form:
- flow.contains(b"SELECT").contains(b"COPY").killall()
- flow.matches(b"^Q").after(2).kill()
- flow.copyOutResponse().kill()

Ideas (unimplemented):
- flow.filter(shard=102457).kill()
- packet.contains("SELECT").kill()
- flow.shard("102456").contains("COPY").kill()
- worker.once(and(flow.contains("COPY"),flow.contains("SELECT"))).partition()
- worker.after(packet.contains("COPY")).then(packet.contains("SELECT")).partition()
- worker.after(query="COPY").and(query="SELECT").partition()
- flow.after(query="COPY").then(query="SELECT").do(worker.partition())

Should probably rename killall() -> killworker()

Instead of having _handle and _accept, you should separate out the builders from the
handlers? The builders just configure a handler which is then passed to mitmproxy

What about: wait until .copyOutResponse(), then kill after 5 of any message have passed
'''

class Stop(Exception):
    pass

class Handler:
    '''
    You want to pull from the previous step?
    When given a message, pass it up to your parent and see what they do with it?

    Alternatively, keep track of which node was the root node, then give it the message
    and wait for it to push the message back down to us?
    '''
    def __init__(self, root=None):
        self.root = root if root else self
        self.next = None

    def _accept(self, flow, message):
        result = self._handle(flow, message)

        if result == 'pass':
            # defer to our child
            if not self.next:
                raise Exception("we don't know what to do!")

            try:
                self.next._accept(flow, message)
            except Stop:
                if self.root is not self:
                    raise
                self.next = KillHandler(self)
        elif result == 'done':
            # stop processing this packet, move on to the next one
            return
        elif result == 'stop':
            # kill all connectinos from here on out
            raise Stop()

    def _handle(self, flow, message):
        return 'pass'

class FilterableMixin:
    def contains(self, pattern):
        self.next = Contains(self.root, pattern)
        return self.next

    def matches(self, pattern):
        self.next = Matches(self.root, pattern)
        return self.next

    def after(self, times):
        self.next = After(self.root, times)
        return self.next

    def copyOutResponse(self):
        self.next = Matches(self.root, b"^H")
        return self.next

class ActionsMixin:
    def kill(self):
        self.next = KillHandler(self.root)
        return self.next

    def allow(self):
        self.next = AcceptHandler(self.root)
        return self.next

    def killall(self):
        self.next = KillAllHandler(self.root)
        return self.next

class AcceptHandler(Handler):
    def __init__(self, root):
        super().__init__(root)
    def _handle(self, flow, message):
        return 'done'

class KillHandler(Handler):
    def __init__(self, root):
        super().__init__(root)
    def _handle(self, flow, message):
        flow.kill()
        return 'done'

class KillAllHandler(Handler):
    def __init__(self, root):
        super().__init__(root)
    def _handle(self, flow, message):
        return 'stop'

class Contains(Handler, ActionsMixin, FilterableMixin):
    def __init__(self, root, pattern):
        super().__init__(root)
        self.pattern = pattern

    def _handle(self, flow, message):
        if self.pattern in message.content:
            return 'pass'
        return 'done'

class Matches(Handler, ActionsMixin, FilterableMixin):
    def __init__(self, root, pattern):
        super().__init__(root)
        self.pattern = re.compile(pattern)

    def _handle(self, flow, message):
        if self.pattern.search(message.content):
            return 'pass'
        return 'done'

class After(Handler, ActionsMixin, FilterableMixin):
    "Don't pass execution to our child until we've handled 'times' messages"
    def __init__(self, root, times):
        super().__init__(root)
        self.target = times

    def _handle(self, flow, message):
        if not hasattr(flow, '_after_count'):
            flow._after_count = 0

        if flow._after_count >= self.target:
            return 'pass'

        flow._after_count += 1
        return 'done'

class RootHandler(Handler, ActionsMixin, FilterableMixin):
    pass

class RecorderCommand:
    def __init__(self):
        self.root = self
        self.command = None

    def dump(self):
        # When the user calls dump() we return everything we've captured
        self.command = 'dump'
        return self

    def reset(self):
        # If the user calls reset() we dump all captured packets without returning them
        self.command = 'reset'
        return self

# helper functions

def build_handler(spec):
    root = RootHandler()
    recorder = RecorderCommand()
    handler = eval(spec, {'__builtins__': {}}, {'flow': root, 'recorder': recorder})
    return handler.root

def print_message(tcp_msg):
    print("[message] from {} to {}:\r\n{}".format(
        "client" if tcp_msg.from_client else "server",
        "server" if tcp_msg.from_client else "client",
        strutils.bytes_to_escaped_str(tcp_msg.content)
    ))

# thread which listens for commands

handler = None
command_thread = None
command_queue = queue.Queue()
response_queue = queue.Queue()
captured_messages = queue.Queue()

def listen_for_commands(fifoname):
    def handle_recorder(recorder):
        result = ''

        if recorder.command is 'reset':
            result = ''
        elif recorder.command is not 'dump':
            # this should never happen
            raise Exception('Unrecognized command: {}'.format(recorder.command))

        try:
            while True:
                message = captured_messages.get(block=False)
                if recorder.command is 'reset':
                    continue
                result += "\nmessage from {}: {}".format(
                    "client" if message.from_client else "server",
                    strutils.bytes_to_escaped_str(message.content)
                ).replace('\\', '\\\\') # this last replace is for COPY's sake
        except queue.Empty:
            pass

        with open(fifoname, mode='w') as fifo:
            fifo.write('{}\n'.format(result))

    while True:
        with open(fifoname, mode='r') as fifo:
            slug = fifo.read()

        try:
            handler = build_handler(slug)
            if isinstance(handler, RecorderCommand):
                handle_recorder(handler)
                continue
        except Exception as e:
            result = str(e)
        else:
            result = None

        if not result:
            command_queue.put(slug)
            result = response_queue.get()

        with open(fifoname, mode='w') as fifo:
            fifo.write('{}\n'.format(result))

# callbacks for mitmproxy

def replace_thread(fifoname):
    global command_thread

    if not fifoname:
        return
    if not len(fifoname):
        return

    if command_thread:
        print('cannot change the fifo path once mitmproxy has started');
        return

    command_thread = threading.Thread(target=listen_for_commands, args=(fifoname,), daemon=True)
    command_thread.start()


def load(loader):
    loader.add_option('slug', str, 'flow.allow()', "A script to run")
    loader.add_option('fifo', str, '', "Which fifo to listen on for commands")


def tick():
    # we do this dance because ctx isn't threadsafe, it is only set while a handler is
    # being called.
    try:
        slug = command_queue.get_nowait()
    except queue.Empty:
        return

    try:
        ctx.options.update(slug=slug)
    except Exception as e:
        response_queue.put(str(e))
    else:
        response_queue.put('')


def configure(updated):
    global handler

    if 'slug' in updated:
        text = ctx.options.slug
        print('using script: {}'.format(text))
        handler = build_handler(text)

    if 'fifo' in updated:
        fifoname = ctx.options.fifo
        print('using fifo: {}'.format(fifoname))
        replace_thread(fifoname)


def next_layer(layer):
    '''
    mitmproxy wasn't really meant for intercepting raw tcp streams, it tries to wrap the
    upsteam connection (the one to the worker) in a tls stream. This hook intercepts the
    part where it creates the TlsLayer (it happens in root_context.py) and instead creates
    a RawTCPLayer. That's the layer which calls our tcp_message hook
    '''
    if isinstance(layer, TlsLayer):
        replacement = RawTCPLayer(layer.ctx)
        layer.reply.send(replacement)


def tcp_message(flow):
    tcp_msg = flow.messages[-1]

    captured_messages.put(tcp_msg)
    print_message(tcp_msg)
    handler._accept(flow, tcp_msg)
    return
