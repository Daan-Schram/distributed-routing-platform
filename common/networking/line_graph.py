from common.networking.network import EdgeMap, ConnectionMap
import torch
from torch_geometric.data import Data

def build_line_graph(
        edge_map: EdgeMap,
        connection_map: ConnectionMap,
) -> tuple[Data, dict[str, int]]:
    # EdgeMap = (from_node, to_node, length_m, time_s)
    edge_to_idx = {edge_id: i for i, edge_id in enumerate(edge_map)}

    src, dst = [], []
    for from_edge, to_edges in connection_map.items():
        for to_edge in to_edges:
            src.append(edge_to_idx[from_edge])
            dst.append(edge_to_idx[to_edge])

    # [2, E_connections]
    edge_index = torch.tensor([src, dst], dtype=torch.long)

    # [N_edges, 1]
    # Extract [length_m, time_s] as static features
    static_features = torch.tensor([[edge_map[e][2], edge_map[e][3]] for e in edge_to_idx], dtype=torch.float)

    data = Data(
        x=static_features,
        edge_index=edge_index
    )

    return data, edge_to_idx