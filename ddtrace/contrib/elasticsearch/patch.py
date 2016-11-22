import elasticsearch
import wrapt

from . import metadata
from .quantize import quantize

from ...compat import json, urlencode
from ...pin import Pin


DEFAULT_SERVICE = 'elasticsearch'
SPAN_TYPE = 'elasticsearch'

# Original Elasticsearch class
_Elasticsearch = elasticsearch.Elasticsearch


def patch():
    setattr(elasticsearch, 'Elasticsearch', TracedElasticsearch)

def unpatch():
    setattr(elasticsearch, 'Elasticsearch', _Elasticsearch)


class TracedElasticsearch(wrapt.ObjectProxy):
    """Traced Elasticsearch object

    Consists in patching the transport.perform_request method and keeping reference of the pin.
    """

    def __init__(self, *args, **kwargs):
        es = _Elasticsearch(*args, **kwargs)
        super(TracedElasticsearch, self).__init__(es)

        pin = Pin.new(service=DEFAULT_SERVICE, app="elasticsearch", app_type="db")
        pin.onto(self)

        wrapt.wrap_function_wrapper(es.transport, 'perform_request', _perform_request)

    def __setddpin__(self, pin):
        """Attach the Pin to the wrapped transport instance

        Since that's where we create the spans.
        """
        pin.onto(self.__wrapped__.transport)

    def __getddpin__(self):
        """Get the Pin from the wrapped transport instance"""
        return Pin.get_from(self.__wrapped__.transport)


def _perform_request(func, instance, args, kwargs):
    pin = Pin.get_from(instance)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    with pin.tracer.trace("elasticsearch.query") as span:
        # Don't instrument if the trace is not sampled
        if not span.sampled:
            return func(*args, **kwargs)

        method, url = args
        params = kwargs.get('params')
        body = kwargs.get('body')

        span.service = pin.service
        span.span_type = SPAN_TYPE
        span.set_tag(metadata.METHOD, method)
        span.set_tag(metadata.URL, url)
        span.set_tag(metadata.PARAMS, urlencode(params))
        if method == "GET":
            span.set_tag(metadata.BODY, json.dumps(body))

        span = quantize(span)

        result = func(*args, **kwargs)

        try:
            # Optional metadata extraction with soft fail.
            if isinstance(result, tuple) and len(result) == 2:
                # elasticsearch<2.4; it returns both the status and the body
                _, data = result
            else:
                # elasticsearch>=2.4; internal change for ``Transport.perform_request``
                # that just returns the body
                data = result

            took = data.get("took")
            if took:
                span.set_metric(metadata.TOOK, int(took))
        except Exception:
            pass

        return result
