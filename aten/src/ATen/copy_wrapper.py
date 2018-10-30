from code_template import CodeTemplate
from function_wrapper import nested_dict

FILE = CodeTemplate("""\
// ${generated_comment}

#include "ATen/Config.h"

#include "TH/TH.h"
${cuda_includes}
#include "ATen/Utils.h"
#include "ATen/Copy.h"
${copy_includes}

namespace at {

${copy_functions}

}
""")

CUDA_INCLUDES = """\
#undef THNN_
#include "THC/THC.h"
"""

# NB: The copy templates static_cast both dst and src, even though
# technically we also perform a checked_cast_tensor in the prologue
# of the copy (meaning that hypothetically, an already casted tensor
# is available.  However, in s_copy, the casted tensor is dst, while
# in _s_copy_from, the casted tensor is src.  So we can reuse the logic
# in both cases, we unconditionally cast both tensors (and rely
# on the surrounding code to establish the necessary invariants.)

COPY_CPU = CodeTemplate("""\
copy_cpu(dst, src);
""")

COPY = CodeTemplate("""\
${THTensor}_copy${cuda}${src_scalar_name}(${state,}\
dst.unsafeGetTensorImpl(), \
src.unsafeGetTensorImpl());
""")

COPY_ASYNC_CPU = CodeTemplate("""\
if (non_blocking) {
    ${THTensor}_copyAsyncCPU(${state,}\
dst.unsafeGetTensorImpl(), \
src.unsafeGetTensorImpl());
    break;
}
""")

COPY_ASYNC_CUDA = CodeTemplate("""\
if (non_blocking) {
    ${THTensor}_copyAsyncCuda(${state,}\
dst.unsafeGetTensorImpl(), \
src.unsafeGetTensorImpl());
    break;
}
""")

CASE = CodeTemplate("""\
case ${case_id}:
    ${copies}
    break;
""")

FUNCTION = CodeTemplate("""\
Tensor & ${Type}::s_copy_(Tensor & dst, const Tensor & src, bool non_blocking) const {
  // code generated by copy_wrapper
  ${checked_cast_dst}
  switch (src.type().ID()) {
    ${copy_body}
    default:
      ${function_fallthrough}
  }
  dst.unsafeGetTensorImpl()->maybe_zero_dim(src.dim() == 0);
  return dst;
}
""")

FUNCTION_FALLTHROUGH_REDISPATCH = "return src.type()._s_copy_from(src, dst, non_blocking);"

FUNCTION_FALLTHROUGH_ERROR = """\
AT_ERROR("copy does not support ", src.type().toString(), " to ", toString(), " copy.");
"""

FUNCTION_FROM = CodeTemplate("""\
Tensor & ${Type}::_s_copy_from(const Tensor & src, Tensor & dst, bool non_blocking) const {
  // code generated by copy_wrapper
  ${checked_cast_src}
  switch (dst.type().ID()) {
    ${copy_body}
    default:
      AT_ERROR("copy does not support ", toString(), " to ", dst.type().toString(), " copy.");
      break;
  }
  dst.unsafeGetTensorImpl()->maybe_zero_dim(src.dim() == 0);
  return dst; // NB! dst
}
""")

# NB: Hypothetically, someone could call s_copy_from directly and get an error
# message which claims something is not supported, when it actually is.  But
# the correct fix in this case was to NOT call copy_from
FUNCTION_FROM_SWAP = CodeTemplate("""\
Tensor & ${Type}::_s_copy_from(const Tensor & src, Tensor & dst, bool non_blocking) const {
  AT_ERROR("copy does not support ", src.type().toString(), " to ", dst.type().toString(), " copy (s_copy_from case).");
}
""")


def create_one_copy(dst_type, all_types):
    copy_body = []

    for src_type in all_types:
        if dst_type['Density'] == 'Sparse' or src_type['Density'] == 'Sparse':
            # skip sparse copies, which are not yet implemented
            continue
        cuda = ''
        state = []
        if src_type['Backend'] == 'CUDA' or dst_type['Backend'] == 'CUDA':
            state.append('globalContext().getTHCState()')
        if src_type['Backend'] == 'CUDA':
            if dst_type['Backend'] == 'CUDA':
                cuda = 'Cuda'
            else:
                # don't attempt to process CPU-CUDA; this is handled in the
                # redispatch
                continue

        body_env = nested_dict({
            'src_scalar_name': src_type['ScalarName'],
            'case_id': src_type['TypeID'],
            'src_tensor': src_type['Tensor'],
            'dst_tensor': dst_type['Tensor'],
            'cuda': cuda,
            'state': state,
        }, dst_type)

        copies = []
        if dst_type['ScalarType'] == src_type['ScalarType']:
            if dst_type['Backend'] == 'CUDA' and src_type['Backend'] == 'CPU':
                copies.append(COPY_ASYNC_CPU.substitute(body_env))
        if dst_type['Backend'] == 'CPU' and src_type['Backend'] == 'CPU':
            copies.append(COPY_CPU.substitute())
        else:
            copies.append(COPY.substitute(body_env))

        copy_body.append(CASE.substitute(body_env, copies=copies))

    if dst_type['Backend'] == 'CPU':
        # CPU fallthrough needs to redispatch to _s_copy_from
        # (Backend == CPU implies Dense)
        assert dst_type['Density'] == 'Dense'
        function_fallthrough = FUNCTION_FALLTHROUGH_REDISPATCH
    else:
        function_fallthrough = FUNCTION_FALLTHROUGH_ERROR

    # Note [checked_cast_tensor is for dense only]
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # checked_cast_tensor is only needed for backends which implement
    # copy and thus do a cast.  Sparse does not support copies, so there
    # is no need to do a checked cast.  (Furthermore, the code as written
    # will not work, as it will try to there is no derived Tensor type
    # for sparse.)
    checked_cast_dst = ''
    if dst_type['Density'] == 'Dense':
        checked_cast_dst = \
            'checked_tensor_unwrap(dst, "dst", 0, false, Backend::{}, ScalarType::{});' \
            .format(dst_type['Backend'],
                    dst_type['ScalarName'])

    env = nested_dict({
        'function_fallthrough': function_fallthrough,
        'checked_cast_dst': checked_cast_dst,
    }, dst_type)
    return FUNCTION.substitute(env, copy_body=copy_body)


def create_one_copy_from(src_type, all_types):
    if src_type['DenseBackend'] == 'CPU':
        return FUNCTION_FROM_SWAP.substitute(src_type)

    copy_body = []

    for dst_type in all_types:
        if dst_type['Density'] == 'Sparse' or src_type['Density'] == 'Sparse':
            # skip sparse copies, which are not yet implemented
            continue
        cuda = ''
        state = []
        if src_type['Backend'] == 'CUDA':
            cuda = 'Cuda'
        if dst_type['Backend'] == 'CUDA' or src_type['Backend'] == 'CUDA':
            state.append('globalContext().getTHCState()')

        body_env = nested_dict({
            'src_scalar_name': src_type['ScalarName'],
            'case_id': dst_type['TypeID'],
            'src_tensor': src_type['Tensor'],
            'dst_tensor': dst_type['Tensor'],
            'cuda': cuda,
            'state': state,
        }, dst_type)

        copies = []
        if dst_type['ScalarType'] == src_type['ScalarType']:
            # NB: Technically, we have already short-circuited the
            # src_type['Backend'] == 'CUDA' case at the beginning of this
            # function
            if dst_type['Backend'] == 'CPU' and src_type['Backend'] == 'CUDA':
                copies.append(COPY_ASYNC_CUDA.substitute(body_env))
        if dst_type['Backend'] == 'CPU' and src_type['Backend'] == 'CPU':
            copies.append(COPY_CPU.substitute())
        else:
            copies.append(COPY.substitute(body_env))

        copy_body.append(CASE.substitute(body_env, copies=copies))

    # See Note [checked_cast_tensor is for dense only]
    checked_cast_src = ''
    if src_type['Density'] != 'Sparse':
        checked_cast_src = \
            'checked_tensor_unwrap(src, "src", 0, false, Backend::{}, ScalarType::{});' \
            .format(src_type['Backend'], src_type['ScalarName'])

    return FUNCTION_FROM.substitute(src_type, copy_body=copy_body, checked_cast_src=checked_cast_src)


def create(all_types, backend):
    top_env = {
        'copy_includes': [],
        'copy_functions': [],
        'cuda_includes': [],
        'generated_comment': '@' + 'generated by aten/src/ATen/copy_wrapper.py'
    }

    if backend == 'CUDA':
        top_env['cuda_includes'].append(CUDA_INCLUDES)

    # Headers to include
    for the_type in all_types:
        # CUDA backend requires all headers (as it also manages CPU-CUDA
        # conversions), but CPU backend should only have CPU headers
        if backend == 'CPU' and the_type['DenseBackend'] != 'CPU':
            continue
        top_env['copy_includes'].append(
            '#include "ATen/{}.h"'.format(the_type['Type']))
    top_env['copy_includes'].append(
        '#include "ATen/core/TensorImpl.h"')

    # Code generation
    for the_type in all_types:
        # Only generate code for the requested backend
        if the_type['DenseBackend'] != backend:
            continue
        top_env['copy_functions'].append(create_one_copy(the_type, all_types))
        top_env['copy_functions'].append(create_one_copy_from(the_type, all_types))

    return FILE.substitute(top_env)
