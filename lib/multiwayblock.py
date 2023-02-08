import torch
import torch.nn as nn
import numpy as np

class DropPath(nn.Module):
    """
    Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    Drop Path and Drop Connect is same.
    """
    def __init__(self, drop_prob: float = 0., scale_by_keep: bool = True):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob
        self.scale_by_keep = scale_by_keep

    def drop_path(x, drop_prob: float = 0., training: bool = False, scale_by_keep: bool = True):
        if drop_prob == 0. or not training:
            return x
        keep_prob = 1 - drop_prob
        shape = (x.shape[0],) + (1,) * (x.ndim - 1)
        random_tensor = x.new_empty(shape).bernoulli_(keep_prob) # binary tensor
        if keep_prob > 0.0 and scale_by_keep:
            random_tensor.div_(keep_prob)
        return x * random_tensor

    def forward(self, x):
        return self.drop_path(x, self.drop_prob, self.training, self.scale_by_keep)

# class PatchEmbed(nn.Module):


class MultiHeadSelfAttention(nn.Module):
    def __init__(self, dim:int, num_heads:int, qkv_bias:bool=False,
                 attn_drop:float=0., proj_drop:float=0.) -> None:
        super(MultiHeadSelfAttention, self).__init__()
        assert dim % num_heads == 0, "dim should be divisible by num_heads"
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim*3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
    
    def forward(self, x : torch.Tensor) -> torch.Tensor:
        B, N, D = x.shape
        # assert D % self.num_heads == 0, "D of x should be divisible by num_heads"
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, D//self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0) # each shape = (B, self.num_heads, N, D//self.num_heads)

        attn = (q @ k.transpose(-2,-1)) * self.scale
        attn_probs = attn.softmax(dim=-1)
        attn = self.attn_drop(attn_probs)

        x = (attn @ v).transpose(1,2).reshape(B, N, D)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x, attn_probs



class Mlp(nn.Module):
    def __init__(self, in_features:int, hidden_features:int, out_features:int,
                 act_layer=nn.GELU, bias:bool=True, drop_rate:float=0.) -> None:
        super(Mlp, self).__init__()
        self.fc1 = nn.Linear(in_features, hidden_features, bias=bias)
        self.act = act_layer()
        self.drop1 = nn.Dropout(drop_rate)
        self.fc2 = nn.Linear(hidden_features, out_features, bias=bias)
        self.drop2 = nn.Dropout(drop_rate)

    def forward(self, x : torch.Tensor) -> torch.Tensor:
        x = self.drop1(self.act(self.fc1(x)))
        x = self.drop2(self.fc2(x))
        return x



class Block(nn.Module):
    r"""
    """
    def __init__(self, dim:int, num_heads:int, mlp_ratio:int=4, qkv_bias:bool=False,
                 attn_drop:float=0., proj_drop:float=0., act_layer=nn.GELU, pos_embed:bool=False,
                 norm_layer = nn.LayerNorm, drop_path:float=0., drop_rate:float=0.) -> None:
        super(Block, self).__init__()
        self.norm1 = norm_layer(dim)
        self.attn = MultiHeadSelfAttention(dim, num_heads, qkv_bias, attn_drop, proj_drop)
        self.drop_path1 = DropPath(drop_path) if drop_path>0. else nn.Identity()

        self.norm2 = norm_layer(dim)
        self.mlp = Mlp(dim, int(dim*mlp_ratio), dim, act_layer, drop_rate=drop_rate)
        self.drop_path2 = DropPath(drop_path) if drop_path>0. else nn.Identity()
    
    def forward(self, x:torch.Tensor) -> torch.Tensor:
        skip = x
        x, attn_probs = self.attn(self.norm1(x))
        x = skip + x
        x = x + self.mlp(self.norm2(x))
        return x, attn_probs



class MultiWayBlock(nn.Module):
    r"""
    num_path: a factor that determines how many inception paths are created.
        so, num_path 1 means gerneral attention.
    direction: if direction is 0, path is odd and there are both pixshuf & unpixshuf
               if direction is 1, there are only unpixshuf path (channel up, spatial size down)
               if direction is 2, there are only pixshuf path (channel down, spatial size up)
    """
    def __init__(self, config, block,
                act_layer=nn.GELU, norm_layer=nn.LayerNorm) -> None:
        super(MultiWayBlock, self).__init__()
        # assert num_path%2==1, f"num_path must be odd number"
        assert config.direction==0 or config.direction==1 or config.direction==2, "direction must be 0 or 1 or 2."
        if (config.direction==0 and config.num_path%2==0):
            assert False, "if direction is 0, num_path must be odd number."
        
        self.dim = config.hidden_size
        self.num_path = config.num_path
        self.median = (self.num_path//2)+1 if self.num_path%2==1 else self.num_path//2
        self.total_dim = self.dim * self.num_path
        self.pixshuf_factor = config.pixshuf_factor
        self.direction = config.direction
        self.concat = config.concat
        # self.pos_embed = config.pos_embed
        self.make_path(config, block)
        if self.concat:
            self.norm = norm_layer(self.total_dim)
            self.fc = nn.Linear(self.total_dim, self.dim)
    

    def make_path(self, config, block)->None:
        for i in range(1, self.num_path+1):
            # print(self.median)
            if self.direction == 0:
                if i < self.median:
                    new_dim = self.dim * (self.pixshuf_factor**(i*2))
                elif i > self.median:   
                    new_dim = int(self.dim / (self.pixshuf_factor**((i-self.median)*2)))
                else:   new_dim = self.dim
            elif self.direction == 1:
                if i==1:    new_dim = self.dim
                else:   new_dim = self.dim * (self.pixshuf_factor**((i-1)*2))
            else:
                if i==1:    new_dim = self.dim
                else:   new_dim = int(self.dim / (self.pixshuf_factor**((i-1)*2)))
            # print(new_dim)
            config.hidden_size = new_dim
            # print(">>>>>>>> ",config.hidden_size)
            self.add_module(f"path{i}", block(config, False))
            # self.total_dim = self.total_dim + new_dim
        # raise Exception("----------end-----------")
        config.hidden_size = self.dim

    def _forward_each_paths(self, x:torch.Tensor) -> torch.Tensor:
        features = []
        flag = True
        for i in range(1, self.num_path+1):
            if self.direction == 0:
                if i < self.median:
                    shuf1 = nn.PixelUnshuffle(self.pixshuf_factor*i)
                    shuf2 = nn.PixelShuffle(self.pixshuf_factor*i)
                elif i > self.median:
                    shuf1 = nn.PixelShuffle(self.pixshuf_factor*(i-self.median))
                    shuf2 = nn.PixelUnshuffle(self.pixshuf_factor*(i-self.median))
                else:
                    flag = False
            elif self.direction == 1: # only up channel
                if i==1:
                    flag = False
                else:   
                    shuf1 = nn.PixelUnshuffle(self.pixshuf_factor*(i-1))
                    shuf2 = nn.PixelShuffle(self.pixshuf_factor*(i-1))
            else: # only down channel 
                if i==1:
                    flag = False
                else:   
                    shuf1 = nn.PixelShuffle(self.pixshuf_factor*(i-1))
                    shuf2 = nn.PixelUnshuffle(self.pixshuf_factor*(i-1))

            x = shuf1(x) if flag else x
            B, D, H, W = x.shape
            x = x.contiguous().view(B,D,H*W).permute(0,2,1)
            x, attn_probs = getattr(self, f"path{i}")(x)
            x = x.permute(0,2,1).view(B,D,H,W)
            x = shuf2(x) if flag else x

            features.append(x)
            flag = True
        return features, attn_probs

    def forward(self, x:torch.Tensor) -> torch.Tensor:
        B, N, D = x.shape
        H = W = int(np.sqrt(N))
        x = x.permute(0,2,1).view(B,D,H,W)
        features, attn_probs = self._forward_each_paths(x)
        if self.concat:
            x = torch.cat(features,dim=1)
            x = x.view(B,self.total_dim,N).permute(0,2,1)
            x = self.fc(self.norm(x))
        else:
            x = features[0]
            for f in features[1:]:
                x = x + f
            x = x.view(B,self.dim,N).permute(0,2,1)
        
        return x, attn_probs