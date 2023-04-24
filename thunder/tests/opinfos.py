import itertools
import math
import operator
from collections import namedtuple
from functools import partial, wraps
from numbers import Number
from typing import Sequence

import numpy as np
import pytest

# TODO: make this import conditional on Torch being available and querying if should test with torch
import torch
from looseversion import LooseVersion

import thunder.core.dtypes as datatypes
import thunder.core.lang as tlang
import thunder.core.prims as prims
import thunder.langs.torch as ttorch
from thunder.core.pytree import tree_map
from thunder.langs.torch import torch_dtype
from thunder.tests.framework import _all_device_types, JAX_AVAILABLE, nvFuser
from thunder.tests.make_tensor import make_tensor

# Useful when specifying the domain of an operation
# NOTE: Big enough such that -1 + eps != -1 in bfloat16
# TODO: improve domain specification to allow intervals to be open or closed at the left and right
#   Today, the domain is assumed to be closed on the left and open on the right, that is: [x, y)
eps = 1e-2


def round_remainder(x, y):
    return x - torch.round(x / y) * y


def push_away_from_singularities(x, singularity_fn, eps):
    """This function takes a tensor and moves individual values away
    from singularities in `eps` increments, until they are further than
    `eps` away from them. The `singularity_fn`  returns the (signed)
    distance from `x` to the nearest singularity."""
    x_dist = singularity_fn(x)
    x_ = torch.where((x_dist > 0) & (x_dist < eps), x + eps, x)
    return torch.where((x_dist < 0) & (x_dist > -eps), x - eps, x_)


def make_number(**kwargs):
    v = make_tensor((), device="cpu", **kwargs).item()
    return v


# Returns a noncontiguous (tensor with the same shape and values as t
# The noncontiguous tensor is constructed such that elements in the innermost
#   dimension are separated by zeros or (whenever possible) nans
# TODO: consider more complicated noncontiguity schemes
def noncontiguous_like(t):
    # Short-circuits if t is already noncontiguous
    if not t.is_contiguous():
        return t

    # Choose a "weird" value that won't be accessed
    if t.dtype.is_floating_point or t.dtype.is_complex:
        value = math.nan
    elif t.dtype == torch.bool:
        value = True
    else:
        value = 12

    result = t.new_empty(t.shape + (2,))
    result[..., 0] = value
    result[..., 1] = t.detach()
    result = result[..., 1]
    result.requires_grad_(t.requires_grad)
    return result


_torch_to_numpy_dtype_map = {
    torch.bool: np.bool_,
    torch.uint8: np.uint8,
    torch.int8: np.int8,
    torch.int16: np.int16,
    torch.int32: np.int32,
    torch.int64: np.int64,
    torch.float16: np.float16,
    torch.float32: np.float32,
    torch.float64: np.float64,
    torch.complex64: np.complex64,
    torch.complex128: np.complex128,
}

_torch_to_jax_dtype_map = None
if JAX_AVAILABLE:
    import jax
    import jax.numpy as jnp

    _torch_to_jax_dtype_map = {
        torch.bool: jnp.bool_,
        torch.uint8: jnp.uint8,
        torch.int8: jnp.int8,
        torch.int16: jnp.int16,
        torch.int32: jnp.int32,
        torch.int64: jnp.int64,
        torch.bfloat16: jnp.bfloat16,
        torch.float16: jnp.float16,
        torch.float32: jnp.float32,
        torch.float64: jnp.float64,
        torch.complex64: jnp.complex64,
        torch.complex128: jnp.complex128,
    }


class SampleInput:
    """Represents sample inputs to a function."""

    __slots__ = [
        "args",
        "kwargs",
    ]

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    # TODO: print kwargs
    def __repr__(self):
        arg_string = ", ".join(tuple(str(a) for a in self.args))
        return f"[SampleInput args=({arg_string})]"

    def noncontiguous(self):
        def to_noncontiguous(t):
            if isinstance(t, torch.Tensor):
                return noncontiguous_like(t)
            elif isinstance(t, torch.dtype):
                return t

            return t

        args, kwargs = tree_map(to_noncontiguous, self.args), tree_map(to_noncontiguous, self.kwargs)
        return SampleInput(*args, **kwargs)

    def jax(self):
        def to_jax(t):
            if isinstance(t, torch.Tensor):
                return jnp.array(t.cpu().numpy())
            if isinstance(t, torch.dtype):
                return _torch_to_jax_dtype_map[t]

            return t

        args, kwargs = tree_map(to_jax, self.args), tree_map(to_jax, self.kwargs)
        return SampleInput(*args, **kwargs)

    def thunder(self):
        def to_thunder(t):
            if isinstance(t, torch.dtype):
                return ttorch.thunder_dtype(t)
            return t

        args, kwargs = tree_map(to_thunder, self.args), tree_map(to_thunder, self.kwargs)
        return SampleInput(*args, **kwargs)


# TODO: add executor
class DecorateInfo:
    """Describes which test, or type of tests, should be wrapped in the given decorator when testing an operator.

    Any test that matches all provided arguments will be decorated. The decorator will only be applied if the active_if
    argument is True.
    """

    __slots__ = [
        "decorator",
        "test_template_name",
        "executors",
        "devicetypes",
        "dtypes",
        "active_if",
    ]

    def __init__(
        self,
        decorator,
        test_template_name=None,
        *,
        executors=None,
        devicetypes=None,
        dtypes=None,
        active_if=True,
    ):
        self.decorator = decorator
        self.test_template_name = test_template_name
        self.executors = executors
        self.devicetypes = devicetypes
        self.dtypes = None if dtypes is None else datatypes.resolve_dtypes(dtypes)
        self.active_if = active_if

    def is_active(self, test_template_name, executor, devicetype, dtype):
        return (
            self.active_if
            and (self.executors is None or executor.name in self.executors)
            and (self.test_template_name is None or self.test_template_name == test_template_name)
            and (self.devicetypes is None or devicetype in self.devicetypes)
            and (self.dtypes is None or dtype in self.dtypes)
        )


Domain = namedtuple("Domain", "low high")
opinfos = []


# TODO: require use of generic Thunder dtypes (once they exist)
class OpInfo:
    """Operator information and helper functions for acquiring it."""

    def __init__(
        self,
        op,
        *,
        name=None,
        devicetypes=None,
        dtypes=None,
        sample_input_generator,
        error_input_generator=None,
        benchmark_generator=None,
        method_variant=None,
        operator_variant=None,
        torch_reference=None,
        numpy_reference=None,
        jax_reference=None,
        test_directives=(),
        domain=(None, None),
        singularity_fn=None,
    ):
        self.op = op
        self.name = name if name is not None else op.__name__
        self._devicetypes = devicetypes if devicetypes is not None else _all_device_types()
        self._dtypes = dtypes if dtypes is not None else (datatypes.exact, datatypes.inexact)
        self.sample_input_generator = sample_input_generator
        self.error_input_generator = error_input_generator
        self.benchmark_generator = benchmark_generator
        self.method_variant = method_variant
        self.operator_variant = operator_variant
        self.torch_reference = torch_reference
        self.numpy_reference = numpy_reference
        self.jax_reference = jax_reference
        self.test_directives = test_directives
        self.domain = Domain(*domain)
        self.singularity_fn = singularity_fn

    def __call__(self, *args, **kwargs):
        """Calls the function variant of the operator."""
        return self.op(*args, **kwargs)

    # TODO: maybe allow sample input generation not using torch
    # NOTE: Today all sample inputs are generated with PyTorch, so Thunder objects,
    #   like dtypes, need to be translated into PyTorch objects
    def sample_inputs(self, device_type, dtype, *, requires_grad=False, **kwargs):
        dtype = torch_dtype(dtype)
        return self.sample_input_generator(self, device_type, dtype, requires_grad, **kwargs)

    def error_inputs(self, device, **kwargs):
        return self.error_input_generator(self, device, **kwargs)

    # NOTE: Today all benchmarks are generated with PyTorch, so Thunder objects,
    #   like dtypes, need to be translated into PyTorch objects
    def benchmarks(self, device_type, dtype, *, requires_grad=False, **kwargs):
        dtype = torch_dtype(dtype)
        return self.benchmark_generator(self, device_type, dtype, requires_grad, **kwargs)

    def device_types(self):
        return set(self._devicetypes)

    def dtypes(self, device_type=None):
        if device_type is not None:
            raise NotImplementedError

        return datatypes.resolve_dtypes(self._dtypes)

    # TODO: add executor
    def test_decorators(self, test_name, executor, devicetype, dtype):
        return [d.decorator for d in self.test_directives if d.is_active(test_name, executor, devicetype, dtype)]


#
# Elementwise Unary OpInfos
#

# TODO: create elementwise unary OpInfo subclass and maybe auto add to list
elementwise_unary_ops = []


# TODO: add small value, large value, and extremal-valued samples
def elementwise_unary_generator(op, device, dtype, requires_grad, *, supports_numbers=True, **kwargs):
    low = None if op.domain.low is None else max(-9, op.domain.low)
    high = None if op.domain.high is None else min(9, op.domain.high)
    make_arg = partial(
        make_tensor, device=device, dtype=dtype, low=low, high=high, requires_grad=requires_grad, **kwargs
    )

    shapes = (
        # TODO: restore size zero cases
        # (0, 2, 1),
        # (5, 0, 3),
        (),
        (11,),
        (4, 4),
        (1024, 1024),
        (64, 64, 64),
    )

    # Typical inputs
    for shape in shapes:
        yield SampleInput(make_arg(shape))

    # Noncontiguous inputs
    for shape in shapes:
        yield SampleInput(make_arg(shape, noncontiguous=True))

    # Arbitrarily strided inputs
    # shape, strides, offset
    strided_cases = (
        ((5, 6, 2), (1, 1, 7), 2),
        ((5, 5, 4), (1, 1, 7), 2),
        ((5, 5, 2), (4, 5, 7), 3),
        ((5, 5, 2), (5, 5, 7), 3),
        ((5, 5, 2), (5, 5, 5), 3),
        ((9, 5, 2), (0, 1, 7), 3),
    )

    for shape, strides, offset in strided_cases:
        a = make_arg(
            500,
        ).as_strided(shape, strides, offset)
        yield SampleInput(a)

    # Scalar case
    # FIXME: see https://github.com/Lightning-AI/lightning-thunder/issues/169
    if supports_numbers and not datatypes.is_complex_dtype(ttorch.thunder_dtype(dtype)):
        a = make_number(dtype=dtype, low=low, high=high, **kwargs)
        yield SampleInput(a)


def elementwise_unary_benchmarks(op, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # name x shape
    cases = (
        ("8x8", (8, 8)),
        ("64x64", (64, 64)),
        ("1024x1024", (1024, 1024)),
    )

    for name, shape in cases:
        yield name, SampleInput(make_arg(shape))


class ElementwiseOpInfo(OpInfo):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class ElementwiseUnaryOpInfo(ElementwiseOpInfo):
    def __init__(
        self,
        *args,
        sample_input_generator=elementwise_unary_generator,
        benchmark_generator=elementwise_unary_benchmarks,
        **kwargs,
    ):
        super().__init__(
            *args,
            sample_input_generator=sample_input_generator,
            benchmark_generator=elementwise_unary_benchmarks,
            **kwargs,
        )

        elementwise_unary_ops.append(self)


# NOTE: many PyTorch operations don't accept numbers as inputs,
#   so this helper wraps and unwraps numbers
def _elementwise_unary_torch(op):
    @wraps(op)
    def _fn(x):
        if isinstance(x, torch.Tensor):
            return op(x)

        return op(torch.tensor(x)).item()

    return _fn


# NOTE: slightly different from generic _elementwise_unary_torch helper
#   because this returns the input when given an unsigned type
@wraps(torch.abs)
def _abs_torch(x):
    if datatypes.is_unsigned_dtype(ttorch.to_dtype(x)):
        return x

    if isinstance(x, torch.Tensor):
        return torch.abs(x)

    return torch.abs(torch.tensor(x)).item()


abs_opinfo = ElementwiseUnaryOpInfo(
    tlang.abs,
    torch_reference=_abs_torch,
)

acos_opinfo = OpInfo(
    tlang.acos,
    domain=(-1, 1),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.acos),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 acos
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(acos_opinfo)

acosh_opinfo = OpInfo(
    tlang.acosh,
    domain=(1, math.inf),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.acosh),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 acosh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
    ),
)
elementwise_unary_ops.append(acosh_opinfo)

asin_opinfo = OpInfo(
    tlang.asin,
    domain=(-1, 1),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.asin),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 asin
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
        # TODO: RuntimeError: Unexpected operator type sqrt in d4 = sqrt(double(0.33680657142871817));
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_unary_ops.append(asin_opinfo)

asinh_opinfo = OpInfo(
    tlang.asinh,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.asinh),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 asinh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
    ),
)
elementwise_unary_ops.append(asinh_opinfo)

atan_opinfo = OpInfo(
    tlang.atan,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.atan),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 atan
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(atan_opinfo)

atanh_opinfo = OpInfo(
    tlang.atanh,
    domain=(-1 + eps, 1 - eps),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.atanh),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 atanh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(atanh_opinfo)

bitwise_not_opinfo = OpInfo(
    tlang.bitwise_not,
    dtypes=(datatypes.exact,),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.bitwise_not),
)
elementwise_unary_ops.append(bitwise_not_opinfo)

ceil_opinfo = OpInfo(
    tlang.ceil,
    dtypes=(datatypes.floating, datatypes.exact),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.ceil),
    test_directives=(
        # Torch doesn't support bool ceil
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bool8,),
        ),
        # Torch doesn't support cpu float16 ceil
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # PyTorch didn't support ceil on exact types before 1.13
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.exact,),
            devicetypes=("cpu",),
            active_if=LooseVersion(torch.__version__) < "1.13",
        ),
    ),
)
elementwise_unary_ops.append(ceil_opinfo)

cos_opinfo = OpInfo(
    tlang.cos,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.cos),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 cos
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(cos_opinfo)

cosh_opinfo = OpInfo(
    tlang.cosh,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.cosh),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 cosh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(cosh_opinfo)

erf_opinfo = OpInfo(
    tlang.erf,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.erf),
    test_directives=(
        # Torch doesn't support CPU float16 erf
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support complex erf
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
    ),
)
elementwise_unary_ops.append(erf_opinfo)

erfc_opinfo = OpInfo(
    tlang.erfc,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.erfc),
    test_directives=(
        # Torch doesn't support CPU float16 erfc
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support complex erfc
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
    ),
)
elementwise_unary_ops.append(erfc_opinfo)

erfcinv_opinfo = OpInfo(
    tlang.erfcinv,
    dtypes=(datatypes.floating,),
    # erfcinv is only defined for x in [0, 2]
    # We use [0.3, 0.7] to avoid the stability issues because we're using
    # erfinv(1 - x) as the reference that is less accurate and less stable than
    # erfcinv
    # TODO: use a better reference (SciPy or pyerf)
    domain=(0.3, 0.7),
    sample_input_generator=partial(elementwise_unary_generator, supports_numbers=False),
    torch_reference=_elementwise_unary_torch(lambda x: torch.erfinv(1 - x)),
    test_directives=(
        # Torch doesn't support CUDA bfloat16 erfinv
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cuda",),
        ),
        # Torch doesn't support CPU float16 erfinv
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Torch is not accurate enough with native bfloat16
        # torch.erfinv(1 - bfloat16) is far from torch.erfinv(1 - bfloat16.float())
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support complex erfinv
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
    ),
)
elementwise_unary_ops.append(erfcinv_opinfo)

erfinv_opinfo = OpInfo(
    tlang.erfinv,
    domain=(-1, 1),
    sample_input_generator=partial(elementwise_unary_generator, supports_numbers=False),
    torch_reference=_elementwise_unary_torch(torch.erfinv),
    test_directives=(
        # Torch doesn't support CUDA bfloat16 erfinv
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cuda",),
        ),
        # Torch doesn't support CPU float16 erfinv
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support complex erfinv
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
    ),
)
elementwise_unary_ops.append(erfinv_opinfo)

exp_opinfo = OpInfo(
    tlang.exp,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.exp),
    test_directives=(
        # Torch doesn't support CPU float16 or complex32 exp
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
        # TODO: this test fails (slightly out of tolerance) on CI machines
        #   Maybe restrict the test to A100 and H100 cards?
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float64,),
            devicetypes=("cuda",),
        ),
    ),
)
elementwise_unary_ops.append(exp_opinfo)

exp2_opinfo = OpInfo(
    tlang.exp2,
    sample_input_generator=partial(elementwise_unary_generator, supports_numbers=False),
    torch_reference=_elementwise_unary_torch(torch.exp2),
    test_directives=(
        # Torch doesn't support complex exp2
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
    ),
)
elementwise_unary_ops.append(exp2_opinfo)

expm1_opinfo = OpInfo(
    tlang.expm1,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.expm1),
    test_directives=(
        # Torch doesn't support CPU float16 expm1
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support complex expm1
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
    ),
)
elementwise_unary_ops.append(expm1_opinfo)

floor_opinfo = OpInfo(
    tlang.floor,
    dtypes=(datatypes.floating, datatypes.exact),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.floor),
    test_directives=(
        # Torch doesn't support bool floor
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bool8,),
        ),
        # Torch doesn't support cpu float16 floor
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # PyTorch didn't support floor on exact types before 1.13
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.exact,),
            devicetypes=("cpu",),
            active_if=LooseVersion(torch.__version__) < "1.13",
        ),
    ),
)
elementwise_unary_ops.append(floor_opinfo)

isfinite_opinfo = OpInfo(
    tlang.isfinite,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.isfinite),
    test_directives=(
        # nvFuser doesn't correctly return outputs as boolean tensors, and doesn't support full
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
        ),
        # Torch preserves the uint8 dtype
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.uint8,),
        ),
    ),
)
elementwise_unary_ops.append(isfinite_opinfo)

rsqrt_opinfo = OpInfo(
    tlang.rsqrt,
    domain=(0, math.inf),
    sample_input_generator=partial(elementwise_unary_generator, exclude_zero=True),
    torch_reference=_elementwise_unary_torch(torch.rsqrt),
    test_directives=(
        # NOTE: Torch doesn't support CPU float16 or complex32 tanh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
        # see https://github.com/csarofeen/pytorch/issues/2367
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
        # NOTE: low-precision types are too different
        # TODO: verify that thunder is weakly more accurate or reduce precision required in these cases
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32, datatypes.bfloat16),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(rsqrt_opinfo)

silu_opinfo = OpInfo(
    tlang.silu,
    dtypes=(datatypes.floating,),
    sample_input_generator=partial(elementwise_unary_generator, supports_numbers=False),
    torch_reference=_elementwise_unary_torch(torch.nn.functional.silu),
    test_directives=(
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
        # NOTE: Torch doesn't support CPU float16 silu
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # test tols are too tight for these half precision tests
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.bfloat16),
        ),
    ),
)
elementwise_unary_ops.append(silu_opinfo)

sigmoid_opinfo = OpInfo(
    tlang.sigmoid,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.sigmoid),
    test_directives=(
        # torch.sigmoid is not implemented for CPU float16 or complex32
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            executors=("TorchEx",),
            devicetypes=("cpu",),
            dtypes=(datatypes.float16, datatypes.complex32),
        ),
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            executors=("TorchEx",),
            devicetypes=("cuda",),
            dtypes=(
                # reciprocal_cuda for ComplexHalf is not implemented in torch
                datatypes.complex32,
            ),
        ),
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            executors=("TorchEx",),
            dtypes=(
                # sometimes fails due to tight tolerances (passes with rtol=1e-4)
                datatypes.complex64,
            ),
        ),
        # test tols are too tight for these half precision tests
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.bfloat16),
        ),
    ),
)
elementwise_unary_ops.append(sigmoid_opinfo)

sign_opinfo = OpInfo(
    tlang.sign,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.sgn),
    test_directives=(
        # TODO: need to add nvFuser specific support for complex sign
        # https://github.com/csarofeen/pytorch/issues/2492
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.complexfloating,),
            executors=("nvFuser",),
        ),
    ),
)
elementwise_unary_ops.append(sign_opinfo)

sin_opinfo = OpInfo(
    tlang.sin,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.sin),
    test_directives=(
        # NOTE: Torch doesn't support CPU float16 or complex32 sin
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(sin_opinfo)

sinh_opinfo = OpInfo(
    tlang.sinh,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.sinh),
    test_directives=(
        # NOTE: Torch doesn't support CPU float16 or complex32 sinh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(sinh_opinfo)

# TODO: refine domain vs. complex domain
sqrt_opinfo = OpInfo(
    tlang.sqrt,
    domain=(0, math.inf),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.sqrt),
    test_directives=(
        # NOTE: Torch doesn't support CPU float16 or complex32 sqrt
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(sqrt_opinfo)


tan_opinfo = OpInfo(
    tlang.tan,
    singularity_fn=lambda x: round_remainder(x, torch.pi / 2),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.tan),
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2360
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", executors=("nvFuser",), dtypes=(datatypes.complex64,)
        ),
        # NOTE: Torch doesn't support CPU float16 or complex32 tan
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(tan_opinfo)

tanh_opinfo = OpInfo(
    tlang.tanh,
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.tanh),
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2360
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", executors=("nvFuser",), dtypes=(datatypes.complex64,)
        ),
        # NOTE: Torch doesn't support CPU float16 or complex32 tanh
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(tanh_opinfo)

# lgamma is defined for all complex numbers EXCEPT negative integers and zero
lgamma_opinfo = OpInfo(
    tlang.lgamma,
    domain=(-1.0 + eps, math.inf),
    sample_input_generator=partial(elementwise_unary_generator, exclude_zero=True),
    torch_reference=_elementwise_unary_torch(torch.lgamma),
    test_directives=(
        # NOTE: Torch doesn't support CPU float16 lgamma
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support CUDA bfloat16 lgamma
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cuda",),
        ),
        # Torch doesn't support complex lgamma
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
    ),
)
elementwise_unary_ops.append(lgamma_opinfo)

log_opinfo = OpInfo(
    tlang.log,
    domain=(0, math.inf),
    sample_input_generator=partial(elementwise_unary_generator, exclude_zero=True),
    torch_reference=_elementwise_unary_torch(torch.log),
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2360
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", executors=("nvFuser",), dtypes=(datatypes.complex64,)
        ),
        # NOTE: Torch doesn't support CPU float16 or complex32 log
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.complex32),
            devicetypes=("cpu",),
        ),
    ),
)
elementwise_unary_ops.append(log_opinfo)

log10_opinfo = OpInfo(
    tlang.log10,
    domain=(0, math.inf),
    sample_input_generator=partial(elementwise_unary_generator, exclude_zero=True),
    torch_reference=_elementwise_unary_torch(torch.log10),
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2360
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", executors=("nvFuser",), dtypes=(datatypes.complex64,)
        ),
        # NOTE: Torch doesn't support CPU float16 log10
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # NOTE: Torch doesn't support complex32 log10
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
        ),
    ),
)
elementwise_unary_ops.append(log10_opinfo)

# TODO: need a way to specify that lhs of the domain is open
log1p_opinfo = OpInfo(
    tlang.log1p,
    domain=(-1 + eps, math.inf),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.log1p),
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2360
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            executors=("nvFuser",),
            dtypes=(datatypes.complexfloating,),
        ),
        # NOTE: Torch gives wrong result: https://github.com/pytorch/pytorch/issues/94333
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cpu",),
        ),
        # NOTE: Torch doesn't support CPU float16 log1p
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # NOTE: Torch doesn't support complex32 log1p
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
        ),
        # PyTorch didn't support CPU complex log1p before 2.0
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
            devicetypes=("cpu",),
            active_if=LooseVersion(torch.__version__) < "2.0",
        ),
    ),
)
elementwise_unary_ops.append(log1p_opinfo)

log2_opinfo = OpInfo(
    tlang.log2,
    domain=(0, math.inf),
    sample_input_generator=partial(elementwise_unary_generator, exclude_zero=True),
    torch_reference=_elementwise_unary_torch(torch.log2),
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2360
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", executors=("nvFuser",), dtypes=(datatypes.complex64,)
        ),
        # NOTE: Torch doesn't support CPU float16 log2
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # NOTE: Torch doesn't support complex32 log2
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
        ),
    ),
)
elementwise_unary_ops.append(log2_opinfo)

neg_opinfo = OpInfo(
    tlang.neg,
    dtypes=set(datatypes.all_dtypes) - set(datatypes.boolean_dtypes),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.neg),
)
elementwise_unary_ops.append(neg_opinfo)

ndtri_opinfo = OpInfo(
    tlang.ndtri,
    sample_input_generator=partial(elementwise_unary_generator, supports_numbers=False),
    torch_reference=_elementwise_unary_torch(torch.special.ndtri),
    test_directives=(
        # Torch doesn't support bfloat16 and float16 ndtri
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16, datatypes.float16),
        ),
        # Torch doesn't support complex ndtri
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complexfloating,),
        ),
    ),
)
elementwise_unary_ops.append(ndtri_opinfo)

reciprocal_opinfo = OpInfo(
    tlang.reciprocal,
    domain=(0 + eps, math.inf),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.reciprocal),
    test_directives=(
        # Torch doesn't support complex32 reciprocal
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
        ),
    ),
)
elementwise_unary_ops.append(reciprocal_opinfo)

round_opinfo = OpInfo(
    tlang.round,
    dtypes=(datatypes.floating, datatypes.exact),
    sample_input_generator=partial(elementwise_unary_generator, supports_numbers=False),
    torch_reference=_elementwise_unary_torch(torch.round),
    test_directives=(
        # Torch doesn't support CPU float16 and bool round
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.bool8),
            devicetypes=("cpu",),
        ),
        # Torch doesn't support CUDA bool round
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bool8,),
            devicetypes=("cuda",),
        ),
    ),
)
elementwise_unary_ops.append(round_opinfo)

trunc_opinfo = OpInfo(
    tlang.trunc,
    dtypes=(datatypes.floating, datatypes.exact),
    sample_input_generator=elementwise_unary_generator,
    torch_reference=_elementwise_unary_torch(torch.trunc),
    test_directives=(
        # Torch doesn't support bool trunc
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bool8,),
        ),
        # Torch doesn't support cpu float16 trunc
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # PyTorch didn't support trunc on exact types before 1.13
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.exact,),
            devicetypes=("cpu",),
            active_if=LooseVersion(torch.__version__) < "1.13",
        ),
        # TODO: nvFuser needs to return copy for integer dtypes.
        # https://github.com/csarofeen/pytorch/issues/2499
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            executors=("nvFuser",),
            dtypes=(datatypes.int32, datatypes.int64),
        ),
    ),
)
elementwise_unary_ops.append(trunc_opinfo)


# Puts all opinfos into the "opinfos" list
opinfos.extend(elementwise_unary_ops)


#
# Elementwise Binary OpInfos
#

# TODO: create elementwise binary OpInfo subclass and maybe auto add to list
elementwise_binary_ops = []


# TODO: extend this generator
def elementwise_binary_generator(op, device, dtype, requires_grad, **kwargs):
    a = make_tensor((4, 4), device=device, dtype=dtype, requires_grad=requires_grad, **kwargs)
    b = make_tensor((4, 4), device=device, dtype=dtype, requires_grad=requires_grad, **kwargs)

    yield SampleInput(a, b)

    # Tests broadcasting
    c = make_tensor((4, 1), device=device, dtype=dtype, requires_grad=requires_grad, **kwargs)
    yield SampleInput(a, c)


# TODO: update dtypes with Thunder dtypes (when they exist)
add_opinfo = OpInfo(
    tlang.add,
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.add,
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2549
        DecorateInfo(
            pytest.mark.skip,
            "test_jvp_correctness",
            executors=("nvFuser",),
        ),
        DecorateInfo(
            pytest.mark.skip,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_binary_ops.append(add_opinfo)

# NOTE: nvFuser does not currently support uint8, int8, or int16
bitwise_and_opinfo = OpInfo(
    tlang.bitwise_and,
    dtypes=(datatypes.exact,),
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.bitwise_and,
)
elementwise_binary_ops.append(bitwise_and_opinfo)

# For grad test stability it's better to use wider range of values
elementwise_comparison_generator = partial(elementwise_binary_generator, low=-1000, high=1000)

eq_opinfo = OpInfo(
    tlang.eq,
    sample_input_generator=elementwise_comparison_generator,
    torch_reference=torch.eq,
    test_directives=(
        # There's a problem of reducing a tensor produced by full op
        # See https://github.com/NVIDIA/Fuser/issues/132
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_binary_ops.append(eq_opinfo)

fmod_opinfo = OpInfo(
    tlang.fmod,
    sample_input_generator=partial(elementwise_binary_generator, exclude_zero=True),
    torch_reference=torch.fmod,
    test_directives=(
        # torch doesn't support bool or complex fmod
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", dtypes=(datatypes.bool8, datatypes.complexfloating)
        ),
        # bfloat16 computation is too inconsistent
        # TODO: improve bfloat16 testing to allow more accurate computations and/or looser
        #   bfloat16 tolerances
        DecorateInfo(pytest.mark.skip, "test_core_vs_torch_consistency", dtypes=(datatypes.bfloat16,)),
    ),
)
elementwise_binary_ops.append(fmod_opinfo)

ge_opinfo = OpInfo(
    tlang.ge,
    # NOTE: comparison is only defined for real numbers
    dtypes=(datatypes.exact, datatypes.floating),
    sample_input_generator=elementwise_comparison_generator,
    torch_reference=torch.ge,
    test_directives=(
        # There's a problem of reducing a tensor produced by full op
        # See https://github.com/NVIDIA/Fuser/issues/132
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_binary_ops.append(ge_opinfo)

lt_opinfo = OpInfo(
    tlang.lt,
    # NOTE: comparison is only defined for real numbers
    dtypes=(datatypes.exact, datatypes.floating),
    sample_input_generator=elementwise_comparison_generator,
    torch_reference=torch.lt,
    test_directives=(
        # There's a problem of reducing a tensor produced by full op
        # See https://github.com/NVIDIA/Fuser/issues/132
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_binary_ops.append(lt_opinfo)

mul_opinfo = OpInfo(
    tlang.mul,
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.mul,
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2549
        DecorateInfo(
            pytest.mark.skip,
            "test_jvp_correctness",
            executors=("nvFuser",),
        ),
        DecorateInfo(
            pytest.mark.skip,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_binary_ops.append(mul_opinfo)

nextafter_opinfo = OpInfo(
    tlang.nextafter,
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.nextafter,
    # NOTE: nextafter is supported by PyTorch only for bfloat16, float32,
    # and float64 arguments (after normal promotion rules) and by NVFuser
    # only for float32, and float64 arguments (after normal promotion rules).
    dtypes=(datatypes.floating,),
    test_directives=(
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.bfloat16),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.7",
        ),
    ),
)
elementwise_binary_ops.append(nextafter_opinfo)

pow_opinfo = OpInfo(
    tlang.pow,
    sample_input_generator=elementwise_binary_generator,
    torch_reference=None if LooseVersion(torch.__version__) < "1.13" else torch._refs.pow,
    test_directives=(
        # NOTE: PyTorch doesn't support bool pow
        DecorateInfo(pytest.mark.xfail, "test_core_vs_torch_consistency", dtypes=(datatypes.bool8,)),
        # NOTE: PyTorch doesn't support cpu complex32 pow, and doesn't seem to promote it properly
        # NOTE: The CUDA version of this test also fails occasionally -- maybe switch to torch reference?
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
        ),
        # See https://github.com/csarofeen/pytorch/issues/2361
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            executors=("nvFuser,"),
            dtypes=(datatypes.complex64, datatypes.complex128),
        ),
    ),
)
elementwise_binary_ops.append(pow_opinfo)

# A test for the prim remainder which corresponds with python's math.remainder and c++ std::remainder
remainder_core_opinfo = OpInfo(
    tlang.remainder,
    name="remainder_prim",
    sample_input_generator=partial(elementwise_binary_generator, exclude_zero=True),
    torch_reference=lambda a, b: a - torch.round(a.div(b)) * b,
    test_directives=(
        # torch doesn't support bool or complex remainder.
        # torch_reference is inaccurate since it computes in the lower precision dtype.
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bool8, datatypes.float16, datatypes.bfloat16, datatypes.complexfloating),
        ),
    ),
)
elementwise_binary_ops.append(remainder_core_opinfo)

remainder_torch_opinfo = OpInfo(
    ttorch.remainder,
    sample_input_generator=partial(elementwise_binary_generator, exclude_zero=True),
    torch_reference=torch.remainder,
    test_directives=(
        # torch doesn't support bool or complex remainder.
        DecorateInfo(
            pytest.mark.xfail, "test_core_vs_torch_consistency", dtypes=(datatypes.bool8, datatypes.complexfloating)
        ),
        # Upstream nvfuser triggers this error:
        # AssertionError: The values for attribute 'dtype' do not match: torch.float32 != torch.float16.
        # See https://github.com/Lightning-AI/lightning-thunder/issues/238
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            executors=("nvFuser,"),
            dtypes=(datatypes.float16, datatypes.bfloat16),
        ),
    ),
)
elementwise_binary_ops.append(remainder_torch_opinfo)

sub_opinfo = OpInfo(
    tlang.sub,
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.sub,
    test_directives=(
        # torch doesn't support bool sub
        DecorateInfo(pytest.mark.xfail, "test_core_vs_torch_consistency", dtypes=(datatypes.bool8,)),
    ),
)
elementwise_binary_ops.append(sub_opinfo)

true_divide_opinfo = OpInfo(
    tlang.true_divide,
    sample_input_generator=elementwise_binary_generator,
    torch_reference=torch.true_divide,
    test_directives=(
        # torch cpu doesn't support complex32 div
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
            devicetypes=("cpu",),
        ),
        # torch doesn't support bool true_divide
        DecorateInfo(pytest.mark.xfail, "test_core_vs_torch_consistency", dtypes=(datatypes.bool8,)),
        # See https://github.com/csarofeen/pytorch/issues/2549
        DecorateInfo(
            pytest.mark.skip,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
elementwise_binary_ops.append(true_divide_opinfo)

# Puts all opinfos into the "opinfos" list
opinfos.extend(elementwise_binary_ops)

#
# Elementwise Ternary OpInfos
#
elementwise_ternary_ops = []


# TODO: add number tensors for value
# TODO: error inputs
def masked_fill_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    number = partial(make_number, dtype=dtype)

    # pred_shape, a_shape, value
    cases = (
        ((2, 1, 2), (1, 2, 2), number()),
        ((4, 6), (6, 4, 6), number()),
        ((3,), (3,), number()),
    )

    for pred_shape, a_shape, value in cases:
        pred, a = make(pred_shape, dtype=torch.bool, requires_grad=False), make(a_shape)
        yield SampleInput(a, pred, value)


masked_fill_opinfo = OpInfo(
    ttorch.masked_fill,
    sample_input_generator=masked_fill_sample_generator,
    torch_reference=torch.masked_fill,
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2378
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16, datatypes.float16),
            executors=("nvFuser",),
        ),
    ),
)
elementwise_ternary_ops.append(masked_fill_opinfo)


def where_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # pred_shape, a_shape, b_shape
    # NOTE: shapes must be broadcastable
    cases = (
        ((5,), (5,), (5,)),
        ((2, 1, 2), (1, 2, 2), (2, 2, 1)),
    )

    # NOTE: pred must have a boolean dtype
    for pred_shape, a_shape, b_shape in cases:
        pred, a, b = make(pred_shape, dtype=torch.bool, requires_grad=False), make(a_shape), make(b_shape)
        yield SampleInput(pred, a, b)


where_opinfo = OpInfo(
    tlang.where,
    sample_input_generator=where_sample_generator,
    torch_reference=torch.where,
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2378
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16, datatypes.float16),
            executors=("nvFuser",),
        ),
    ),
)
elementwise_ternary_ops.append(where_opinfo)

# Puts all elementwise ternary opinfos into the "opinfos" list
opinfos.extend(elementwise_ternary_ops)

#
# Shape/dtype Op OpInfos
#
shape_ops = []


# TODO: these samples could be improved
def broadcast_in_dim_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # The first 5 test cases below are taken from JAX's broadcast_in_dim tests
    #   https://github.com/google/jax/blob/main/tests/lax_test.py#L1171

    # inshape, outshape, dims
    cases = (
        ([2], [2, 2], [0]),
        ([2], [2, 2], [1]),
        ([2], [2, 3], [0]),
        ([], [2, 3], []),
        ([1], [2, 3], [1]),
        ((4, 6, 3, 1), (5, 4, 7, 6, 3, 6, 6), (1, 3, 4, 5)),
    )

    for inshape, outshape, dims in cases:
        a = make(inshape)
        yield SampleInput(a, outshape, dims)


def broadcast_in_dim_error_generator(op, device, **kwargs):
    make = partial(make_tensor, device=device, dtype=torch.float32)

    # inshape, outshape, dims, ex_info
    cases = (
        # broadcast dimensions must be strictly ascending
        ((2, 2), (2, 2), (1, 0), RuntimeError),
        # broadcast dimensions must have the same length as a.ndim
        ((3, 2, 2), (3, 2, 2), (0, 1), RuntimeError),
        ((3, 2, 2), (3, 2, 2), (0, 1, 2, 3), RuntimeError),
        # Invalid outshape
        ((3, 2, 2), (6, 2, 2), (0, 1, 2), RuntimeError),
        ((3, 2, 2), (3, 1, 2), (0, 1, 2), RuntimeError),
    )

    for inshape, outshape, dims, ex_info in cases:
        a = make(inshape)
        yield SampleInput(a, outshape, dims), ex_info


broadcast_in_dim_opinfo = OpInfo(
    prims.broadcast_in_dim,
    sample_input_generator=broadcast_in_dim_sample_generator,
    error_input_generator=broadcast_in_dim_error_generator,
    jax_reference=jax.lax.broadcast_in_dim if JAX_AVAILABLE else None,
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2549
        DecorateInfo(
            pytest.mark.skip,
            "test_jvp_correctness",
            executors=("nvFuser",),
        ),
        DecorateInfo(
            pytest.mark.skip,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
shape_ops.append(broadcast_in_dim_opinfo)


def getitem_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # TODO: all these cases are basic indexing
    # NOTE: PyTorch does not allow negative steps
    # a.shape, key
    cases = (
        # Fully specified slicing
        ((5, 5), (slice(1, 3, 1), slice(2, 4, 2))),
        ((11, 23), (slice(4, 9, 6), slice(3, 21, 4))),
        ((11, 23), (slice(4, 9, 33), slice(3, 21, 1))),
        # NOTE: PyTorch allows start > stop and will return a 0 length dim
        ((5, 3), (slice(3, 1), slice(1, 2))),
        # NOTE: NumPy allows slicing beyond the end of a dimension
        ((5, 3), (slice(6, 7), slice(0, 2))),
        ((5, 3), (slice(6, 2), slice(0, 2))),
        ((5, 3), (slice(1, 9), slice(0, 2))),
        # Inferred start
        ((5, 3), (slice(None, 9), slice(0, 2))),
        # Inferred end
        ((5, 3), (slice(2, None), slice(0, 2))),
        # Inferred start and end
        ((5, 3), (slice(None, None), slice(0, 2))),
        # Negative start and stop
        ((5, 3), (slice(-3, -1), slice(0, -2))),
        ((5, 3), (slice(-4, -1), slice(-1, -2))),
        # Partially specified slicing
        ((5, 3), (slice(-4, -1),)),
        # Slicing and numbers
        ((1, 5, 3), (0, slice(2, 3), 2)),
        ((1, 5, 3), (-1, slice(2, 3), -2)),
        # All numbers
        ((1, 5, 3), (-1, 3, -2)),
        # Ellipses
        ((1, 5, 3), (..., slice(1, 2))),
        ((1, 5, 3), (0, ..., slice(1, 2))),
        # Newaxis/None
        # TODO: nvFuser only supports kernels with <= 8 dimensions
        # ((1, 5, 3), (None, None, 0, None, 2, ..., None, None, None)),
        ((1, 5, 3), (None, None, 0, None, 2, ..., None, None)),
        # Addtl. cases
        ((7, 9, 5), (slice(2, 6, 2), None, ..., slice(3, 7), None, 2, None)),
        # TODO: nvFuser only supports kernels with <= 8 dimensions
        # ((11, 7, 9, 5), (None, slice(2, 6, 2), None, ..., slice(3, 7), None, 2, None, None)),
    )

    for shape, key in cases:
        a = make(shape)
        yield SampleInput(a, key)


getitem_opinfo = OpInfo(
    operator.getitem,
    sample_input_generator=getitem_sample_generator,
    torch_reference=operator.getitem,
    jax_reference=operator.getitem,
    test_directives=(
        # NotImplementedError: VJP for Ops.SQUEEZE is not implemented
        DecorateInfo(pytest.mark.xfail, "test_vjp_correctness"),
    ),
)
shape_ops.append(getitem_opinfo)


# TODO: only remove these cases when the executor is nvFuser
# FIXME: Zero-dim cases are skipped due to https://github.com/csarofeen/pytorch/issues/2383
# FIXME: tensors with no elements are skipped because of no nvFuser support
def reshape_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # tensor shape, shape
    cases = (
        ((4, 2), (2, -1, 2)),
        # ((), (-1,)),  # neg index, empty
        ((4, 7, 9, 1, 1), (1, 4, 3, -1, 1)),  # neg index
    )

    reversible_cases = (
        ((4,), (4,)),
        ((2, 2, 2), (4, 2)),
        ((125,), (25, 5)),
        ((25, 25), (1, 5, 5, 1, 5, 1, 5, 1)),
        ((16, 32), (2, 4, 1, 4, 4, 1, 4)),
        ((16, 12), (12, 16)),
        ((1, 16, 12), (12, 16)),
        ((1, 5, 1, 5), (25, 1)),
        ((2, 4, 2), (4, 4)),
        ((1, 4), (1, 1, 2, 1, 2)),
        ((3, 5, 7), (7, 5, 3)),
        # ((1,), ()),  # empty
        # ((5, 0, 2, 3), (5, 0, 2, 3)),
        # ((2, 1, 0, 3, 1), (5, 0)),
        # ((1,), ()),  # empty
        ((4, 5, 6), (4, 5, 6, 1, 1, 1)),
        # ((), (1, 1, 1, 1)),  # empty
        # ((), ()),
    )

    for tensor_shape, shape in cases:
        yield SampleInput(make(tensor_shape), shape)

    for shape0, shape1 in reversible_cases:
        yield SampleInput(make(shape0), shape1)
        yield SampleInput(make(shape1), shape0)


reshape_opinfo = OpInfo(
    tlang.reshape,
    sample_input_generator=reshape_sample_generator,
    torch_reference=torch.reshape,
)
shape_ops.append(reshape_opinfo)


def pad_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape, padding_config
    cases = (
        ((1, 3), ((0, 0, 0), (0, 0, 0))),
        ((3, 7, 5), ((-2, 1, 0), (1, 3, 0), (-1, 2, 0))),
        # NOTE: The following samples use padding between elements, which is not
        # supported by nvFuser as of v0.0.6. These should be uncommented once
        # nvFuser supports padding between elements.
        # ((2, 2), ((1, 1, 1), (-1, 2, 0))),
        # ((2, 0, 3), ((1, 0, 0), (1, 1, 2), (0, 0, 0))),
        # ((7, 5), ((0, 0, 3), (-6, 2, 1))),
        # ((3, 2, 5), ((-2, 1, 0), (1, -1, 0), (-1, 3, 1))),
        # Versions of above examples but with padding between elements set to 0
        ((2, 2), ((1, 1, 0), (-1, 2, 0))),
        ((2, 0, 3), ((1, 0, 0), (1, 1, 0), (0, 0, 0))),
        # without padding between, the following results in narrowing beyond extent
        # ((7, 5), ((0, 0, 0), (-6, 2, 0))),
        # Swapping the order fixes this because the second padding config
        # triplet now corresponds to the axis with extent 7.
        ((5, 7), ((0, 0, 0), (-6, 2, 0))),
        ((3, 2, 5), ((-2, 1, 0), (1, -1, 0), (-1, 3, 0))),  # negative pad in all 3 dims
    )

    for shape, padding_config in cases:
        yield SampleInput(make(shape), make_number(dtype=dtype), padding_config)


# NOTE: jax is very strict about tensor dtype vs number type, necessitating this helper
def _jax_pad(a, padding_value, padding_config):
    padding_value = jax.lax.convert_element_type(padding_value, a.dtype)
    return jax.lax.pad(a, padding_value, padding_config)


pad_opinfo = OpInfo(
    prims.pad,
    sample_input_generator=pad_sample_generator,
    jax_reference=_jax_pad if JAX_AVAILABLE else None,
    test_directives=(
        # NVFuser introduced the pad() op in v0.0.6
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.6",
        ),
        # PyTorch's pad doesn't support complex padding values
        DecorateInfo(
            pytest.mark.xfail,
            executors=("TorchEx",),
            dtypes=(datatypes.complexfloating,),
        ),
    ),
)
shape_ops.append(pad_opinfo)


def slice_in_dim_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape, start_index, limit_index, stride, dim
    cases = (
        ((4, 6, 7), 1, 3, 2, 1),
        ((4, 6, 7), 0, -1, 3, 2),
    )

    for shape, start_idx, limit_idx, stride, dim in cases:
        a = make(shape)
        yield SampleInput(a, start_idx, limit_idx, stride, dim)


slice_in_dim = OpInfo(
    tlang.slice_in_dim,
    sample_input_generator=slice_in_dim_sample_generator,
    jax_reference=jax.lax.slice_in_dim if JAX_AVAILABLE else None,
    test_directives=(
        # nvFuser executor doesn't support pad correctly
        # See https://github.com/Lightning-AI/lightning-thunder/issues/285
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
shape_ops.append(slice_in_dim)


# TODO: add stride testing
def slice_prim_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape, start_indices, end_indices
    cases = (
        ((5, 7, 8), (1, 0, 3), (2, 6, 8)),
        ((3,), (1,), (2,)),
    )

    for shape, start_indices, end_indices in cases:
        a = make(shape)
        yield SampleInput(a, start_indices, end_indices)


slice_prim_opinfo = OpInfo(
    prims.slice_prim,
    name="slice_prim",
    sample_input_generator=slice_prim_sample_generator,
    jax_reference=jax.lax.slice if JAX_AVAILABLE else None,
    test_directives=(
        # nvFuser executor doesn't support pad correctly
        # See https://github.com/Lightning-AI/lightning-thunder/issues/285
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
shape_ops.append(slice_prim_opinfo)


def split_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape, size_or_sections, dim
    cases = (
        ((4, 6, 7), 2, 0),
        ((4, 6, 7), 3, 0),
        ((4, 6, 7), 3, -1),
        ((4, 6, 7), 9, 1),
        ((4, 6, 7), (1, 2, 1, 2), 1),
        ((4, 6, 7), (3, 1, 2, 0, 0, 1), -1),
        ((4, 4, 12), 4, 2),
    )

    for shape, size_or_sections, dim in cases:
        yield SampleInput(make(shape), size_or_sections, dim)


split_opinfo = OpInfo(
    ttorch.split,
    sample_input_generator=split_sample_generator,
    torch_reference=torch.split,
    test_directives=(
        # nvFuser executor doesn't support pad correctly
        # See https://github.com/Lightning-AI/lightning-thunder/issues/285
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
shape_ops.append(split_opinfo)


def squeeze_torch_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # a.shape, dim
    cases = (
        ((1, 2, 1, 1, 3, 1), None),
        ((), None),
        ((1, 1, 1), None),
        ((1, 2, 1, 1, 3, 1), 0),
        ((1, 2, 1, 1, 3, 1), 2),
        ((1, 2, 1, 1, 3, 1), 5),
        ((1, 2, 1, 1, 3, 1), (2, 3)),
        ((1, 1, 1), (0, 1, 2)),
    )

    for shape, dim in cases:
        a = make(shape)
        yield SampleInput(a, dim)


def torch_squeeze_helper(a, dim):
    # TODO: dim as a sequence is only supported on PyTorch 2.0 and greater
    if isinstance(dim, Sequence):
        for dim in sorted(dim, reverse=True):
            a = a.squeeze(dim)
        return a

    if dim is None:
        return torch.squeeze(a)

    # dim is a number
    return torch.squeeze(a, dim)


squeeze_torch_opinfo = OpInfo(
    ttorch.squeeze,
    name="squeeze_torch",
    sample_input_generator=squeeze_torch_sample_generator,
    torch_reference=torch_squeeze_helper,
)
shape_ops.append(squeeze_torch_opinfo)


def squeeze_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # a.shape, dim
    cases = (
        ((1, 2, 1, 1, 3, 1), (2, 3)),
        ((1, 1, 1), (0, 1, 2)),
    )

    for shape, dim in cases:
        a = make(shape)
        yield SampleInput(a, dim)


squeeze_opinfo = OpInfo(
    tlang.squeeze,
    sample_input_generator=squeeze_sample_generator,
    jax_reference=jax.lax.squeeze if JAX_AVAILABLE else None,
)
shape_ops.append(squeeze_opinfo)


def tensor_split_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape, indices_or_sections, dim
    cases = (
        ((4, 6, 7), 2, 1),
        ((4, 6, 7), 2, 2),
        ((4, 6, 7), 3, 0),
        ((4, 6, 7), 5, -1),
        ((4, 6, 7), (0, 1), 1),
        ((4, 6, 7), (1, 5, 6), 2),
        ((4, 6, 7), (1, 5, 9, 9), 2),
        ((4, 6, 7), (1, 5, 6, 7), 2),
        ((4, 6, 7), (0, 0, 1, 1, 2), -2),
    )

    for shape, indices_or_sections, dim in cases:
        yield SampleInput(make(shape), indices_or_sections, dim)


tensor_split_opinfo = OpInfo(
    ttorch.tensor_split,
    sample_input_generator=tensor_split_sample_generator,
    torch_reference=torch.tensor_split,
    test_directives=(
        # nvFuser executor doesn't support pad correctly
        # See https://github.com/Lightning-AI/lightning-thunder/issues/285
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
shape_ops.append(tensor_split_opinfo)


def transpose_torch_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # tensor shape, dim0, dim1
    cases = (
        ((2, 12, 1024, 64), 1, 2),
        ((4, 3, 2), 0, -1),
        ((4, 3, 2), 0, -2),
        ((4, 3, 2), 1, 2),
        ((1, 2), 0, -1),
        ((5,), 0, 0),
    )

    for shape, dim0, dim1 in cases:
        yield SampleInput(make(shape), dim0, dim1)


transpose_torch_opinfo = OpInfo(
    ttorch.transpose,
    name="torch_transpose",
    sample_input_generator=transpose_torch_sample_generator,
    torch_reference=torch.transpose,
)
shape_ops.append(transpose_torch_opinfo)


def transpose_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape, perm
    cases = (
        ((4, 7, 8), (0, 1, 2)),
        ((4, 7, 8), (1, 2, 0)),
        ((4, 7, 8), (2, 1, 0)),
        ((4, 7, 8), (0, 2, 1)),
        ((4, 7, 8), (0, -1, 1)),
        ((4, 7), (1, 0)),
    )

    for shape, perm in cases:
        yield SampleInput(make(shape), perm)


transpose_opinfo = OpInfo(
    tlang.transpose,
    sample_input_generator=transpose_sample_generator,
    torch_reference=torch.permute,
)
shape_ops.append(transpose_opinfo)


def take_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_index = partial(make_tensor, device=device, requires_grad=False)

    # a.shape, dim, b.shape
    cases = (
        ((4, 2, 3), 0, (8)),
        ((4, 2, 3), 1, (7)),
        ((4, 2, 3), 2, (2)),
        ((4,), 0, (8)),
        ((4,), 0, (1)),
        ((4, 1), 0, (3)),
        ((4, 1), 1, (5)),
        ((1, 0, 3), 0, (8)),
        # TODO: FIXME
        # nvFuser: index array can't have zero elements
        # ((4, 2, 3), 0, (0)),
        # ((4, 2, 3), 1, (0)),
        # ((4, 2, 3), 2, (0)),
        # nvFuser: index array must be 1D
        # ((4, 2, 3), 0, ()),
        # ((4, 2, 3), 1, ()),
        # ((4, 2, 3), 2, ()),
    )

    for shape_a, dim, shape_b in cases:
        for index_dtype in [torch.int, torch.long]:
            a = make(shape_a)
            b = make_index(shape_b, low=0, high=shape_a[dim], dtype=index_dtype)
            yield SampleInput(a, b, dim)


def torch_index_select_wrapper(a, b, dim):
    return torch.index_select(a, dim, b)


# TODO: mapping jax.lax.gather for testing
take_opinfo = OpInfo(
    tlang.take,
    sample_input_generator=take_sample_generator,
    torch_reference=torch_index_select_wrapper,
    test_directives=(
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.3",
        ),
    ),
)
shape_ops.append(take_opinfo)


def take_along_axis_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    # torch.take_along_dim expects index to be long but not int
    make_index = partial(make_tensor, device=device, dtype=torch.long, requires_grad=False)

    # a.shape, dim, b.shape
    cases = (
        ((4, 2, 3), 0, (8, 2, 3)),
        ((4, 2, 3), 1, (4, 1, 3)),
        ((4, 2, 3), 2, (4, 2, 5)),
        ((4,), 0, (8)),
        ((4,), 0, (1)),
        ((4, 1), 0, (3, 1)),
        ((4, 1), 1, (4, 5)),
        # broadcasting is supported by numpy
        ((4, 2, 3), 2, (1, 2, 7)),
    )

    for shape_a, dim, shape_b in cases:
        a = make(shape_a)
        b = make_index(shape_b, low=0, high=shape_a[dim])
        yield SampleInput(a, b, dim)


# TODO: mapping jax.lax.gather for testing
take_along_axis_opinfo = OpInfo(
    tlang.take_along_axis,
    sample_input_generator=take_along_axis_sample_generator,
    torch_reference=torch.take_along_dim,
    # Torch doesn't support complex half on take_along_dim
    test_directives=(
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
        ),
    ),
)
shape_ops.append(take_along_axis_opinfo)


def unsqueeze_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # a.shape, dims
    cases = (
        ((4, 2), (0, 1, 4)),
        ((2, 1, 3), ()),
        ((2, 1, 3), (-1,)),
        ((2, 1, 3), (-1, 1, 2, -2)),
        ((), (0, -1)),
        ((2, 2), (1,)),
    )

    for shape, dims in cases:
        a = make(shape)
        yield SampleInput(a, dims)


unsqueeze_opinfo = OpInfo(
    tlang.unsqueeze,
    sample_input_generator=unsqueeze_sample_generator,
    jax_reference=jax.lax.expand_dims if JAX_AVAILABLE else None,
    test_directives=(
        # See https://github.com/csarofeen/pytorch/issues/2549
        DecorateInfo(
            pytest.mark.skip,
            "test_jvp_correctness",
            executors=("nvFuser",),
        ),
        DecorateInfo(
            pytest.mark.skip,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
shape_ops.append(unsqueeze_opinfo)


def convert_element_type_sample_generator(op, device, dtype, requires_grad, **kwargs):
    a = make_tensor((2, 3, 4), device=device, dtype=dtype, requires_grad=requires_grad)

    # TODO: add more source and target dtype pairs
    yield SampleInput(a, torch.float32)


convert_element_type_opinfo = OpInfo(
    prims.convert_element_type,
    sample_input_generator=convert_element_type_sample_generator,
    torch_reference=torch.Tensor.to,
    jax_reference=jax.lax.convert_element_type if JAX_AVAILABLE else None,
    test_directives=(
        # These usually pass but tols are still too tight to perform these tests
        DecorateInfo(
            pytest.mark.skip,
            "test_vjp_correctness",
        ),
    ),
)
shape_ops.append(convert_element_type_opinfo)


opinfos.extend(shape_ops)

#
# Reduction OpInfos
#
reduction_ops = []


# TODO: increase reduction samples and refacort amax and sum generators
def amax_amin_sample_generator(op, device, dtype, requires_grad, **kwargs):
    # For grad test stability it's better to use wider range of values
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad, low=-1000, high=1000)

    # shape, dim, keepdim
    cases = (
        ((4, 4), None, False),
        ((8, 1, 6), (1,), True),
        ((8, 7, 5, 1), (0, 1), False),
    )

    for shape, dim, keepdim in cases:
        yield (SampleInput(make(shape), dim, keepdim))


amax_opinfo = OpInfo(
    ttorch.amax,
    sample_input_generator=amax_amin_sample_generator,
    torch_reference=torch.amax,
    # Complex numbers are unordered
    dtypes=(datatypes.exact, datatypes.floating),
)
reduction_ops.append(amax_opinfo)


amin_opinfo = OpInfo(
    ttorch.amin,
    sample_input_generator=amax_amin_sample_generator,
    torch_reference=torch.amin,
    # Complex numbers are unordered
    dtypes=(datatypes.exact, datatypes.floating),
)
reduction_ops.append(amin_opinfo)


def reduction_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(
        make_tensor,
        device=device,
        dtype=dtype,
        requires_grad=requires_grad,
        # We set low (inclusive) and high (exclusive) here to avoid values
        # whose products can otherwise become extremely large
        low=-2,
        high=3,
    )

    # shape, dim, keepdim, dtype
    cases = (
        ((4, 4), None, False, None),
        ((5,), None, True, None),
        ((5,), 0, False, None),
        ((8, 1, 6), 1, True, None),
        ((8, 7, 5, 1), (0, 1), True, None),
        ((8, 7, 5, 1), (1, 3), False, None),
        # torch.prod() behaves differently when passing `dim=None` compared to
        # simply omitting the argument, due to pybind11's overload resolution
        # mechanism. Passing `None` in those cases instead leads to an error.
        # We test this behavior explicitly in order to try and catch such edge
        # cases.
        ((4, 4), None),
    )

    for c in cases:
        if len(c) == 2:
            shape, dim = c
            yield (SampleInput(make(shape), dtype=dtype))
        else:
            shape, dim, keepdim, dtype = c
            yield (SampleInput(make(shape), dim, keepdim, dtype=dtype))


prod_opinfo = OpInfo(
    ttorch.prod,
    sample_input_generator=reduction_sample_generator,
    torch_reference=torch._refs.prod,
    test_directives=(
        # Torch doesn't support cpu real (float16) or complex half prod
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32, datatypes.float16),
            devicetypes=("cpu",),
        ),
        # See https://github.com/csarofeen/pytorch/issues/2369
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.complexfloating,),
            executors=("nvFuser",),
        ),
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.4",
        ),
    ),
)
reduction_ops.append(prod_opinfo)


sum_opinfo = OpInfo(
    ttorch.sum,
    sample_input_generator=reduction_sample_generator,
    torch_reference=torch.sum,
    test_directives=(
        # Torch doesn't support cpu complex half sum
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
            devicetypes=("cpu",),
        ),
        # See https://github.com/csarofeen/pytorch/issues/2369
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.complexfloating,),
            executors=("nvFuser",),
        ),
        # Some PyTorch versions before PyTorch 1.13 throw a runtime error
        #   insisting, incorrectly, that dimensions be specified by name
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            active_if=LooseVersion(torch.__version__) < "1.13",
        ),
    ),
)
reduction_ops.append(sum_opinfo)


def var_sample_generator(op, device, dtype, requires_grad):
    unbiased = (None, True, False)
    correction = (None, 0, 1)
    samples = reduction_sample_generator(op, device, dtype, requires_grad)
    for u, c, sample in itertools.product(unbiased, correction, samples):
        a = sample.args[0]
        dim = sample.args[1] if len(sample.args) > 1 else None
        keepdim = sample.args[2] if len(sample.args) > 2 else False
        # cannot specify both correction and unbiased arguments
        if u is not None and c is not None:
            continue
        elif u is not None:
            yield SampleInput(a, dim, u, keepdim)
        elif c is not None:
            yield SampleInput(a, dim, keepdim=keepdim, correction=c)
        else:
            yield SampleInput(a, dim, keepdim)

    # TODO: nvFuser's var and var_mean doesn't support 0-dim input
    # re-enable this sample when https://github.com/NVIDIA/Fuser/pull/121 is merged
    # yield SampleInput(make_tensor((), device=device, dtype=dtype, requires_grad=requires_grad))


mean_opinfo = OpInfo(
    ttorch.mean,
    sample_input_generator=reduction_sample_generator,
    torch_reference=torch.mean,
    dtypes=(datatypes.floating, datatypes.complexfloating),
    test_directives=(
        # PyTorch doesn't support CPU and CUDA complex half mean
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.complex32,),
            devicetypes=("cpu", "cuda"),
        ),
        # See https://github.com/csarofeen/pytorch/issues/2369
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.complexfloating,),
            executors=("nvFuser",),
        ),
        # PyTorch CPU bfloat16 doesn't pass test_core_vs_torch_consistency
        # but CUDA bfloat16 passes
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cpu",),
        ),
    ),
)
reduction_ops.append(mean_opinfo)

var_mean_opinfo = OpInfo(
    ttorch.var_mean,
    sample_input_generator=var_sample_generator,
    torch_reference=torch.var_mean,
    # Complex var is not supported yet
    dtypes=(datatypes.floating,),
    test_directives=(
        # bfloat16 on CPU has accuracy things
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
            devicetypes=("cpu",),
        ),
        # PyTorch doesn't support float16 and bfloat16 on CUDA
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16, datatypes.bfloat16),
            devicetypes=("cuda",),
        ),
        # NotImplementedError: VJP for Ops.VAR is not implemented
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("TorchEx",),
        ),
        # Internal nvFuser error
        # RuntimeError: producer->getMemoryType() == MemoryType::Global
        DecorateInfo(
            pytest.mark.xfail,
            executors=("nvFuser",),
            active_if=nvFuser().version() < "0.0.7",
        ),
    ),
)
reduction_ops.append(var_mean_opinfo)

opinfos.extend(reduction_ops)

#
# Tensor Creation OpInfos
#
tensor_creation_ops = []


def arange_sample_generator(op, device, dtype, requires_grad, **kwargs):
    # start, end, step
    common_cases = (
        (0, 1, 2),
        (-5, -8, -1),
        (-3, 11, 3),
    )
    extra_cases = ()

    if datatypes.is_inexact_dtype(dtype):
        # start, end, step
        extra_cases = (
            (5, 11, 0.3),
            (3, -4.2, -1),
        )

    for start, end, step in itertools.chain(common_cases, extra_cases):
        yield SampleInput(start=start, end=end, step=step, dtype=dtype, device=device)


arange_opinfo = OpInfo(
    ttorch.arange,
    sample_input_generator=arange_sample_generator,
    torch_reference=torch.arange,
    dtypes=(datatypes.signedinteger, datatypes.unsignedinteger, datatypes.floating),
    test_directives=(
        # https://github.com/csarofeen/pytorch/issues/2370
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.bfloat16, datatypes.float16),
            executors=("nvFuser",),
        ),
    ),
)
tensor_creation_ops.append(arange_opinfo)


# TODO: match fill values to dtype
def full_sample_generator(op, device, dtype, requires_grad, **kwargs):
    # shape, fill_value
    cases = (
        # ((), .5),  # FIXME: https://github.com/csarofeen/pytorch/issues/2358
        ((4, 4), 1),
        ((8, 1, 6), 1),
        ((8, 7, 5, 1), 1),
    )

    for shape, fill_value in cases:
        yield SampleInput(shape, fill_value, device=device, dtype=dtype)


full_opinfo = OpInfo(
    tlang.full,
    sample_input_generator=full_sample_generator,
    torch_reference=torch.full,
)
tensor_creation_ops.append(full_opinfo)


def empty_sample_generator(op, device, dtype, requires_grad, **kwargs):
    cases = (
        # (),  # FIXME: https://github.com/csarofeen/pytorch/issues/2358
        (1,),
        (4, 4),
        # (2, 0, 3),  # FIXME: nvFuser does not yet support shapes with 0-sized dimensions
        (8, 1, 6),
        (8, 7, 5, 1),
    )

    for shape in cases:
        yield SampleInput(shape, device=device, dtype=dtype)


empty_opinfo = OpInfo(
    ttorch.empty,
    sample_input_generator=empty_sample_generator,
    torch_reference=torch.zeros,
)
tensor_creation_ops.append(empty_opinfo)


opinfos.extend(tensor_creation_ops)

#
# Matmul OpInfos
#
matmul_ops = []


def matmul_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    M = 4
    N = 3
    B = 2
    # shape_a, shape_b
    cases = (
        ((M,), (M,)),
        ((M,), (M, N)),
        ((M, N), (N,)),
        ((M,), (B, M, N)),
        ((B, M, N), (N,)),
        ((M, N), (N, M)),
        ((B, M, N), (B, N, M)),
        ((B, B, M, N), (B, B, N, M)),
    )

    for shape_a, shape_b in cases:
        yield SampleInput(make(shape_a), make(shape_b))


matmul_opinfo = OpInfo(
    ttorch.matmul,
    sample_input_generator=matmul_sample_generator,
    torch_reference=torch.matmul,
    dtypes=(datatypes.floating, datatypes.complexfloating),
    test_directives=(
        # PyTorch CPU doesn't support float16 matmul
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # PyTorch doesn't support complex32 matmul
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.complex32,),
            devicetypes=("cpu", "cuda"),
        ),
    ),
)
matmul_ops.append(matmul_opinfo)


def linear_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    in_features = 3
    out_features = 5
    batch_size = 2
    # shape_input, shape_weight
    cases_no_bias = (
        ((in_features,), (out_features, in_features)),
        ((batch_size, in_features), (out_features, in_features)),
    )

    for shape_input, shape_weight in cases_no_bias:
        yield SampleInput(make(shape_input), make(shape_weight))

    # shape_input, shape_weight, shape_bias
    cases_with_bias = (
        ((in_features,), (out_features, in_features), (out_features,)),
        ((batch_size, in_features), (out_features, in_features), (out_features,)),
    )

    for shape_input, shape_weight, shape_bias in cases_with_bias:
        yield SampleInput(make(shape_input), make(shape_weight), make(shape_bias))


linear_opinfo = OpInfo(
    ttorch.linear,
    sample_input_generator=linear_sample_generator,
    torch_reference=torch.nn.functional.linear,
    dtypes=(datatypes.floating, datatypes.complexfloating),
    test_directives=(
        # PyTorch CPU doesn't support float16 linear
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # PyTorch doesn't support complex32 linear
        DecorateInfo(
            pytest.mark.xfail,
            dtypes=(datatypes.complex32,),
            devicetypes=("cpu", "cuda"),
        ),
    ),
)
matmul_ops.append(linear_opinfo)

opinfos.extend(matmul_ops)

#
# NN Ops
#
nn_ops = []


# TODO: improve sample generation, test dtype argument
def softmax_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    S = 2
    M = 5
    # Shape, dim, dtype
    cases = (
        ((S,), 0),
        ((S, S), 0),
        ((S, S), 1),
        ((S, S), -1),
        ((S, M, S), 2),
        ((), 0),
    )

    for shape, dim in cases:
        yield SampleInput(make(shape), dim)


softmax_opinfo = OpInfo(
    ttorch.softmax,
    sample_input_generator=softmax_sample_generator,
    torch_reference=None if LooseVersion(torch.__version__) < "1.13" else torch._refs.softmax,
    dtypes=(datatypes.floating,),
    test_directives=(
        # torch.softmax doesn't support float16 on CPU
        # RuntimeError: "softmax_lastdim_kernel_impl" not implemented for 'Half'
        DecorateInfo(
            pytest.mark.xfail,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.float16,),
            devicetypes=("cpu",),
        ),
        # Tolerances are currently too conservative for this test with half precision
        DecorateInfo(
            pytest.mark.skip,
            "test_core_vs_torch_consistency",
            dtypes=(datatypes.bfloat16,),
        ),
        # TypeError: sum(): incompatible function arguments.
        # See: https://github.com/Lightning-AI/lightning-thunder/issues/269
        DecorateInfo(
            pytest.mark.xfail,
            "test_vjp_correctness",
            executors=("nvFuser",),
        ),
    ),
)
nn_ops.append(softmax_opinfo)


def embedding_sample_generator(op, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    N = 5
    S = 2
    # indices_shape, weight_shape, padding_idx, max_norm, norm_type, scale_grad_by_freq, sparse
    cases = (
        ((S,), (N, S), None, None, 2.0, False, False),
        ((S,), (N, S), 0, None, 2.0, False, False),
        ((S,), (N, S), None, None, 2.0, True, False),
        # nvFuser executor would raise an error when running this test
        # PyTorch works fine
        # RuntimeError: unsupported memory format option Contiguous
        # Because sparse=True, the output tensor is always in sparse format
        # ((S,), (N, S), None, None, 2.0, False, True),
    )

    for indices_shape, weight_shape, padding_idx, max_norm, norm_type, scale_grad_by_freq, sparse in cases:
        yield SampleInput(
            make(indices_shape, low=0, high=N, dtype=torch.long, requires_grad=False),
            make(weight_shape),
            padding_idx=padding_idx,
            max_norm=max_norm,
            norm_type=norm_type,
            scale_grad_by_freq=scale_grad_by_freq,
            sparse=sparse,
        )


embedding_opinfo = OpInfo(
    ttorch.embedding,
    sample_input_generator=embedding_sample_generator,
    torch_reference=torch.nn.functional.embedding,
    dtypes=(datatypes.floating, datatypes.complexfloating),
)
nn_ops.append(embedding_opinfo)


opinfos.extend(nn_ops)
