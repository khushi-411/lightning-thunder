from thunder.executors import triton_utils
from thunder.extend import OperatorExecutor

triton_version: None | str = triton_utils.triton_version()

triton_ex: None | OperatorExecutor = None
if triton_version is not None:
    try:
        from thunder.executors.triton_crossentropy_impl import triton_ex as impl_ex

        triton_ex = impl_ex
    except Exception:
        import warnings

        warnings.warn("triton is present but cannot be initialized")
        triton_version = None
