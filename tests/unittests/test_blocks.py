import torch
import unittest

from torch.nn import MultiheadAttention
from cflearn.modules.blocks import *


class TestBlocks(unittest.TestCase):
    def test_attention(self):
        num_heads = 8
        input_dim = embed_dim = 256
        batch_size = 32
        q_len = 20
        k_len = 40
        k_dim = 512
        v_dim = 1024

        q = torch.randn(batch_size, q_len, input_dim)
        k = torch.randn(batch_size, k_len, k_dim)
        v = torch.randn(batch_size, k_len, v_dim)
        mask = torch.randn(batch_size, q_len, k_len) >= 0.5

        q_proj_weight = torch.randn(embed_dim, input_dim)
        k_proj_weight = torch.randn(embed_dim, k_dim)
        v_proj_weight = torch.randn(embed_dim, v_dim)
        in_proj_bias = torch.randn(3 * embed_dim)
        out_proj_weight = torch.randn(embed_dim, input_dim)
        out_proj_bias = torch.randn(input_dim)

        torch_attention = MultiheadAttention(
            input_dim,
            num_heads,
            kdim=k_dim,
            vdim=v_dim,
        )
        torch_attention.q_proj_weight.data = q_proj_weight
        torch_attention.k_proj_weight.data = k_proj_weight
        torch_attention.v_proj_weight.data = v_proj_weight
        torch_attention.in_proj_bias.data = in_proj_bias
        torch_attention.out_proj.weight.data = out_proj_weight
        torch_attention.out_proj.bias.data = out_proj_bias

        permute = lambda t: t.permute(1, 0, 2)
        qt, kt, vt = map(permute, [q, k, v])
        torch_attn_mask = mask.repeat(num_heads, 1, 1)
        torch_output = torch_attention(qt, kt, vt, attn_mask=torch_attn_mask)[0]

        attention = Attention(input_dim, num_heads)
        qb, kb, vb = in_proj_bias.split(input_dim)
        attention.q_linear.linear.weight.data = q_proj_weight
        attention.q_linear.linear.bias.data = qb
        attention.k_linear.linear.weight.data = k_proj_weight
        attention.k_linear.linear.bias.data = kb
        attention.v_linear.linear.weight.data = v_proj_weight
        attention.v_linear.linear.bias.data = vb
        attention.out_linear.linear.weight.data = out_proj_weight
        attention.out_linear.linear.bias.data = out_proj_bias

        output = attention(q, k, v, mask=mask).output

        self.assertTrue(torch.allclose(permute(torch_output), output))


if __name__ == "__main__":
    unittest.main()
