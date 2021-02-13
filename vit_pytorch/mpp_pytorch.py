import math
from functools import reduce

import torch
from torch import nn
import torch.nn.functional as F

from einops import rearrange, repeat

# helpers

def prob_mask_like(t, prob):
    batch, seq_length, _ = t.shape
    return torch.zeros((batch, seq_length)).float().uniform_(0, 1) < prob

def get_mask_subset_with_prob(patched_input, prob):
    batch, seq_len, _, device = *patched_input.shape, patched_input.device
    max_masked = math.ceil(prob * seq_len)

    rand = torch.rand((batch, seq_len), device=device)
    _, sampled_indices = rand.topk(max_masked, dim=-1)

    new_mask = torch.zeros((batch, seq_len), device=device)
    new_mask.scatter_(1, sampled_indices, 1)
    return new_mask.bool()

# mpp loss

class MPPLoss(nn.Module):
    def __init__(self, patch_size, channels, output_channel_bits):
        super(MPPLoss, self).__init__()
        self.patch_size = patch_size
        self.channels = channels
        self.output_channel_bits = output_channel_bits

    def forward(self, predicted_patches, target, mask):
        # reshape target to patches
        p = self.patch_size
        target = rearrange(target, "b c (h p1) (w p2) -> b (h w) c (p1 p2) ", p1 = p, p2 = p)

        avg_target = target.mean(dim=3)

        channel_bins = torch.tensor([0.333, 0.666, 1.0])
        descritized_target = torch.bucketize(avg_target, channel_bins, right=True)
        descritized_target = F.one_hot(descritized_target, self.output_channel_bits)
        c, bi = self.channels, self.output_channel_bits
        descritized_target = rearrange(descritized_target, "b n c bi -> b n (c bi)", c = c, bi = bi)

        predicted_patches = predicted_patches[mask]
        descritized_target = descritized_target[mask]

        loss = F.mse_loss(predicted_patches, descritized_target)
        return loss

# main class

class MPP(nn.Module):
    def __init__(
        self,
        transformer,
        patch_size,
        dim,
        output_channel_bits = 3,
        channels = 3,
        mask_prob = 0.15,
        replace_prob = 0.5,
        random_patch_prob = 0.5):
        super().__init__()

        self.transformer = transformer
        self.loss = MPPLoss(patch_size, channels, output_channel_bits)

        # output transformation
        self.to_bits = nn.Linear(dim, output_channel_bits * channels)
        
        # vit related dimensions
        self.patch_size = patch_size

        # mpp related probabilities
        self.mask_prob = mask_prob
        self.replace_prob = replace_prob
        self.random_patch_prob = random_patch_prob

        # token ids
        self.mask_token = nn.Parameter(torch.randn(1, 1, dim * channels))

    def forward(self, input, **kwargs):
        # clone original image for loss
        img = input.clone().detach()

        # reshape raw image to patches
        p = self.patch_size
        input = rearrange(input, 'b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = p, p2 = p)

        mask = get_mask_subset_with_prob(input, self.mask_prob)

        # mask input with mask patches with probability of `replace_prob` (keep patches the same with probability 1 - replace_prob)
        masked_input = input.clone().detach()

         # if random token probability > 0 for mpp
        if self.random_patch_prob > 0:
            random_patch_sampling_prob = self.random_patch_prob / (1 - self.replace_prob)
            random_patch_prob = prob_mask_like(input, random_patch_sampling_prob)
            bool_random_patch_prob = mask * random_patch_prob == True
            random_patches = torch.randint(0, input.shape[1], (input.shape[0], input.shape[1]), device=input.device)
            randomized_input = masked_input[torch.arange(masked_input.shape[0]).unsqueeze(-1), random_patches]
            masked_input[bool_random_patch_prob] = randomized_input[bool_random_patch_prob]

        # [mask] input
        replace_prob = prob_mask_like(input, self.replace_prob)
        bool_mask_replace = (mask * replace_prob) == True
        masked_input[bool_mask_replace] = self.mask_token

        # linear embedding of patches
        masked_input = self.transformer.patch_to_embedding(masked_input)

        # add cls token to input sequence
        b, n, _ = masked_input.shape
        cls_tokens = repeat(self.transformer.cls_token, '() n d -> b n d', b = b)
        masked_input = torch.cat((cls_tokens, masked_input), dim=1)

        # add positional embeddings to input
        masked_input += self.transformer.pos_embedding[:, :(n + 1)]
        masked_input = self.transformer.dropout(masked_input)

        # get generator output and get mpp loss
        masked_input = self.transformer.transformer(masked_input, **kwargs)
        cls_logits = self.to_bits(masked_input)
        logits = cls_logits[:,1:,:]

        mpp_loss = self.loss(logits, img, mask)

        return mpp_loss

        