"""
Code to support various backends in a plugin dispatch architecture.

Create a Dispatcher
-------------------

To be a valid plugin, a package must register an entry_point
of `networkx.plugins` with a key pointing to the handler.

For example::

    entry_points={'networkx.plugins': 'sparse = networkx_plugin_sparse'}

The plugin must create a Graph-like object which contains an attribute
``__networkx_plugin__`` with a value of the entry point name.

Continuing the example above::

    class WrappedSparse:
        __networkx_plugin__ = "sparse"
        ...

When a dispatchable NetworkX algorithm encounters a Graph-like object
with a ``__networkx_plugin__`` attribute, it will look for the associated
dispatch object in the entry_points, load it, and dispatch the work to it.


Testing
-------
To assist in validating the backend algorithm implementations, if an
environment variable ``NETWORKX_GRAPH_CONVERT`` is set to a registered
plugin keys, the dispatch machinery will automatically convert regular
networkx Graphs and DiGraphs to the backend equivalent by calling
``<backend dispatcher>.convert_from_nx(G, edge_attrs=edge_attrs, name=name)``.

The arguments to ``convert_from_nx`` are:

- ``G`` : networkx Graph
- ``edge_attrs`` : dict, optional
    Dict that maps edge attributes to default values if missing in ``G``.
    If None, then no edge attributes will be converted and default may be 1.
- ``node_attrs``: dict, optional
    Dict that maps node attribute to default values if missing in ``G``.
    If None, then no node attributes will be converted.
- ``preserve_edge_attrs`` : bool
    Whether to preserve all edge attributes.
- ``preserve_node_attrs`` : bool
    Whether to preserve all node attributes.

The converted object is then passed to the backend implementation of
the algorithm. The result is then passed to
``<backend dispatcher>.convert_to_nx(result, name=name)`` to convert back
to a form expected by the NetworkX tests.

By defining ``convert_from_nx`` and ``convert_to_nx`` methods and setting
the environment variable, NetworkX will automatically route tests on
dispatchable algorithms to the backend, allowing the full networkx test
suite to be run against the backend implementation.

Example pytest invocation::

    NETWORKX_GRAPH_CONVERT=sparse pytest --pyargs networkx

Dispatchable algorithms which are not implemented by the backend
will cause a ``pytest.xfail()``, giving some indication that not all
tests are working, while avoiding causing an explicit failure.

A special ``on_start_tests(items)`` function may be defined by the backend.
It will be called with the list of NetworkX tests discovered. Each item
is a test object that can be marked as xfail if the backend does not support
the test using `item.add_marker(pytest.mark.xfail(reason=...))`.
"""
import functools
import inspect
import os
import sys
from importlib.metadata import entry_points

from ..exception import NetworkXNotImplemented

__all__ = ["_dispatch", "_mark_tests"]


class PluginInfo:
    """Lazily loaded entry_points plugin information"""

    def __init__(self):
        self._items = None

    def __bool__(self):
        return len(self.items) > 0

    @property
    def items(self):
        if self._items is None:
            if sys.version_info < (3, 10):
                self._items = entry_points()["networkx.plugins"]
            else:
                self._items = entry_points(group="networkx.plugins")
        return self._items

    def __contains__(self, name):
        if sys.version_info < (3, 10):
            return len([ep for ep in self.items if ep.name == name]) > 0
        return name in self.items.names

    def __getitem__(self, name):
        if sys.version_info < (3, 10):
            return [ep for ep in self.items if ep.name == name][0]
        return self.items[name]


plugins = PluginInfo()
_registered_algorithms = {}


def _register_algo(name, wrapped_func):
    if name in _registered_algorithms:
        raise KeyError(f"Algorithm already exists in dispatch registry: {name}")
    _registered_algorithms[name] = wrapped_func
    wrapped_func.dispatchname = name


def _dispatch(
    func=None,
    *,
    name=None,
    graphs="G",
    edge_attrs=None,
    node_attrs=None,
    preserve_edge_attrs=None,
    preserve_node_attrs=None,
):
    """Dispatches to a backend algorithm based on input graph types.

    Parameters
    ----------
    func : function

    name : str, optional
        The name of the algorithm to use for dispatching. If not provided,
        the name of ``func`` will be used. ``name`` is useful to avoid name
        conflicts, as all dispatched algorithms live in a single namespace.

    graphs : str or dict, default "G"
        If a string, the parameter name of the graph, which must be the first
        argument of the wrapped function. If more than one graph is required
        for the algorithm (or if the graph is not the first argument), provide
        a dict of parameter name to argument position for each graph argument.
        For example, ``@_dispatch(graphs={"G": 0, "auxiliary?": 4})``
        indicates the 0th parameter ``G`` of the function is a required graph,
        and the 4th parameter ``auxiliary`` is an optional graph.

    edge_attrs : str or dict, optional
        ``edge_attrs`` may be strings of the function argument that indicate
        a single edge attribute to use to convert the graph. They may also be
        dicts that map argument name of attributes to argument name of default
        values. The default value may be a non-string value such as 0.
        If not provided, the default value is assumed to be 1.

    node_attrs : str or dict, optional
        Like ``edge_attrs``, but for node attributes.

    preserve_edge_attrs : str or bool, optional
        If a string, the parameter name that may indicate (with ``True``)
        whether all edge attributes should be preserved when converting.
        If ``preserve_edge_attrs`` itself is ``True``, then always preserve
        edge attributes. When all edges are to be preserved, ``edge_attrs``
        is ignored.

    preserve_node_attrs : str or bool, optional
        Like ``preserve_edge_attrs``, but for node attributes.

    """
    # Allow any of the following decorator forms:
    #  - @_dispatch
    #  - @_dispatch()
    #  - @_dispatch(name="override_name")
    #  - @_dispatch(graphs="graph")
    #  - @_dispatch(edge_attrs="weight")
    #  - @_dispatch(graphs={"G": 0, "H": 1}, edge_attrs={"weight": "default"})
    if func is None:
        if (
            name is None
            and graphs == "G"
            and edge_attrs is None
            and node_attrs is None
            and preserve_edge_attrs is None
            and preserve_node_attrs is None
        ):
            return _dispatch
        return functools.partial(
            _dispatch,
            name=name,
            graphs=graphs,
            edge_attrs=edge_attrs,
            node_attrs=node_attrs,
            preserve_edge_attrs=preserve_edge_attrs,
            preserve_node_attrs=preserve_node_attrs,
        )
    if isinstance(func, str):
        raise TypeError("'name' and 'graphs' must be passed by keyword") from None
    # If name not provided, use the name of the function
    if name is None:
        name = func.__name__

    if isinstance(graphs, str):
        graphs = {graphs: 0}
    elif len(graphs) == 0:
        raise KeyError("'graphs' must contain at least one variable name") from None

    # This dict comprehension is complicated for better performance; equivalent shown below.
    optional_graphs = set()
    graphs = {
        optional_graphs.add(val := k[:-1]) or val if k[-1] == "?" else k: v
        for k, v in graphs.items()
    }
    # The above is equivalent to:
    # optional_graphs = {k[:-1] for k in graphs if k[-1] == "?"}
    # graphs = {k[:-1] if k[-1] == "?" else k: v for k, v in graphs.items()}

    @functools.wraps(func)
    def wrapper(*args, **kwds):
        graphs_resolved = {}
        for gname, pos in graphs.items():
            if pos < len(args):
                if gname in kwds:
                    raise TypeError(
                        f"{name}() got multiple values for {gname!r}"
                    ) from None
                val = args[pos]
            elif gname in kwds:
                val = kwds[gname]
            elif gname not in optional_graphs:
                raise TypeError(
                    f"{name}() missing required graph argument: {gname}"
                ) from None
            else:
                continue
            if val is None:
                if gname not in optional_graphs:
                    raise TypeError(
                        f"{name}() required graph argument {gname!r} is None; must be a graph"
                    ) from None
            else:
                graphs_resolved[gname] = val

        # Alternative to the above that does not check duplicated args or missing required graphs.
        # graphs_resolved = {
        #     val
        #     for gname, pos in graphs.items()
        #     if (val := args[pos] if pos < len(args) else kwds.get(gname)) is not None
        # }

        # Check if any graph comes from a plugin
        if any(hasattr(g, "__networkx_plugin__") for g in graphs_resolved.values()):
            # Find common plugin name
            plugin_names = {
                getattr(g, "__networkx_plugin__", "networkx")
                for g in graphs_resolved.values()
            }
            if len(plugin_names) != 1:
                raise TypeError(
                    f"{name}() graphs must all be from the same plugin, found {plugin_names}"
                ) from None
            [plugin_name] = plugin_names
            if plugin_name not in plugins:
                raise ImportError(f"Unable to load plugin: {plugin_name}") from None
            backend = plugins[plugin_name].load()
            if hasattr(backend, name):
                return getattr(backend, name).__call__(*args, **kwds)
            else:
                raise NetworkXNotImplemented(
                    f"'{name}' not implemented by {plugin_name}"
                )
        return func(*args, **kwds)

    _register_algo(name, wrapper)
    return wrapper


def test_override_dispatch(
    func=None,
    *,
    name=None,
    graphs="G",
    edge_attrs=None,
    node_attrs=None,
    preserve_edge_attrs=None,
    preserve_node_attrs=None,
):
    """Auto-converts graph arguments into the backend equivalent,
    causing the dispatching mechanism to trigger for every
    decorated algorithm."""
    if func is None:
        if (
            name is None
            and graphs == "G"
            and edge_attrs is None
            and node_attrs is None
            and preserve_edge_attrs is None
            and preserve_node_attrs is None
        ):
            return _dispatch
        return functools.partial(
            _dispatch,
            name=name,
            graphs=graphs,
            edge_attrs=edge_attrs,
            node_attrs=node_attrs,
            preserve_edge_attrs=preserve_edge_attrs,
            preserve_node_attrs=preserve_node_attrs,
        )
    if isinstance(func, str):
        raise TypeError("'name' and 'graphs' must be passed by keyword") from None
    # If name not provided, use the name of the function
    if name is None:
        name = func.__name__

    if isinstance(graphs, str):
        graphs = {graphs: 0}
    elif len(graphs) == 0:
        raise KeyError("'graphs' must contain at least one variable name") from None

    # This dict comprehension is complicated for better performance; equivalent shown below.
    optional_graphs = set()
    graphs = {
        optional_graphs.add(val := k[:-1]) or val if k[-1] == "?" else k: v
        for k, v in graphs.items()
    }
    # The above is equivalent to:
    # optional_graphs = {k[:-1] for k in graphs if k[-1] == "?"}
    # graphs = {k[:-1] if k[-1] == "?" else k: v for k, v in graphs.items()}

    sig = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args, **kwds):
        backend = plugins[plugin_name].load()
        if not hasattr(backend, name):
            if plugin_name == "nx-loopback":
                raise NetworkXNotImplemented(
                    f"'{name}' not found in {backend.__class__.__name__}"
                )
            pytest.xfail(f"'{name}' not implemented by {plugin_name}")
        bound = sig.bind(*args, **kwds)
        bound.apply_defaults()
        # Check that graph names are actually in the signature
        if set(graphs) - set(bound.arguments):
            raise KeyError(f"Invalid graph names: {set(graphs) - set(bound.arguments)}")
        # Convert graphs into backend graph-like object
        #   Include the edge and/or node labels if provided to the algorithm
        _preserve_edge_attrs = preserve_edge_attrs
        _edge_attrs = edge_attrs
        if _preserve_edge_attrs is None:
            _preserve_edge_attrs = False
        elif _preserve_edge_attrs is True:
            _edge_attrs = None
        elif (
            isinstance(_preserve_edge_attrs, str)
            and bound.arguments[_preserve_edge_attrs] is True
        ):
            _edge_attrs = None
            _preserve_edge_attrs = True
        else:
            _preserve_edge_attrs = False
        if _edge_attrs is None:
            pass
        elif isinstance(_edge_attrs, str):
            if _edge_attrs[0] == "[":
                _edge_attrs = {
                    edge_attr: 1 for edge_attr in bound.arguments[_edge_attrs[1:-1]]
                }
            else:
                _edge_attrs = {bound.arguments[_edge_attrs]: 1}
        elif isinstance(_edge_attrs, dict):
            _edge_attrs = {
                bound.arguments[key]: bound.arguments.get(val, 1)
                if isinstance(val, str)
                else val
                for key, val in _edge_attrs.items()
            }
        else:
            raise RuntimeError(f"bad type for edge_attrs: {type(edge_attrs)}")

        _preserve_node_attrs = preserve_node_attrs
        _node_attrs = node_attrs
        if _preserve_node_attrs is None:
            _preserve_node_attrs = False
        elif _preserve_node_attrs is True:
            _node_attrs = None
        elif (
            isinstance(_preserve_node_attrs, str)
            and bound.arguments[_preserve_node_attrs] is True
        ):
            _node_attrs = None
            _preserve_node_attrs = True
        else:
            _preserve_node_attrs = False
        if _node_attrs is None:
            pass
        elif isinstance(_node_attrs, str):
            if _node_attrs[0] == "[":
                _node_attrs = {
                    node_attr: None for node_attr in bound.arguments[_node_attrs[1:-1]]
                }
            else:
                _node_attrs = {bound.arguments[_node_attrs]: None}
        elif isinstance(_node_attrs, dict):
            _node_attrs = {
                bound.arguments[key]: bound.arguments.get(val)
                if isinstance(val, str)
                else val
            }
        else:
            raise RuntimeError(f"bad type for node_attrs: {type(node_attrs)}")

        for gname in graphs:
            bound.arguments[gname] = backend.convert_from_nx(
                bound.arguments[gname],
                edge_attrs=_edge_attrs,
                node_attrs=_node_attrs,
                preserve_edge_attrs=_preserve_edge_attrs,
                preserve_node_attrs=_preserve_node_attrs,
                name=name,
            )
        result = getattr(backend, name).__call__(**bound.arguments)
        return backend.convert_to_nx(result, name=name)

    _register_algo(name, wrapper)
    return wrapper


# Check for auto-convert testing
# This allows existing NetworkX tests to be run against a backend
# implementation without any changes to the testing code. The only
# required change is to set an environment variable prior to running
# pytest.
if os.environ.get("NETWORKX_GRAPH_CONVERT"):
    plugin_name = os.environ["NETWORKX_GRAPH_CONVERT"]
    if not plugins:
        raise Exception("No registered networkx.plugins entry_points")
    if plugin_name not in plugins:
        raise Exception(
            f"No registered networkx.plugins entry_point named {plugin_name}"
        )

    try:
        import pytest
    except ImportError:
        raise ImportError(
            f"Missing pytest, which is required when using NETWORKX_GRAPH_CONVERT"
        )

    # Override `dispatch` for testing
    _dispatch = test_override_dispatch


def _mark_tests(items):
    """Allow backend to mark tests (skip or xfail) if they aren't able to correctly handle them"""
    if os.environ.get("NETWORKX_GRAPH_CONVERT"):
        plugin_name = os.environ["NETWORKX_GRAPH_CONVERT"]
        backend = plugins[plugin_name].load()
        if hasattr(backend, "on_start_tests"):
            getattr(backend, "on_start_tests")(items)