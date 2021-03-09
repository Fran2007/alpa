from functools import wraps, partial
from collections import OrderedDict

import numpy as np

import jax
from jax import linear_util as lu
from jax.api_util import (
    shaped_abstractify,
    flatten_fun,
    flatten_axes,
    flatten_fun_nokwargs,
    argnums_partial,
)
from jax.config import flags, config, bool_env
from jax.core import ShapedArray
from jax.experimental.maps import mesh
from jax.experimental.pjit import pjit
from jax.interpreters import xla, partial_eval as pe
from jax.interpreters.pxla import parallel_callable, mesh_callable, Mesh
from jax.interpreters.sharded_jit import PartitionSpec
from jax.lib import xla_bridge as xb, xla_client as xc
from jax.tree_util import tree_flatten, tree_unflatten, tree_map
from jax._src.util import (
    unzip2,
    curry,
    partial,
    safe_map,
    safe_zip,
    prod,
    split_list,
    extend_name_stack,
    wrap_name,
    cache,
    wraps,
    HashableFunction,
)

from paranum import util
from paranum.data_parallel import should_replicate_map, should_replicate_is_leaf

unsafe_map, map = map, safe_map  # type: ignore


def jaxpr_to_xla_computation(jaxpr, in_avals, consts, fun_name="", backend=None):
    c = xb.make_computation_builder(f"xla_computation_{fun_name}")
    xla_consts = map(partial(xb.constant, c), consts)
    should_tuple = len(in_avals) > 100
    xla_args, donated_invars = xla._xla_callable_args(c, in_avals, should_tuple)
    axis_env = xla.AxisEnv(1, (), ())
    out_nodes = xla.jaxpr_subcomp(
        c,
        jaxpr,
        backend,
        axis_env,
        xla_consts,
        extend_name_stack(wrap_name(fun_name, "xla_computation")),
        *xla_args,
    )
    build_out_tuple = partial(xc.ops.Tuple, c, out_nodes)
    out_tuple = build_out_tuple()
    built = c.build(out_tuple)
    return built


def shard_first_dim(x):
    if util.compute_bytes(x) < 128:
        return OrderedDict()
    return OrderedDict([('mesh_x', 0)])


@lu.cache
def shard_parallel_callable(
    fun: lu.WrappedFun,
    in_tree,
    out_tree_thunk,
    devices,
    *avals
):
    fun_name = fun.__name__
    devices = devices or np.array(jax.devices())

    # Get jaxpr and XLA hlo
    jaxpr, out_avals, consts = pe.trace_to_jaxpr_dynamic(fun, avals)

    #strategy = 'partition_all'
    strategy = 'data_parallel'

    if strategy == 'partition_all':
        mesh = Mesh(devices, ('mesh_x',))
        in_axes = tuple(unsafe_map(shard_first_dim, avals))
        out_axes = tuple(unsafe_map(shard_first_dim, out_avals))
        out_axes_thunk = lambda: out_axes
        donated_invars = (False,) * len(avals)
    elif strategy == 'data_parallel':
        # Detect weight tensors and mark them as "should_replicate"
        dyn_args = tree_unflatten(in_tree, avals)
        should_replicate = tree_map(
            should_replicate_map, dyn_args, should_replicate_is_leaf
        )
        should_replicate = tuple(
            flatten_axes("shard_parallel_callable should_replicate", in_tree, should_replicate)
        )

        # Create in_axes paritition spec
        in_axes = tuple(OrderedDict() if should_replicate[i] else shard_first_dim(avals[i])
                        for i in range(len(avals)))

        # Create out_axes paritition spec
        unflatten_out_avals = tree_unflatten(out_tree_thunk(), out_avals)
        out_should_replicate = tree_map(
            should_replicate_map, unflatten_out_avals, should_replicate_is_leaf
        )
        out_should_replicate = flatten_axes(
            "shard_parallel_callable out_should_replicate",
            out_tree_thunk(),
            out_should_replicate,
        )
        out_axes = tuple(OrderedDict() if out_should_replicate[i] else shard_first_dim(out_avals[i])
                        for i in range(len(out_avals)))

        mesh = Mesh(devices, ('mesh_x',))
        out_axes_thunk = lambda: out_axes
        donated_invars = (False,) * len(avals)
    else:
        raise ValueError("Invalid strategy: " + strategy)

    # Clean stores for the next call
    for store in fun.stores:
        store and store.reset()

    # Lower to mesh_callable
    compiled_func = mesh_callable(fun, fun_name, None, mesh,
                                  in_axes, out_axes_thunk, donated_invars,
                                  True, *avals, tile_by_mesh_axes=False)
    return compiled_func

