from __future__ import annotations
from typing import List

import random
from math import ceil
# from functools import partial, cache
from functools import partial
from itertools import zip_longest

import torch
from torch import nn, Tensor
from torch.nn import Module, ModuleList
import torch.nn.functional as F
import torch.distributed as dist

from einops import rearrange, repeat, reduce, pack, unpack

from einx import get_at

from .vector_quantize_pytorch import VectorQuantize

# helper functions

def exists(val):
    return val is not None

def first(it):
    return it[0]

def default(val, d):
    return val if exists(val) else d

def round_up_multiple(num, mult):
    return ceil(num / mult) * mult

# distributed helpers

# @cache
def is_distributed():
    return dist.is_initialized() and dist.get_world_size() > 1

# the mlp for generating the neural implicit codebook
# from Huijben et al. https://arxiv.org/abs/2401.14732

class MLP(Module):
    def __init__(
        self,
        dim,
        dim_hidden = None,
        depth = 4,             # they used 4 layers in the paper
        l2norm_output = False
    ):
        super().__init__()
        dim_hidden = default(dim_hidden, dim)

        self.proj_in = nn.Linear(2 * dim, dim)

        layers = ModuleList([])

        for _ in range(depth):
            layers.append(nn.Sequential(
                nn.Linear(dim, dim_hidden),
                nn.SiLU(),
                nn.Linear(dim_hidden, dim)
            ))

        self.layers = layers
        self.l2norm_output = l2norm_output

    def forward(
        self,
        codes,
        *,
        condition
    ):
        one_headed = codes.ndim == 2

        if one_headed:
            codes = rearrange(codes, 'c d -> 1 c d')

        heads, num_codes, batch, seq_len = codes.shape[0], codes.shape[-2], condition.shape[0], condition.shape[-2]

        codes = repeat(codes, 'h c d -> h b n c d', n = seq_len, b = batch)
        condition = repeat(condition, 'b n d -> h b n c d', c = num_codes, h = heads)

        x = torch.cat((condition, codes), dim = -1)
        x = self.proj_in(x)

        for layer in self.layers:
            x = layer(x) + x

        if self.l2norm_output:
            x = F.normalize(x, dim = -1)

        if not one_headed:
            return x

        return rearrange(x, '1 ... -> ...')

# main class

class ResidualVQ(Module):
    """ Follows Algorithm 1. in https://arxiv.org/pdf/2107.03312.pdf """
    def __init__(
        self,
        *,
        dim,
        num_quantizers,
        codebook_dim = None,
        shared_codebook = False,
        heads = 1,
        quantize_dropout = False,
        quantize_dropout_cutoff_index = 0,
        quantize_dropout_multiple_of = 1,
        accept_image_fmap = False,
        implicit_neural_codebook = False, # QINCo from https://arxiv.org/abs/2401.14732
        mlp_kwargs: dict = dict(),
        **vq_kwargs
    ):
        super().__init__()
        assert heads == 1, 'residual vq is not compatible with multi-headed codes'
        codebook_dim = default(codebook_dim, dim)
        codebook_input_dim = codebook_dim * heads

        requires_projection = codebook_input_dim != dim
        self.project_in = nn.Linear(dim, codebook_input_dim) if requires_projection else nn.Identity()
        self.project_out = nn.Linear(codebook_input_dim, dim) if requires_projection else nn.Identity()
        self.has_projections = requires_projection

        self.num_quantizers = num_quantizers

        self.accept_image_fmap = accept_image_fmap

        self.implicit_neural_codebook = implicit_neural_codebook

        if implicit_neural_codebook:
            vq_kwargs.update(
                learnable_codebook = True,
                ema_update = False
            )

        self.layers = ModuleList([VectorQuantize(dim = codebook_dim, codebook_dim = codebook_dim, accept_image_fmap = accept_image_fmap, **vq_kwargs) for _ in range(num_quantizers)])

        assert all([not vq.has_projections for vq in self.layers])

        self.quantize_dropout = quantize_dropout and num_quantizers > 1

        assert quantize_dropout_cutoff_index >= 0

        self.quantize_dropout_cutoff_index = quantize_dropout_cutoff_index
        self.quantize_dropout_multiple_of = quantize_dropout_multiple_of  # encodec paper proposes structured dropout, believe this was set to 4

        # setting up the MLPs for implicit neural codebooks

        self.mlps = ModuleList([MLP(dim = codebook_dim, l2norm_output = first(self.layers).use_cosine_sim, **mlp_kwargs) for _ in range(num_quantizers - 1)])

        # sharing codebook logic

        if not shared_codebook:
            return

        first_vq, *rest_vq = self.layers
        codebook = first_vq._codebook

        for vq in rest_vq:
            vq._codebook = codebook
    
    @property
    def codebook_size(self):
        return self.layers[0].codebook_size
    
    @property
    def codebook_dim(self):
        return self.layers[0].codebook_dim

    @property
    def codebooks(self):
        codebooks = [layer._codebook.embed for layer in self.layers]
        codebooks = torch.stack(codebooks, dim = 0)
        codebooks = rearrange(codebooks, 'q 1 c d -> q c d')
        return codebooks

    def get_codes_from_indices(self, indices):

        batch, quantize_dim = indices.shape[0], indices.shape[-1]

        # may also receive indices in the shape of 'b h w q' (accept_image_fmap)

        indices, ps = pack([indices], 'b * q')

        # because of quantize dropout, one can pass in indices that are coarse
        # and the network should be able to reconstruct

        if quantize_dim < self.num_quantizers:
            assert self.quantize_dropout > 0., 'quantize dropout must be greater than 0 if you wish to reconstruct from a signal with less fine quantizations'
            indices = F.pad(indices, (0, self.num_quantizers - quantize_dim), value = -1)

        # take care of quantizer dropout

        mask = indices == -1.
        indices = indices.masked_fill(mask, 0) # have it fetch a dummy code to be masked out later

        if not self.implicit_neural_codebook:
            # gather all the codes

            all_codes = get_at('q [c] d, b n q -> q b n d', self.codebooks, indices)

        else:
            # else if using implicit neural codebook, codes will need to be derived layer by layer

            code_transform_mlps = (None, *self.mlps)

            all_codes = []
            quantized_out = 0.

            for codes, indices, maybe_transform_mlp in zip(self.codebooks, indices.unbind(dim = -1), code_transform_mlps):

                if exists(maybe_transform_mlp):
                    codes = maybe_transform_mlp(codes, condition = quantized_out)
                    layer_codes = get_at('b n [c] d, b n -> b n d', codes, indices)
                else:
                    layer_codes = get_at('[c] d, b n -> b n d', codes, indices)

                all_codes.append(layer_codes)
                quantized_out += layer_codes

            all_codes = torch.stack(all_codes)

        # mask out any codes that were dropout-ed

        all_codes = all_codes.masked_fill(rearrange(mask, 'b n q -> q b n 1'), 0.)

        # if (accept_image_fmap = True) then return shape (quantize, batch, height, width, dimension)

        all_codes, = unpack(all_codes, ps, 'q b * d')

        return all_codes

    def get_output_from_indices(self, indices):
        codes = self.get_codes_from_indices(indices)
        codes_summed = reduce(codes, 'q ... -> ...', 'sum')
        return self.project_out(codes_summed)

    # NEW FUNC FOR TasteSpeech
    def get_distributed_codes(self, taste_logits):
        taste_probs = F.softmax(taste_logits, dim=-1) # [b, t, q, k]
        codebooks = self.codebooks.detach() # [q, k, d]
        codes = torch.einsum('btqk,qkd->btqd', taste_probs, codebooks)
        return codes

    # NEW FUNC FOR TasteSpeech
    def get_code_from_indices(self, indices):
        codes = self.get_codes_from_indices(indices)
        codes_summed = reduce(codes, 'q ... -> ...', 'sum')
        return codes_summed

    # NEW FUNC FOR TasteSpeech
    def get_indices_from_code(
        self,
        code,
        mask = None,
        indices: Tensor | List[Tensor] | None = None,
        sample_codebook_temp = None,
        freeze_codebook = False,
        rand_quantize_dropout_fixed_seed = None
    ):
        num_quant, quant_dropout_multiple_of, device = self.num_quantizers, self.quantize_dropout_multiple_of, code.device

        assert not (self.accept_image_fmap and exists(indices))

        quantized_out = 0.
        residual = code

        all_indices = []

        if isinstance(indices, list):
            indices = torch.stack(indices)

        should_quantize_dropout = self.training and self.quantize_dropout

        # sample a layer index at which to dropout further residual quantization
        # also prepare null indices and loss

        if should_quantize_dropout:

            if exists(rand_quantize_dropout_fixed_seed):
                # seed is manually passed in
                rand = random.Random(rand_quantize_dropout_fixed_seed)

            elif is_distributed():
                # in distributed environment, synchronize a random seed value if not given
                t = torch.tensor(random.randrange(10_000), device = device)
                # dropout_seed = dist.all_reduce(t).item() # remove
                dist.all_reduce(t) # added
                dropout_seed = t.item() # added 
                rand = random.Random(dropout_seed)

            else:
                rand = random

            rand_quantize_dropout_index = rand.randrange(self.quantize_dropout_cutoff_index, num_quant)

            if quant_dropout_multiple_of != 1:
                rand_quantize_dropout_index = round_up_multiple(rand_quantize_dropout_index + 1, quant_dropout_multiple_of) - 1

            null_indices_shape = (code.shape[0], *code.shape[-2:]) if self.accept_image_fmap else tuple(code.shape[:2])
            null_indices = torch.full(null_indices_shape, -1., device = device, dtype = torch.long)

        # setup the mlps for implicit neural codebook

        maybe_code_transforms = (None,) * len(self.layers)

        if self.implicit_neural_codebook:
            maybe_code_transforms = (None, *self.mlps)

        # go through the layers

        for quantizer_index, (vq, maybe_mlp) in enumerate(zip(self.layers, maybe_code_transforms)):

            if should_quantize_dropout and quantizer_index > rand_quantize_dropout_index:
                all_indices.append(null_indices)
                continue

            layer_indices = None

            # setup the transform code function to be passed into VectorQuantize forward

            if exists(maybe_mlp):
                maybe_mlp = partial(maybe_mlp, condition = quantized_out)

            # vector quantize forward

            quantized, *rest = vq(
                residual,
                mask = mask,
                indices = layer_indices,
                sample_codebook_temp = sample_codebook_temp,
                freeze_codebook = freeze_codebook,
                codebook_transform_fn = maybe_mlp
            )

            residual = residual - quantized.detach()
            quantized_out = quantized_out + quantized

            embed_indices, _ = rest

            all_indices.append(embed_indices)

        # project out, if needed

        # whether to early return the cross entropy loss

        # stack all losses and indices

        all_indices = torch.stack(all_indices, dim = -1)

        return all_indices

    def forward(
        self,
        x,
        mask = None,
        indices: Tensor | List[Tensor] | None = None,
        return_all_codes = False,
        sample_codebook_temp = None,
        freeze_codebook = False,
        rand_quantize_dropout_fixed_seed = None
    ):
        num_quant, quant_dropout_multiple_of, return_loss, device = self.num_quantizers, self.quantize_dropout_multiple_of, exists(indices), x.device

        x = self.project_in(x)

        assert not (self.accept_image_fmap and exists(indices))

        quantized_out = 0.
        residual = x

        all_losses = []
        all_indices = []

        if isinstance(indices, list):
            indices = torch.stack(indices)

        if return_loss:
            assert not torch.any(indices == -1), 'some of the residual vq indices were dropped out. please use indices derived when the module is in eval mode to derive cross entropy loss'
            ce_losses = []

        should_quantize_dropout = self.training and self.quantize_dropout and not return_loss

        # sample a layer index at which to dropout further residual quantization
        # also prepare null indices and loss

        if should_quantize_dropout:

            if exists(rand_quantize_dropout_fixed_seed):
                # seed is manually passed in
                rand = random.Random(rand_quantize_dropout_fixed_seed)

            elif is_distributed():
                # in distributed environment, synchronize a random seed value if not given
                t = torch.tensor(random.randrange(10_000), device = device)
                # dropout_seed = dist.all_reduce(t).item() # remove
                dist.all_reduce(t) # added
                dropout_seed = t.item() # added 
                rand = random.Random(dropout_seed)

            else:
                rand = random

            rand_quantize_dropout_index = rand.randrange(self.quantize_dropout_cutoff_index, num_quant)

            if quant_dropout_multiple_of != 1:
                rand_quantize_dropout_index = round_up_multiple(rand_quantize_dropout_index + 1, quant_dropout_multiple_of) - 1

            null_indices_shape = (x.shape[0], *x.shape[-2:]) if self.accept_image_fmap else tuple(x.shape[:2])
            null_indices = torch.full(null_indices_shape, -1., device = device, dtype = torch.long)
            null_loss = torch.full((1,), 0., device = device, dtype = x.dtype)

        # setup the mlps for implicit neural codebook

        maybe_code_transforms = (None,) * len(self.layers)

        if self.implicit_neural_codebook:
            maybe_code_transforms = (None, *self.mlps)

        # go through the layers

        for quantizer_index, (vq, maybe_mlp) in enumerate(zip(self.layers, maybe_code_transforms)):

            if should_quantize_dropout and quantizer_index > rand_quantize_dropout_index:
                all_indices.append(null_indices)
                all_losses.append(null_loss)
                continue

            layer_indices = None
            if return_loss:
                layer_indices = indices[..., quantizer_index]

            # setup the transform code function to be passed into VectorQuantize forward

            if exists(maybe_mlp):
                maybe_mlp = partial(maybe_mlp, condition = quantized_out)

            # vector quantize forward

            quantized, *rest = vq(
                residual,
                mask = mask,
                indices = layer_indices,
                sample_codebook_temp = sample_codebook_temp,
                freeze_codebook = freeze_codebook,
                codebook_transform_fn = maybe_mlp
            )

            residual = residual - quantized.detach()
            quantized_out = quantized_out + quantized

            if return_loss:
                ce_loss = rest[0]
                ce_losses.append(ce_loss)
                continue

            embed_indices, loss = rest

            all_indices.append(embed_indices)
            all_losses.append(loss)

        # project out, if needed

        quantized_out = self.project_out(quantized_out)

        # whether to early return the cross entropy loss

        if return_loss:
            return quantized_out, sum(ce_losses)

        # stack all losses and indices

        all_losses, all_indices = map(partial(torch.stack, dim = -1), (all_losses, all_indices))

        ret = (quantized_out, all_indices, all_losses)

        if return_all_codes:
            # whether to return all codes from all codebooks across layers
            all_codes = self.get_codes_from_indices(all_indices)

            # will return all codes in shape (quantizer, batch, sequence length, codebook dimension)
            ret = (*ret, all_codes)

        return ret

# grouped residual vq

class GroupedResidualVQ(Module):
    def __init__(
        self,
        *,
        dim,
        groups = 1,
        accept_image_fmap = False,
        **kwargs
    ):
        super().__init__()
        self.dim = dim
        self.groups = groups
        assert (dim % groups) == 0
        dim_per_group = dim // groups

        self.accept_image_fmap = accept_image_fmap

        self.rvqs = ModuleList([])

        for _ in range(groups):
            self.rvqs.append(ResidualVQ(
                dim = dim_per_group,
                accept_image_fmap = accept_image_fmap,
                **kwargs
            ))

    @property
    def codebooks(self):
        return torch.stack(tuple(rvq.codebooks for rvq in self.rvqs))

    @property
    def split_dim(self):
        return 1 if self.accept_image_fmap else -1

    def get_codes_from_indices(self, indices):
        codes = tuple(rvq.get_codes_from_indices(chunk_indices) for rvq, chunk_indices in zip(self.rvqs, indices))
        return torch.stack(codes)

    def get_output_from_indices(self, indices):
        outputs = tuple(rvq.get_output_from_indices(chunk_indices) for rvq, chunk_indices in zip(self.rvqs, indices))
        return torch.cat(outputs, dim = self.split_dim)

    def forward(
        self,
        x,
        indices = None,
        return_all_codes = False,
        sample_codebook_temp = None,
        freeze_codebook = False,
        mask = None,
    ):
        shape, split_dim = x.shape, self.split_dim
        assert shape[split_dim] == self.dim

        # split the feature dimension into groups

        x = x.chunk(self.groups, dim = split_dim)

        indices = default(indices, tuple())
        return_ce_loss = len(indices) > 0
        assert len(indices) == 0 or len(indices) == self.groups

        forward_kwargs = dict(
            return_all_codes = return_all_codes,
            sample_codebook_temp = sample_codebook_temp,
            mask = mask,
            freeze_codebook = freeze_codebook,
            rand_quantize_dropout_fixed_seed = random.randint(0, int(1e7))
        )

        # invoke residual vq on each group

        out = tuple(rvq(chunk, indices = chunk_indices, **forward_kwargs) for rvq, chunk, chunk_indices in zip_longest(self.rvqs, x, indices))
        out = tuple(zip(*out))

        # if returning cross entropy loss to rvq codebooks

        if return_ce_loss:
            quantized, ce_losses = out
            return torch.cat(quantized, dim = split_dim), sum(ce_losses)

        # otherwise, get all the zipped outputs and combine them

        quantized, all_indices, commit_losses, *maybe_all_codes = out

        quantized = torch.cat(quantized, dim = split_dim)
        all_indices = torch.stack(all_indices)
        commit_losses = torch.stack(commit_losses)

        ret = (quantized, all_indices, commit_losses, *maybe_all_codes)
        return ret
