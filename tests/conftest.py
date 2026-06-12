import sys
import types


def _ensure_langchain_core_stubs() -> None:
    langchain_core = sys.modules.setdefault("langchain_core", types.ModuleType("langchain_core"))
    messages_module = sys.modules.setdefault(
        "langchain_core.messages",
        types.ModuleType("langchain_core.messages"),
    )
    callbacks_module = sys.modules.setdefault(
        "langchain_core.callbacks",
        types.ModuleType("langchain_core.callbacks"),
    )

    class BaseMessage:
        def __init__(self, content: str = ""):
            self.content = content

    class AIMessage(BaseMessage):
        pass

    class HumanMessage(BaseMessage):
        pass

    class SystemMessage(BaseMessage):
        pass

    class BaseCallbackHandler:
        pass

    for name, value in {
        "BaseMessage": BaseMessage,
        "AIMessage": AIMessage,
        "HumanMessage": HumanMessage,
        "SystemMessage": SystemMessage,
    }.items():
        if not hasattr(messages_module, name):
            setattr(messages_module, name, value)
    if not hasattr(callbacks_module, "BaseCallbackHandler"):
        callbacks_module.BaseCallbackHandler = BaseCallbackHandler
    if not hasattr(langchain_core, "messages"):
        langchain_core.messages = messages_module
    if not hasattr(langchain_core, "callbacks"):
        langchain_core.callbacks = callbacks_module


_ensure_langchain_core_stubs()
