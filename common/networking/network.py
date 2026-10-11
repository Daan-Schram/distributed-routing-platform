import xml.etree.ElementTree as ET
import networkx as nx

EdgeMap = dict[str, tuple[str, str, float, float]]  # (from_node, to_node, length_m, time_s)
ConnectionMap = dict[str, list[str]]  # from_edge_id -> [to_edge_id, ...]


def load_network(net_xml_path: str) -> tuple[nx.DiGraph, nx.DiGraph, EdgeMap, ConnectionMap]:
    """Parse a SUMO .net.xml into a junction-graph, a line-graph, and edge lookup"""
    root = ET.parse(net_xml_path).getroot()

    graph: nx.DiGraph = nx.DiGraph()
    line_graph: nx.DiGraph = nx.DiGraph()
    edge_map: EdgeMap = {}
    connection_map: ConnectionMap = {}

    for edge in root.findall("edge"):
        if edge.get("function") == "internal":
            continue

        edge_id = edge.get("id")
        from_node = edge.get("from")
        to_node = edge.get("to")

        if not edge_id or not from_node or not to_node:
            continue

        lane = edge.find("lane")
        length = float(lane.get("length", 0.0)) if lane is not None else 0.0
        speed  = float(lane.get("speed",  13.89)) if lane is not None else 13.89
        time   = length / speed if speed > 0 else 0.0

        graph.add_node(from_node)
        graph.add_node(to_node)
        edge_map[edge_id] = (from_node, to_node, length, time)
        
        # Add to line graph as a node
        line_graph.add_node(edge_id, length=length, time=time)

    for conn in root.findall("connection"):
        from_edge = conn.get("from")
        to_edge = conn.get("to")

        if not from_edge or not to_edge:
            continue
        if from_edge not in edge_map or to_edge not in edge_map:
            continue

        connection_map.setdefault(from_edge, []).append(to_edge)

        # Junction graph
        _, junction, _, _ = edge_map[from_edge]
        _, to_node, length, time = edge_map[to_edge]
        graph.add_edge(junction, to_node, id=to_edge, length=length, time=time)
        
        # Line graph: connection between edges
        line_graph.add_edge(from_edge, to_edge, length=length, time=time)

    print(f"Loaded {len(edge_map)} edges and {line_graph.size()} connections from {net_xml_path}")
    return graph, line_graph, edge_map, connection_map


def load_edge_geometry(net_xml_path: str) -> dict:
    """
    Parse a SUMO .net.xml into drawable edge polylines for the route/loop map

    Returns {"edges": [{"id", "points": [[x, y], ...]}, ...],
             "bounds": {"minX", "minY", "maxX", "maxY"}}.
    """
    root = ET.parse(net_xml_path).getroot()
    edges: list[dict] = []
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for edge in root.findall("edge"):
        if edge.get("function") == "internal":
            continue
        edge_id = edge.get("id")
        if not edge_id or edge_id.startswith(":"):
            continue

        lane = edge.find("lane")
        shape = lane.get("shape") if lane is not None else edge.get("shape")
        if not shape:
            continue

        points: list[list[float]] = []
        for pair in shape.split():
            xy = pair.split(",")
            if len(xy) < 2:
                continue
            x, y = float(xy[0]), float(xy[1])
            points.append([x, y])
            min_x, min_y = min(min_x, x), min(min_y, y)
            max_x, max_y = max(max_x, x), max(max_y, y)
        if len(points) >= 2:
            edges.append({"id": edge_id, "points": points})

    if not edges:
        bounds = {"minX": 0.0, "minY": 0.0, "maxX": 0.0, "maxY": 0.0}
    else:
        bounds = {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y}
    return {"edges": edges, "bounds": bounds}
