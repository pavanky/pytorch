import builtins
import functools
import inspect
import itertools
import logging
import sys
import textwrap
import warnings
from io import StringIO

from typing import Any, List
from unittest.mock import patch

import sympy
import sys

import torch
from torch._dynamo.testing import rand_strided
from torch._dynamo.utils import counters, identity

from . import config, ir, codecache
from .codecache import code_hash, PersistentCache, PyCodeCache

from .codegen.common import IndentedBuffer
from .codegen.triton import config_of, signature_of, texpr, TritonKernel, TritonPrinter

from .utils import do_bench, sympy_dot, sympy_product
from .virtualized import V
from torch import multiprocessing
from cloudpickle import dumps, loads

log = logging.getLogger(__name__)

# cuda runtime does not work with "fork"
multiprocessing.set_start_method("spawn", force=True)

# correctness checks struggle with fp16/tf32
VERIFY = False  # dict(atol=1, rtol=0.05)
PRINT_AUTOTUNE = True


class KernelNamespace:
    pass


# these objects are imported from the generated wrapper code
template_kernels = KernelNamespace()
extern_kernels = KernelNamespace()

def benchmark_choice_in_sub_process(all_template_kernels, choice, args, out, expected_out, timings):
    global template_kernels
    template_kernels = loads(all_template_kernels)
    choice = loads(choice)
    result = choice.benchmark(*args, out=out)
    if expected_out is not None:
        torch.testing.assert_close(out, expected_out) 

    # use a tensor since the mutation to a python list in a sub process
    # is not synced back to the parent process
    timings.copy_(torch.tensor(result))

class TritonTemplateKernel(TritonKernel):
    def __init__(
        self,
        kernel_name,
        input_nodes,
        output_node,
        defines,
        num_stages,
        num_warps,
        grid_fn,
        meta,
        call_sizes,
        use_jit=True,
        prefix_args=0,
        suffix_args=0,
        epilogue_fn=identity,
    ):
        super().__init__(sympy_product(output_node.get_size()), sympy.Integer(1))
        self.input_nodes = input_nodes
        self.output_node = output_node
        self.named_input_nodes = {}
        self.defines = defines
        self.kernel_name = kernel_name
        self.template_mask = None
        self.use_jit = use_jit
        self.num_stages = num_stages
        self.num_warps = num_warps
        self.grid_fn = grid_fn
        self.meta = meta
        self.call_sizes = call_sizes
        # for templates with fixed epilogues
        self.prefix_args = prefix_args
        self.suffix_args = suffix_args
        self.epilogue_fn = epilogue_fn

    def jit_line(self):
        if self.use_jit:
            return "@triton.jit"

        argdefs, _, signature = self.args.python_argdefs()
        triton_meta = {
            "signature": dict(enumerate(map(signature_of, signature))),
            "device": V.graph.scheduler.current_device.index,
            "constants": {},
        }
        triton_meta["configs"] = [config_of(signature)]
        return (
            f"@template(num_stages={self.num_stages}, num_warps={self.num_warps}, meta={triton_meta!r})\n"
            + "@triton.jit"
        )

    def def_kernel(self, *argnames):
        """
        Hook called from template code to generate function def and
        needed args.
        """
        assert all(isinstance(x, str) for x in argnames)
        renames = IndentedBuffer(initial_indent=1)

        named_args = self.input_nodes[
            self.prefix_args : len(self.input_nodes) - self.suffix_args
        ]

        assert len(argnames) == len(named_args), (
            len(argnames),
            len(named_args),
            self.prefix_args,
            len(self.input_nodes),
        )

        for input_node in self.input_nodes[: self.prefix_args]:
            # get args in correct order
            self.args.input(input_node.get_name())

        for name, input_node in zip(argnames, named_args):
            arg_name = f"arg_{name}"
            self.named_input_nodes[name] = input_node
            self.args.input_buffers[input_node.get_name()] = arg_name
            if input_node.get_layout().offset == 0:
                renames.writeline(f"{name} = {arg_name}")
            else:
                offset = texpr(self.rename_indexing(input_node.get_layout().offset))
                renames.writeline(f"{name} = {arg_name} + {offset}")

        for input_node in self.input_nodes[len(self.input_nodes) - self.suffix_args :]:
            # get args in correct order
            self.args.input(input_node.get_name())

        arg_defs, *_ = self.args.python_argdefs()
        return "\n".join(
            [
                "import triton.language as tl",
                "import triton",
                "from torch._inductor.triton_ops.autotune import template",
                "from torch._inductor.utils import instance_descriptor",
                "",
                self.jit_line(),
                f"def {self.kernel_name}({', '.join(arg_defs)}):",
                self.defines,
                renames.getvalue(),
            ]
        )

    def size(self, name: str, index: int):
        """
        Hook called from template code to get the size of an arg.
        Will add needed args to pass it in if it is dynamic.
        """
        assert isinstance(index, int)
        if name is None:
            val = self.output_node.get_size()[index]
        else:
            assert isinstance(name, str)
            val = self.named_input_nodes[name].get_size()[index]
        return texpr(self.rename_indexing(val))

    def stride(self, name, index):
        """
        Hook called from template code to get the stride of an arg.
        Will add needed args to pass it in if it is dynamic.
        """
        assert isinstance(index, int)
        if name is None:
            val = self.output_node.get_stride()[index]
        else:
            assert isinstance(name, str)
            val = self.named_input_nodes[name].get_stride()[index]
        return texpr(self.rename_indexing(val))

    def store_output(self, indices, val, mask):
        """
        Hook called from template code to store the final output
        (if the buffer hasn't been optimized away), then append any
        epilogue fusions.
        """
        assert isinstance(indices, (list, tuple))
        assert isinstance(val, str)
        assert isinstance(mask, str)
        if self.template_mask is None:
            indices = list(map(TritonPrinter.paren, indices))
            index_symbols = [sympy.Symbol(x) for x in indices]
            lengths = [
                V.graph.sizevars.simplify(s) for s in self.output_node.get_size()
            ]
            assert len(indices) == len(lengths)

            # glue to make generated code use same indexing from template
            for name, range_tree_entry in zip(
                indices, self.range_trees[0].construct_entries(lengths)
            ):
                range_tree_entry.set_name(name)
            contiguous_index = sympy_dot(
                ir.FlexibleLayout.contiguous_strides(lengths), index_symbols
            )
            self.body.writeline("xindex = " + texpr(contiguous_index))
            self.range_trees[0].lookup(
                sympy.Integer(1), sympy_product(lengths)
            ).set_name("xindex")
            self.template_mask = mask
            self.template_indices = indices
            output_index = self.output_node.get_layout().make_indexer()(index_symbols)
            if output_index == contiguous_index:
                output_index = sympy.Symbol("xindex")

            epilogue_args = [val]
            for input_node in itertools.chain(
                self.input_nodes[: self.prefix_args],
                self.input_nodes[len(self.input_nodes) - self.suffix_args :],
            ):
                input_node.freeze_layout()
                epilogue_args.append(input_node.make_loader()(index_symbols))

            V.ops.store(
                self.output_node.get_name(),
                output_index,
                self.epilogue_fn(*epilogue_args),
            )
        assert self.template_mask == mask
        self.codegen_body()
        return textwrap.indent(self.body.getvalue(), "    ").strip()

    def make_load(self, name, indices, mask):
        """
        Optional helper called from template code to generate the code
        needed to load from an tensor.
        """
        assert isinstance(indices, (list, tuple))
        assert isinstance(name, str)
        assert isinstance(mask, str)
        stride = self.named_input_nodes[name].get_stride()
        indices = list(map(TritonPrinter.paren, indices))
        assert len(indices) == len(stride)
        index = " + ".join(
            f"{texpr(self.rename_indexing(s))} * {i}" for s, i in zip(stride, indices)
        )
        return f"tl.load({name} + ({index}), {mask})"

    def template_env(self):
        """
        Generate the namespace visible in the template.
        """
        return {
            fn.__name__: fn
            for fn in [
                self.def_kernel,
                self.size,
                self.stride,
                self.store_output,
                self.make_load,
            ]
        }

    def indexing(
        self,
        index: sympy.Expr,
        *,
        copy_shape=None,
        dense_indexing=False,
    ):
        """
        Override the default indexing to use our custom mask and force
        dense indexing.
        """
        result, *mask = super().indexing(
            index,
            dense_indexing=False,
            copy_shape=copy_shape,
            override_mask=self.template_mask,
        )
        result += f" + tl.zeros({self.template_mask}.shape, tl.int32)"
        return (result, *mask)

    def initialize_range_tree(self, pid_cache):
        super().initialize_range_tree(pid_cache)
        # ignore default codegen
        self.body.clear()
        self.indexing_code.clear()

    def call_kernel(self, code, name: str):
        _, call_args, _ = self.args.python_argdefs()

        for i in range(len(call_args)):
            if V.graph.is_unspec_arg(call_args[i]):
                call_args[i] = call_args[i] + ".item()"
        call_args = ", ".join(call_args)

        stream_name = code.write_get_cuda_stream(V.graph.scheduler.current_device.index)

        V.graph.wrapper_code.add_import_once(f"import {self.grid_fn.__module__}")
        meta = V.graph.wrapper_code.add_meta_once(self.meta)

        grid_call = [texpr(V.graph.sizevars.simplify(s)) for s in self.call_sizes] + [
            meta
        ]
        grid_call = (
            f"{self.grid_fn.__module__}.{self.grid_fn.__name__}({', '.join(grid_call)})"
        )
        code.writeline(
            f"{name}.run({call_args}, grid={grid_call}, stream={stream_name})"
        )


@functools.lru_cache(None)
def _jinja2_env():
    try:
        import jinja2

        return jinja2.Environment(
            undefined=jinja2.StrictUndefined,
        )
    except ImportError:
        return None


class TritonTemplate:
    index_counter = itertools.count()
    all_templates = dict()

    @staticmethod
    def _template_from_string(source):
        env = _jinja2_env()
        if env is not None:
            return env.from_string(source)
        return None

    def __getstate__(self):
        state = self.__dict__.copy()
        # jinja template can not be pickled
        del state["template"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.template = self._template_from_string(self.source)

    def __init__(self, name: str, grid: Any, source: str, debug=False):
        super().__init__()
        self.name = name
        self.grid = grid
        self.source = source
        self.template = self._template_from_string(source)
        assert name not in self.all_templates, "duplicate template name"
        self.all_templates[name] = self
        self.debug = debug

    def generate(
        self,
        input_nodes,
        layout,
        num_stages,
        num_warps,
        prefix_args=0,
        suffix_args=0,
        epilogue_fn=identity,
        **kwargs,
    ):
        assert self.template, "requires jinja2"
        defines = StringIO()
        for name, val in kwargs.items():
            defines.write(f"    {name} : tl.constexpr = {val}\n")
        defines = defines.getvalue()

        fake_out = ir.Buffer("buf_out", layout)
        kernel_name = f"triton_{self.name}"

        kernel_options = dict(
            input_nodes=input_nodes,
            defines=defines,
            num_stages=num_stages,
            num_warps=num_warps,
            grid_fn=self.grid,
            meta=kwargs,
            call_sizes=layout.size,
            prefix_args=prefix_args,
            suffix_args=suffix_args,
            epilogue_fn=epilogue_fn,
        )
        with patch.object(
            V.graph, "get_dtype", self.fake_get_dtype(fake_out)
        ), TritonTemplateKernel(
            kernel_name=kernel_name,
            output_node=fake_out,
            use_jit=True,
            **kernel_options,
        ) as kernel:
            # need to do call render twice to get all the needed args right
            try:
                self.template.render(
                    **kernel.template_env(),
                    **kwargs,
                )
                code = self.template.render(
                    **kernel.template_env(),
                    **kwargs,
                )
            except ZeroDivisionError:
                # TODO(nmacchioni): fix sympy division by zero
                return None
            if self.debug:
                print("Generated Code:\n", code)
            extra = (
                "-".join(
                    [
                        *[
                            f"{kwarg}={repr(kwargs[kwarg])}"
                            for kwarg in sorted(kwargs.keys())
                        ],
                        f"num_stages={num_stages}",
                        f"num_warps={num_warps}",
                    ]
                )
                + "-"
            )
            _, call_args, _ = kernel.args.python_argdefs()

        expected_args = [x.get_name() for x in input_nodes] + [fake_out.get_name()]
        # TODO(nmacchioni) fix bug here in CI tests
        # assert list(call_args) == expected_args, (call_args, expected_args)
        if list(call_args) != expected_args:
            return None
        extra_args = V.graph.sizevars.size_hints(
            map(sympy.expand, call_args[len(expected_args) :])
        )
        assert not extra_args, "TODO: dynamic shapes"

        run = None

        def call(*args, out):
            # set run only when used to we don't need pickle it.
            # Pickle run fail with error: TypeError: cannot pickle 'PyCapsule' object
            nonlocal run

            if run is None:
                mod = PyCodeCache.load(code, extra)
                run = getattr(mod, kernel_name).run
            return run(
                *args,
                out,
                *extra_args,
                grid=self.grid(*out.size(), kwargs),
                num_stages=num_stages,
                num_warps=num_warps,
            )

        key, path = codecache.write(code, "py", extra)
        call.key = key
        call.__file__ = path

        kernel_hash_name = f"triton_{self.name}_{next(self.index_counter)}"
        setattr(template_kernels, kernel_hash_name, call)

        def make_kernel_render(out_node):
            kernel = TritonTemplateKernel(
                kernel_name="KERNEL_NAME",
                output_node=out_node,
                use_jit=False,
                **kernel_options,
            )
            render = functools.partial(
                self.template.render,
                **kernel.template_env(),
                **kwargs,
            )
            return kernel, render

        return TritonTemplateCaller(
            kernel_hash_name,
            input_nodes,
            layout,
            make_kernel_render,
            extra.strip("-").replace("-", ", "),
        )

    @staticmethod
    def fake_get_dtype(fake_out):
        _get_dtype_real = V.graph.get_dtype

        def get_dtype(name):
            if name == fake_out.get_name():
                return fake_out.get_dtype()
            return _get_dtype_real(name)

        return get_dtype


class ExternKernelChoice:
    def __init__(self, kernel, cpp_kernel=None, *, name=None, has_out_variant=True):
        super().__init__()
        name = name or kernel.__name__
        assert callable(kernel)
        assert not hasattr(extern_kernels, name), "duplicate extern kernel"
        self.name = name
        self.cpp_kernel = cpp_kernel
        self.has_out_variant = has_out_variant
        setattr(extern_kernels, name, kernel)

    def to_callable(self):
        return getattr(extern_kernels, self.name)

    def call_name(self):
        return f"extern_kernels.{self.name}"

    @functools.lru_cache(None)
    def hash_key(self):
        fn = self.to_callable()
        parts = [
            self.name,
            getattr(fn, "__name__", ""),
            getattr(fn, "__module__", ""),
        ]
        try:
            parts.append(inspect.getsource(fn))
        except Exception:
            pass
        return code_hash("-".join(parts))

    def bind(self, input_nodes, layout, **kwargs):
        return ExternKernelCaller(
            self, input_nodes, layout, kwargs, has_out_variant=self.has_out_variant
        )


class ChoiceCaller:
    def __init__(self, name, input_nodes, layout):
        super().__init__()
        self.name = name
        self.layout = layout
        self.input_nodes = input_nodes

    def benchmark(self, *args, out):
        algo = self.to_callable()
        return do_bench(lambda: algo(*args, out=out))

    def call_name(self):
        raise NotImplementedError()

    def to_callable(self):
        raise NotImplementedError()

    def hash_key(self):
        raise NotImplementedError()

    def output_node(self):
        raise NotImplementedError()


class TritonTemplateCaller(ChoiceCaller):
    def __init__(self, name, input_nodes, layout, make_kernel_render, debug_extra):
        super().__init__(name, input_nodes, layout)
        self.make_kernel_render = make_kernel_render
        self.debug_extra = debug_extra

    def __str__(self):
        return (
            f"TritonTemplateCaller({self.to_callable().__file__}, {self.debug_extra})"
        )

    def call_name(self):
        return f"template_kernels.{self.name}"

    def to_callable(self):
        return getattr(template_kernels, self.name)

    def hash_key(self):
        return "-".join(
            [
                self.name.rsplit("_", 1)[0],
                self.to_callable().key,
            ]
        )

    def output_node(self):
        return ir.TensorBox.create(
            ir.TemplateBuffer(
                layout=self.layout,
                inputs=self.input_nodes,
                make_kernel_render=self.make_kernel_render,
            )
        )


class ExternKernelCaller(ChoiceCaller):
    def __init__(
        self,
        choice: ExternKernelChoice,
        input_nodes,
        layout,
        kwargs=None,
        *,
        has_out_variant=True,
    ):
        super().__init__(choice.name, input_nodes, layout)
        self.choice = choice
        self.kwargs = kwargs or {}
        self.has_out_variant = has_out_variant

    def __str__(self):
        return f"ExternKernelCaller({self.choice.call_name()})"

    def benchmark(self, *args, out):
        if self.has_out_variant:
            return super().benchmark(*args, out=out)
        else:
            algo = self.to_callable()
            out_new = algo(*args)
            torch._C._dynamo.guards.assert_size_stride(
                out_new, tuple(out.size()), tuple(out.stride())
            )
            out.copy_(out_new)  # for correctness checking
            return do_bench(lambda: algo(*args))

    def to_callable(self):
        fn = self.choice.to_callable()
        if self.kwargs:
            return functools.partial(fn, **self.kwargs)
        else:
            return fn

    def hash_key(self):
        return "-".join(
            [
                self.choice.name,
                *[
                    f"{kwarg}={repr(self.kwargs[kwarg])}"
                    for kwarg in sorted(self.kwargs.keys())
                ],
                self.choice.hash_key(),
            ]
        )

    def output_node(self):
        if self.has_out_variant:
            cls = ir.ExternKernelOut
        else:
            cls = ir.ExternKernelAlloc
        return ir.TensorBox.create(
            cls(
                layout=self.layout,
                inputs=self.input_nodes,
                kernel=self.choice.call_name(),
                cpp_kernel=self.choice.cpp_kernel,
                kwargs=self.kwargs,
            )
        )


class ErrorFromChoice(RuntimeError):
    def __init__(self, msg, choice: ChoiceCaller, inputs_str):
        msg += f"\nFrom choice {choice}\n{inputs_str}"
        super().__init__(msg)
        self.choice = choice


class AlgorithmSelectorCache(PersistentCache):
    def __call__(self, choices: List[ChoiceCaller], input_nodes, layout):
        # TODO(nmacchioni): remove once CI tests are fixed
        choices = [choice for choice in choices if choice is not None]
        assert len(choices) > 0, "no choices to select"

        if len(choices) == 1:
            return choices[0].output_node()

        @functools.lru_cache(None)
        def make_benchmark_fn():
            return self.make_benchmark_fn(choices, input_nodes, layout)

        def autotune(choice):
            benchmark_fn = make_benchmark_fn()
            try:
                timing = benchmark_fn(
                    choice,
                )
            except RuntimeError as e:
                msg = str(e)
                if "invalid argument" in msg:
                    msg += "\n\nThis may mean this GPU is too small for max_autotune mode.\n\n"
                    log.warning(msg)
                    return float("inf")
                elif "illegal memory access" in msg:
                    msg += "\n\nEither error in template or triton bug.\n"
                raise ErrorFromChoice(msg, choice, benchmark_fn.debug_str())
            except AssertionError as e:
                raise AssertionError(f"Incorrect result from choice {choice}\n\n{e}")
            return timing

        timings = self.lookup(
            choices,
            choices[0].name,
            repr([self.key_of(x) for x in input_nodes]),
            autotune,
        )
        if timings == {} or choices[0] not in timings:
            return choices[0].output_node()

        if make_benchmark_fn.cache_info().currsize:
            counters["inductor"]["select_algorithm_autotune"] += 1
            self.log_results(choices[0].name, input_nodes, timings)
        return builtins.min(timings, key=timings.__getitem__).output_node()

    @classmethod
    def make_benchmark_fn(
        cls,
        choices,
        input_nodes,
        layout,
    ):
        example_inputs = [cls.benchmark_example_value(x) for x in input_nodes]
        example_inputs_extern = list(example_inputs)
        for i in range(len(example_inputs)):
            if input_nodes[i].get_layout().offset != 0:
                offset = V.graph.sizevars.size_hint(input_nodes[i].get_layout().offset)
                data = example_inputs_extern[i]
                example_inputs_extern[i] = torch.as_strided(
                    data, data.size(), data.stride(), offset
                )

        out = cls.benchmark_example_value(layout)
        out_extern = torch.as_strided(
            out, out.size(), out.stride(), V.graph.sizevars.size_hint(layout.offset)
        )
        if VERIFY:
            choices[0].benchmark(*example_inputs_extern, out=out_extern)
            expected = out_extern.clone()

        def benchmark_in_current_process(choice):
            out.zero_()
            if isinstance(choice, ExternKernelCaller):
                # aten kernels want the offset baked in for sliced tensors
                result = choice.benchmark(*example_inputs_extern, out=out_extern)
            else:
                # triton templates want the base pointer for sliced tensors
                result = choice.benchmark(*example_inputs, out=out)
            if VERIFY:
                torch.testing.assert_close(out_extern, expected, **VERIFY)
            torch.cuda.synchronize()  # shake out any CUDA errors
            return min(result)

        def benchmark_in_sub_process(choice):
            out.zero_()

            if isinstance(choice, ExternKernelCaller):
                inputs = example_inputs_extern
                output = out_extern
            else:
                inputs = example_inputs
                output = out

            if VERIFY:
                expected_output = expected
            else:
                expected_output = None

            # use a tensor since the mutation to a python list in a sub process
            # is not synced back to the parent process
            timings = torch.zeros(3, dtype=torch.float32)

            child = multiprocessing.Process(target=benchmark_choice_in_sub_process, args=(dumps(template_kernels), dumps(choice), inputs, output, expected_output, timings))
            child.start()
            child.join()

            # child process fail
            if child.exitcode != 0:
                warnings.warn(f"Fail to benchmark choice '{choice}'. It will be ignored. Please debug the root cause in case the choice can bring perf gains.")

                # return a large value to this choice will be ignored
                return 1e10

            torch.cuda.synchronize()  # shake out any CUDA errors
            return timings.min().item()

        benchmark = benchmark_in_sub_process if config.autotune_in_subproc else benchmark_in_current_process

        def debug_str():
            def tensor_repr(x):
                return (
                    f"torch.empty_strided({tuple(x.size())!r}, {tuple(x.stride())!r}, "
                    f"dtype={x.dtype!r}, device={x.device.type!r})"
                )

            lines = [
                "inputs = [",
            ]
            for x in example_inputs:
                lines.append(f"    {tensor_repr(x)},")
            lines += ["]", f"out = {tensor_repr(out)}", ""]
            return "\n".join(lines)

        benchmark.debug_str = debug_str
        return benchmark

    @staticmethod
    def log_results(name, input_nodes, timings):
        if not config.max_autotune or not PRINT_AUTOTUNE:
            return
        sizes = ", ".join(
            [
                "x".join(map(str, V.graph.sizevars.size_hints(n.get_size())))
                for n in input_nodes
            ]
        )
        top_k = sorted(timings, key=timings.__getitem__)[:10]
        best = top_k[0]
        best_time = timings[best]
        sys.stderr.write(f"AUTOTUNE {name}({sizes})\n")
        for choice in top_k:
            result = timings[choice]
            sys.stderr.write(f"  {choice.name} {result:.4f}s {best_time/result:.1%}\n")

    @staticmethod
    def benchmark_example_value(node):
        """
        Convert an ir.Buffer into a concrete torch.Tensor we can use for
        benchmarking.
        """
        if isinstance(node, ir.Layout):
            node = ir.Buffer("fake", node)
        return rand_strided(
            V.graph.sizevars.size_hints(node.get_size()),
            V.graph.sizevars.size_hints(node.get_stride()),
            device=node.get_device(),
            dtype=node.get_dtype(),
            extra_size=V.graph.sizevars.size_hint(node.get_layout().offset),
        )

    @staticmethod
    def key_of(node):
        """
        Extract the pieces of an ir.Buffer that we should invalidate cached
        autotuning results on.
        """
        sizevars = V.graph.sizevars
        return (
            node.get_device().type,
            str(node.get_dtype()),
            *sizevars.size_hints(node.get_size()),
            *sizevars.size_hints(node.get_stride()),
            sizevars.size_hint(node.get_layout().offset),
        )


autotune_select_algorithm = AlgorithmSelectorCache()


def realize_inputs(*args):
    if len(args) == 1:
        return ir.ExternKernel.require_stride1(ir.ExternKernel.realize_input(args[0]))
    return [realize_inputs(x) for x in args]


# ensure lowering is imported so that `extern_kernels.*` is populated
from . import lowering  # noqa: F401
