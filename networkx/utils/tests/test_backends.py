import pickle

import pytest

import networkx as nx

sp = pytest.importorskip("scipy")
pytest.importorskip("numpy")


def test_dispatch_kwds_vs_args():
    G = nx.path_graph(4)
    nx.pagerank(G)
    nx.pagerank(G=G)
    with pytest.raises(TypeError):
        nx.pagerank()


def test_pickle():
    count = 0
    for name, func in nx.utils.backends._registered_algorithms.items():
        try:
            # Some functions can't be pickled, but it's not b/c of _dispatchable
            pickled = pickle.dumps(func)
        except pickle.PicklingError:
            continue
        assert pickle.loads(pickled) is func
        count += 1
    assert count > 0
    assert pickle.loads(pickle.dumps(nx.inverse_line_graph)) is nx.inverse_line_graph


@pytest.mark.skipif(
    "not nx.config['backend_priority'] "
    "or nx.config['backend_priority'][0] != 'nx-loopback'"
)
def test_graph_converter_needs_backend():
    # When testing, `nx.from_scipy_sparse_array` will *always* call the backend
    # implementation if it's implemented. If `backend=` isn't given, then the result
    # will be converted back to NetworkX via `convert_to_nx`.
    # If not testing, then calling `nx.from_scipy_sparse_array` w/o `backend=` will
    # always call the original version. `backend=` is *required* to call the backend.
    from networkx.classes.tests.dispatch_interface import (
        LoopbackDispatcher,
        LoopbackGraph,
    )

    A = sp.sparse.coo_array([[0, 3, 2], [3, 0, 1], [2, 1, 0]])

    side_effects = []

    def from_scipy_sparse_array(self, *args, **kwargs):
        side_effects.append(1)  # Just to prove this was called
        return self.convert_from_nx(
            self.__getattr__("from_scipy_sparse_array")(*args, **kwargs),
            preserve_edge_attrs=True,
            preserve_node_attrs=True,
            preserve_graph_attrs=True,
        )

    @staticmethod
    def convert_to_nx(obj, *, name=None):
        if type(obj) is nx.Graph:
            return obj
        return nx.Graph(obj)

    # *This mutates LoopbackDispatcher!*
    orig_convert_to_nx = LoopbackDispatcher.convert_to_nx
    LoopbackDispatcher.convert_to_nx = convert_to_nx
    LoopbackDispatcher.from_scipy_sparse_array = from_scipy_sparse_array

    try:
        assert side_effects == []
        assert type(nx.from_scipy_sparse_array(A)) is nx.Graph
        assert side_effects == [1]
        assert (
            type(nx.from_scipy_sparse_array(A, backend="nx-loopback")) is LoopbackGraph
        )
        assert side_effects == [1, 1]
    finally:
        LoopbackDispatcher.convert_to_nx = staticmethod(orig_convert_to_nx)
        del LoopbackDispatcher.from_scipy_sparse_array
    with pytest.raises(ImportError, match="Unable to load"):
        nx.from_scipy_sparse_array(A, backend="bad-backend-name")


def test_dispatchable_are_functions():
    assert type(nx.pagerank) is type(nx.pagerank.orig_func)


def test_bad_backend_name():
    """Using `backend=` raises with unknown backend even if there are no backends."""
    with pytest.raises(ImportError, match="Unable to load"):
        nx.null_graph(backend="this_backend_does_not_exist")
