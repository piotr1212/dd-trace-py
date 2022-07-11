import os
import re
import sys
from typing import TYPE_CHECKING

import graphql
from graphql import MiddlewareManager
from graphql.error import GraphQLError
from graphql.execution import ExecutionResult
from graphql.language.source import Source

from ddtrace import config
from ddtrace.constants import ANALYTICS_SAMPLE_RATE_KEY
from ddtrace.constants import SPAN_MEASURED_KEY
from ddtrace.internal.compat import stringify
from ddtrace.internal.utils import ArgumentError
from ddtrace.internal.utils import get_argument_value
from ddtrace.internal.utils import set_argument_value
from ddtrace.internal.utils.formats import asbool
from ddtrace.internal.utils.version import parse_version
from ddtrace.internal.wrapping import unwrap
from ddtrace.internal.wrapping import wrap
from ddtrace.pin import Pin

from .. import trace_utils
from ...ext import SpanTypes


if TYPE_CHECKING:
    from typing import Callable
    from typing import Dict
    from typing import Iterable
    from typing import List
    from typing import Tuple
    from typing import Union

    from ddtrace import Span


_graphql_version_str = getattr(graphql, "__version__")
graphql_version = parse_version(_graphql_version_str)

if graphql_version < (3, 0):
    from graphql.language.ast import Document
else:
    from graphql.language.ast import DocumentNode as Document

config._add("graphql", dict(_default_service="graphql"))


def _graphql_resolvers_enabled():
    """
    Patching graphql resolvers may produce large traces.
    DD_TRACE_GRAPHQL_RESOLVERS_ENABLED is disabled by default.
    """
    return asbool(os.getenv("DD_TRACE_GRAPHQL_RESOLVERS_ENABLED", default=False))


def patch():
    if getattr(graphql, "_datadog_patch", False) or graphql_version < (2, 0):
        return
    setattr(graphql, "_datadog_patch", True)
    Pin().onto(graphql)

    if graphql_version < (3, 0):
        to_patch = [
            ("graphql.graphql", "execute_graphql", _traced_query),
            ("graphql.language.parser", "parse", _traced_parse),
            ("graphql.validation.validation", "validate", _traced_validate),
            ("graphql.execution.executor", "execute", _traced_execute),
        ]
    else:
        to_patch = [
            ("graphql.graphql", "graphql_impl", _traced_query),
            ("graphql.language.parser", "parse", _traced_parse),
            ("graphql.validation.validate", "validate", _traced_validate),
            ("graphql.execution.execute", "execute", _traced_execute),
        ]

    for module_str, func_str, wrapper in to_patch:
        module = sys.modules[module_str]
        func = getattr(module, func_str)
        # wrap(...) updates the behavior of the original function.
        # All references to the function will be patched.
        wrap(func, wrapper)


def unpatch():
    if not getattr(graphql, "_datadog_patch", False) or graphql_version < (2, 0):
        return

    if graphql_version < (3, 0):
        patched = [
            ("graphql.graphql", "execute_graphql", _traced_query),
            ("graphql.language.parser", "parse", _traced_parse),
            ("graphql.validation.validation", "validate", _traced_validate),
            ("graphql.execution.executor", "execute", _traced_execute),
        ]
    else:
        patched = [
            ("graphql.graphql", "graphql_impl", _traced_query),
            ("graphql.language.parser", "parse", _traced_parse),
            ("graphql.validation.validate", "validate", _traced_validate),
            ("graphql.execution.execute", "execute", _traced_execute),
        ]

    for module_str, patch_method, wrapper in patched:
        module = sys.modules[module_str]
        func = getattr(module, patch_method)
        unwrap(func, wrapper)

    setattr(graphql, "_datadog_patch", False)


def _traced_parse(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    with pin.tracer.trace(
        name="graphql.parse",
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        _init_span(span)
        return func(*args, **kwargs)


def _traced_validate(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    with pin.tracer.trace(
        name="graphql.validate",
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        _init_span(span)
        errors = func(*args, **kwargs)
        _set_span_errors(errors, span)
        return errors


def _traced_execute(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    if _graphql_resolvers_enabled():
        # patch resolvers
        middleware = _get_middleware_func(pin)
        args, kwargs = _inject_trace_middleware_to_args(middleware, args, kwargs)

    # set resource name
    if graphql_version < (3, 0):
        document = get_argument_value(args, kwargs, 1, "document_ast")
    else:
        document = get_argument_value(args, kwargs, 1, "document")
    resource = _get_source_str(document)

    with pin.tracer.trace(
        name="graphql.execute",
        resource=resource,
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        _init_span(span)
        result = func(*args, **kwargs)
        if isinstance(result, ExecutionResult):
            # set error tags if the result contains a list of GraphqlErrors, skip if it's a promise
            # TODO: support promises in graphql-core==2
            _set_span_errors(result.errors, span)
        return result


def _traced_query(func, args, kwargs):
    pin = Pin.get_from(graphql)
    if not pin or not pin.enabled():
        return func(*args, **kwargs)

    # set resource name
    source = get_argument_value(args, kwargs, 1, "source")
    resource = _get_source_str(source)

    with pin.tracer.trace(
        name="graphql.query",
        resource=resource,
        service=trace_utils.int_service(pin, config.graphql),
        span_type=SpanTypes.GRAPHQL,
    ) as span:
        _init_span(span)
        result = func(*args, **kwargs)
        if isinstance(result, ExecutionResult):
            # set error tags if the result contains a list of GraphqlErrors, skip if it's a promise
            _set_span_errors(result.errors, span)
        return result


def _init_span(span):
    # type: (Span) -> None
    """mark span as measured and set sample rate"""
    span.set_tag(SPAN_MEASURED_KEY)
    sample_rate = config.graphql.get_analytics_sample_rate()
    if sample_rate is not None:
        span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, sample_rate)


def _get_middleware_func(pin):
    # type: (Pin) -> Callable
    def resolver_middleware(next_middleware, root, info, **args):
        """
        trace middleware which wraps the resolvers of graphql fields.
        Note - graphql middlewares can not be a partial. It must be a class or a function.
        """
        with pin.tracer.trace(
            name="graphql.resolve",
            resource=info.field_name,
            service=trace_utils.int_service(pin, config.graphql),
            span_type=SpanTypes.GRAPHQL,
        ):
            return next_middleware(root, info, **args)

    return resolver_middleware


def _inject_trace_middleware_to_args(trace_middleware, args, kwargs):
    # type: (Callable, Tuple, Dict) -> Tuple[Tuple, Dict]
    """
    Adds a trace middleware to graphql.execute(..., middleware, ...)
    """
    middlewares_arg = 8
    if graphql_version >= (3, 2):
        # middleware is the 10th argument graphql.execute(..) version 3.2+
        middlewares_arg = 9

    # get middlewares from args or kwargs
    try:
        middlewares = get_argument_value(args, kwargs, middlewares_arg, "middleware") or []
        # if middleware is a MiddlewareManager get iterable from field
        if isinstance(middlewares, MiddlewareManager):
            middlewares = middlewares.middlewares  # type: Iterable
    except ArgumentError:
        middlewares = []

    # add trace_middleware to the list of execute middlewares
    middlewares = [trace_middleware] + list(middlewares)

    # update args and kwargs to contain trace_middleware
    args, kwargs = set_argument_value(args, kwargs, middlewares_arg, "middleware", middlewares)
    return args, kwargs


def _get_source_str(obj):
    # type: (Union[str, Source, Document]) -> str
    """
    Parses graphql Documents and Source objects to retrieve
    the graphql source input for a request.
    """
    if isinstance(obj, str):
        source_str = obj
    elif isinstance(obj, Source):
        source_str = obj.body
    elif isinstance(obj, Document):
        source_str = obj.loc.source.body
    else:
        source_str = ""
    # remove new lines, tabs and extra whitespace from source_str
    return re.sub(r"\s+", " ", source_str).strip()


def _set_span_errors(errors, span):
    # type: (List[GraphQLError], Span) -> None
    if not errors:
        # do nothing if the list of graphql errors is empty
        return

    error_msgs = "\n".join([stringify(error) for error in errors])
    span.set_exc_fields(GraphQLError, error_msgs, "")
