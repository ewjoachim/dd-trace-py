# stdlib
import logging
import time
import ddtrace
from json import loads

# project
from .encoding import get_encoder, JSONEncoder
from .compat import httplib, PYTHON_VERSION, PYTHON_INTERPRETER, get_connection_response


log = logging.getLogger(__name__)

TRACE_COUNT_HEADER = 'X-Datadog-Trace-Count'

_VERSIONS = {'v0.4': {'traces': '/v0.4/traces',
                      'services': '/v0.4/services',
                      'compatibility_mode': False,
                      'fallback': 'v0.3'},
             'v0.3': {'traces': '/v0.3/traces',
                      'services': '/v0.3/services',
                      'compatibility_mode': False,
                      'fallback': 'v0.2'},
             'v0.2': {'traces': '/v0.2/traces',
                      'services': '/v0.2/services',
                      'compatibility_mode': True,
                      'fallback': None}}


class API(object):
    """
    Send data to the trace agent using the HTTP protocol and JSON format
    """
    class Response(object):
        """
        Custom API Response object to represent a response from calling the API.

        We do this to ensure we know expected properties will exist, and so we
        can call `resp.read()` and load the body once into an instance before we
        close the HTTPConnection used for the request.

        DEV: Calling `HTTPConnection.close()` before `HTTPResponse.read()` will result in
             `.read()` always returning back `b''`
        """
        __slots__ = ['status', 'body']

        def __init__(self, status=None, body=None):
            self.status = status
            self.body = body

        @classmethod
        def from_http_response(cls, resp):
            return cls(status=resp.status, body=resp.read())

        def get_json(self):
            """Helper to parse the body of this request as JSON"""
            try:
                body = self.body
                if not body:
                    log.debug('Empty reply from trace-agent, %r', self)
                    return

                if not isinstance(body, str) and hasattr(body, 'decode'):
                    body = body.decode('utf-8')

                if hasattr(body, 'startswith') and body.startswith('OK'):
                    # This typically happens when using a priority-sampling enabled
                    # library with an outdated agent. It still works, but priority sampling
                    # will probably send too many traces, so the next step is to upgrade agent.
                    log.debug("'OK' is not a valid JSON, please make sure trace-agent is up to date")
                    return

                return loads(body)
            except (ValueError, TypeError) as err:
                log.debug("unable to load JSON '%s': %s" % (body, err))

        def __repr__(self):
            return 'API.Response(status={0!r}, body={1!r})'.format(self.status, self.body)

    def __init__(self, hostname, port, headers=None, encoder=None, priority_sampling=False):
        self.hostname = hostname
        self.port = port

        self._headers = headers or {}
        self._version = None

        if priority_sampling:
            self._set_version('v0.4', encoder=encoder)
        else:
            self._set_version('v0.3', encoder=encoder)

        self._headers.update({
            'Datadog-Meta-Lang': 'python',
            'Datadog-Meta-Lang-Version': PYTHON_VERSION,
            'Datadog-Meta-Lang-Interpreter': PYTHON_INTERPRETER,
            'Datadog-Meta-Tracer-Version': ddtrace.__version__,
        })

    def _set_version(self, version, encoder=None):
        if version not in _VERSIONS:
            version = 'v0.2'
        if version == self._version:
            return
        self._version = version
        self._traces = _VERSIONS[version]['traces']
        self._services = _VERSIONS[version]['services']
        self._fallback = _VERSIONS[version]['fallback']
        self._compatibility_mode = _VERSIONS[version]['compatibility_mode']
        if self._compatibility_mode:
            self._encoder = JSONEncoder()
        else:
            self._encoder = encoder or get_encoder()
        # overwrite the Content-type with the one chosen in the Encoder
        self._headers.update({'Content-Type': self._encoder.content_type})

    def _downgrade(self):
        """
        Downgrades the used encoder and API level. This method must fallback to a safe
        encoder and API, so that it will success despite users' configurations. This action
        ensures that the compatibility mode is activated so that the downgrade will be
        executed only once.
        """
        self._set_version(self._fallback)

    def send_traces(self, traces):
        if not traces:
            return
        start = time.time()
        data = self._encoder.encode_traces(traces)
        response = self._put(self._traces, data, len(traces))

        # the API endpoint is not available so we should downgrade the connection and re-try the call
        if response.status in [404, 415] and self._fallback:
            log.debug('calling endpoint "%s" but received %s; downgrading API', self._traces, response.status)
            self._downgrade()
            return self.send_traces(traces)

        log.debug("reported %d traces in %.5fs", len(traces), time.time() - start)
        return response

    def send_services(self, services):
        if not services:
            return
        s = {}
        for service in services:
            s.update(service)
        data = self._encoder.encode_services(s)
        response = self._put(self._services, data)

        # the API endpoint is not available so we should downgrade the connection and re-try the call
        if response.status in [404, 415] and self._fallback:
            log.debug('calling endpoint "%s" but received %s; downgrading API', self._services, response.status)
            self._downgrade()
            return self.send_services(services)

        log.debug("reported %d services", len(services))
        return response

    def _put(self, endpoint, data, count=0):
        conn = httplib.HTTPConnection(self.hostname, self.port)
        try:
            headers = self._headers
            if count:
                headers = dict(self._headers)
                headers[TRACE_COUNT_HEADER] = str(count)

            conn.request("PUT", endpoint, data, headers)

            # Parse the HTTPResponse into a API.Response
            # DEV: This will call `resp.read()` which must happen `conn.close()` below,
            #      otherwise the if we `.close()` then all `.read()` calls will return `b''`
            resp = get_connection_response(conn)
            return API.Response.from_http_response(resp)
        finally:
            conn.close()
