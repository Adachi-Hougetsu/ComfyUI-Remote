"""新节点/链接 id 分配器（基于模板里的最大值，单调递增）"""


class IdAllocator:
    def __init__(self, max_node_id: int = 0, max_link_id: int = 0):
        self._node = max_node_id + 1
        self._link = max_link_id + 1

    def next_node(self) -> int:
        n = self._node
        self._node += 1
        return n

    def next_link(self) -> int:
        n = self._link
        self._link += 1
        return n

    @property
    def last_node_id(self) -> int:
        return self._node - 1

    @property
    def last_link_id(self) -> int:
        return self._link - 1
