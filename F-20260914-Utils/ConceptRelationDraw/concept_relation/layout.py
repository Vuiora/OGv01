from functools import lru_cache
from heapq import heappop, heappush

from .selection import RELATION_PRIORITY

NODE_WIDTH = 176
NODE_HEIGHT = 56


def graph_layout(graph):
    nodes = tuple(node["id"] for node in graph["concepts"])
    ids = set(nodes)
    edges = tuple((edge["id"], edge["source"], edge["target"], edge["type"], edge["confidence"], edge["review"])
                  for edge in graph["relations"] if edge["review"] != "rejected"
                  and edge["source"] in ids and edge["target"] in ids and edge["source"] != edge["target"])
    return _layout(nodes, edges)


def segment_clear(start, end, boxes):
    for left, top, right, bottom in boxes:
        if start[0] == end[0]:
            if left < start[0] < right and max(start[1], end[1]) > top and min(start[1], end[1]) < bottom:
                return False
        elif top < start[1] < bottom and max(start[0], end[0]) > left and min(start[0], end[0]) < right:
            return False
    return True


def simplify(points):
    result = []
    for point in points:
        if result and point == result[-1]:
            continue
        while len(result) > 1 and ((result[-2][0] == result[-1][0] == point[0]) or
                                  (result[-2][1] == result[-1][1] == point[1])):
            result.pop()
        result.append(point)
    return result


class Router:
    def __init__(self, positions):
        self.boxes = [(point["x"] - 96, point["y"] - 36, point["x"] + 96, point["y"] + 36)
                      for point in positions.values()]
        self.xs = sorted({point["x"] + offset for point in positions.values() for offset in (-112, 112)})
        self.ys = sorted({point["y"] + offset for point in positions.values() for offset in (-50, 0, 50)})
        self.clear = {}
        self.used = {}

    def route(self, source, target):
        source_side = 1 if target["x"] >= source["x"] else -1
        target_side = -source_side if target["x"] != source["x"] else source_side
        start = (self.xs.index(source["x"] + source_side * 112), self.ys.index(source["y"]))
        finish = (self.xs.index(target["x"] + target_side * 112), self.ys.index(target["y"]))
        initial = (*start, 0)
        queue, costs, previous = [(0, 0, initial)], {initial: 0}, {}
        final = None
        while queue:
            _, cost, state = heappop(queue)
            if cost != costs[state]:
                continue
            column, row, direction = state
            if (column, row) == finish:
                final = state
                break
            for next_column, next_row, next_direction in ((column - 1, row, 1), (column + 1, row, 1),
                                                          (column, row - 1, 2), (column, row + 1, 2)):
                if not (0 <= next_column < len(self.xs) and 0 <= next_row < len(self.ys)):
                    continue
                start_point = (self.xs[column], self.ys[row])
                end_point = (self.xs[next_column], self.ys[next_row])
                segment = tuple(sorted((start_point, end_point)))
                if segment not in self.clear:
                    self.clear[segment] = segment_clear(start_point, end_point, self.boxes)
                if not self.clear[segment]:
                    continue
                length = abs(end_point[0] - start_point[0]) + abs(end_point[1] - start_point[1])
                new_cost = cost + length + (24 if direction and direction != next_direction else 0) + self.used.get(segment, 0) * 30
                next_state = (next_column, next_row, next_direction)
                if new_cost >= costs.get(next_state, float("inf")):
                    continue
                costs[next_state], previous[next_state] = new_cost, state
                distance = abs(end_point[0] - self.xs[finish[0]]) + abs(end_point[1] - self.ys[finish[1]])
                heappush(queue, (new_cost + distance, new_cost, next_state))
        if final is None:
            raise ValueError("无法为关系生成避让节点的路径")
        steps = []
        while final is not None:
            steps.append((self.xs[final[0]], self.ys[final[1]]))
            final = previous.get(final)
        steps.reverse()
        for start_point, end_point in zip(steps, steps[1:]):
            segment = tuple(sorted((start_point, end_point)))
            self.used[segment] = self.used.get(segment, 0) + 1
        return simplify([(source["x"] + source_side * 88, source["y"]), *steps,
                         (target["x"] + target_side * 88, target["y"])])


@lru_cache(maxsize=32)
def _layout(nodes, edges):
    parents = {node: node for node in nodes}
    adjacency = {node: [] for node in nodes}
    order = {node: index for index, node in enumerate(nodes)}

    def root(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node

    backbone = []
    ranked = sorted(edges, key=lambda edge: (edge[5] != "accepted", -RELATION_PRIORITY[edge[3]], -edge[4], edge[0]))
    for edge_id, source, target, *_ in ranked:
        source_root, target_root = root(source), root(target)
        if source_root == target_root:
            continue
        parents[source_root] = target_root
        adjacency[source].append(target)
        adjacency[target].append(source)
        backbone.append(edge_id)
    groups = {}
    for node in nodes:
        groups.setdefault(root(node), []).append(node)
    positions = {}
    shelf_x, shelf_y, shelf_height = 140, 160, 0
    components = sorted(groups.values(), key=lambda group: (-len(group), min(order[node] for node in group)))
    for component in components:
        def eccentricity(node):
            visited, pending, maximum = {node}, [(node, 0)], 0
            while pending:
                current, distance = pending.pop()
                maximum = max(maximum, distance)
                for neighbor in adjacency[current]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        pending.append((neighbor, distance + 1))
            return maximum

        origin = min(component, key=lambda node: (eccentricity(node), -len(adjacency[node]), order[node]))
        local = {}
        next_leaf = 0

        def place(node, parent, depth):
            nonlocal next_leaf
            children = sorted((neighbor for neighbor in adjacency[node] if neighbor != parent), key=order.get)
            for child in children:
                place(child, node, depth + 1)
            if children:
                vertical = (local[children[0]]["y"] + local[children[-1]]["y"]) / 2
            else:
                vertical = next_leaf * 116
                next_leaf += 1
            local[node] = {"x": depth * 292, "y": vertical}

        place(origin, None, 0)
        width = max(point["x"] for point in local.values()) + 232
        height = max(point["y"] for point in local.values()) + 116
        if shelf_x > 140 and shelf_x + width > 1500:
            shelf_x, shelf_y, shelf_height = 140, shelf_y + shelf_height + 60, 0
        positions.update({node: {"x": point["x"] + shelf_x, "y": point["y"] + shelf_y} for node, point in local.items()})
        shelf_x += width + 60
        shelf_height = max(shelf_height, height)
    routes = {}
    backbone_set = set(backbone)
    for edge_id, source_id, target_id, *_ in edges:
        if edge_id not in backbone_set:
            continue
        source, target = positions[source_id], positions[target_id]
        side = 1 if target["x"] > source["x"] else -1
        middle = (source["x"] + target["x"]) / 2
        routes[edge_id] = simplify([(source["x"] + 88 * side, source["y"]), (middle, source["y"]),
                                   (middle, target["y"]), (target["x"] - 88 * side, target["y"])])
    router = Router(positions)
    for edge_id, source, target, *_ in ranked:
        if edge_id not in backbone_set:
            routes[edge_id] = router.route(positions[source], positions[target])
    return dict(version=1, node_width=NODE_WIDTH, node_height=NODE_HEIGHT, positions=positions,
                routes=routes, backbone_ids=backbone,
                width=max((point["x"] for point in positions.values()), default=400) + 160,
                height=max((point["y"] for point in positions.values()), default=200) + 120)
