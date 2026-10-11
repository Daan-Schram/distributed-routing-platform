import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GATConv


class TrafficGNN(nn.Module):
    """Q-value per edge over the line-graph.

    One GAT layer.
    The best value is Q*(e) ~= -(cost(e) + dist(e,dest))
    and both terms are node-local input features
    """

    def __init__(self, in_channels: int, hidden=64, heads=4, dropout=0.1):
        super().__init__()
        self.conv1 = GATConv(in_channels, hidden, heads=heads, concat=True, dropout=dropout)
        self.q_head = nn.Linear(hidden * heads, 1)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        return self.q_head(F.elu(self.conv1(x, edge_index)))
