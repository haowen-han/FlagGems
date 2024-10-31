import logging
from typing import Optional, Tuple

import torch
import triton
import triton.language as tl


def configs():
    block = [1024, 2048]
    warps = [4, 8]
    return [
        triton.Config({"BLOCK_SIZE": bs}, num_warps=wp) for bs in block for wp in warps
    ]


@triton.autotune(configs=configs(), key=["N", "C", "OH", "OW"])
@triton.heuristics(
    {
        "SAME_H": lambda args: args["OH"] == args["IH"],
        "SAME_W": lambda args: args["OW"] == args["IW"],
    }
)
@triton.jit
def upsample_nearest2d_kernel(
    ptr_o,
    ptr_i,
    N,
    C,
    OH,
    OW,
    IH,
    IW,
    reciprocal_scale_h,
    reciprocal_scale_w,
    BLOCK_SIZE: tl.constexpr,
    SAME_H: tl.constexpr,
    SAME_W: tl.constexpr,
):
    pid = tl.program_id(axis=0)
    idx = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    ow = idx % OW
    oh = idx // OW % OH
    c = idx // OW // OH % C
    n = idx // OW // OH // C % N
    if SAME_H:
        ih = oh
    else:
        # tl.floor() cannot be found in 2.3.1, using int trunc
        ih = tl.minimum((oh * reciprocal_scale_h).to(tl.int32), IH - 1)
    if SAME_W:
        iw = ow
    else:
        iw = tl.minimum((ow * reciprocal_scale_w).to(tl.int32), IW - 1)
    offset_o = ((n * C + c) * OH + oh) * OW + ow
    offset_i = ((n * C + c) * IH + ih) * IW + iw
    data = tl.load(ptr_i + offset_i)
    tl.store(ptr_o + offset_o, data)


def upsample_nearest2d(
    input: torch.Tensor,
    output_size: Tuple[int],
    scales_h: Optional[float] = None,
    scales_w: Optional[float] = None,
) -> torch.Tensor:
    logging.debug("GEMS UPSAMPLE NEAREST2D")
    assert input.is_cuda
    assert input.ndim == 4, "The ndim of input must be 4"
    assert len(output_size) == 2, "The len of output_size must be 2"
    OH, OW = output_size
    N, C, IH, IW = input.shape
    if scales_h is not None:
        reciprocal_scale_h = 1 / scales_h
    else:
        reciprocal_scale_h = IH / OH
    if scales_w is not None:
        reciprocal_scale_w = 1 / scales_w
    else:
        reciprocal_scale_w = IW / OW
    # allocate output
    output = torch.empty((N, C, OH, OW), device=input.device, dtype=input.dtype)
    total_threads = N * C * OH * OW
    grid = lambda META: (triton.cdiv(total_threads, META["BLOCK_SIZE"]),)
    upsample_nearest2d_kernel[grid](
        output, input, N, C, OH, OW, IH, IW, reciprocal_scale_h, reciprocal_scale_w
    )
    return output