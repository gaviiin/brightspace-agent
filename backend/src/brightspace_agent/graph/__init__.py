"""S4 graph assembly: the deterministic step that turns topics, materials,
edges, and assignments into the JSON the frontend renders. No LLM involved --
see graph/build.py."""

from brightspace_agent.graph.build import build_graph

__all__ = ["build_graph"]
