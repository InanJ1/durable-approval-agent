"""Smallest possible LangGraph graph, used to prove agent-chat-ui can talk to `langgraph dev`."""

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph


def text_of(message) -> str:
    content = message.content
    if isinstance(content, list):  # agent-chat-ui sends content blocks
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def reply(state: MessagesState) -> dict:
    said = text_of(state["messages"][-1])
    return {"messages": [AIMessage(content=f"Hello from a minimal LangGraph graph. You said: {said}")]}


graph = StateGraph(MessagesState).add_node("reply", reply).add_edge(START, "reply").add_edge("reply", END).compile()
