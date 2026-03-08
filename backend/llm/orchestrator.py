import json
from typing import Optional

from backend.llm.base import BaseLLMProvider, ChatMessage, CompletionResponse, LLMError
from backend.llm.registry import LLMRegistry

ROUTE_PROMPT = """\
You are a routing orchestrator. Given a task description, decide which LLM instance should handle it.
Available instances: {instances}

{user_prompt}

Respond ONLY with valid JSON: {{"instance": "<name>", "reason": "<one sentence>"}}"""


class OrchestratorLLM:
    """
    Uses one named LLM instance to decide which other instance handles a task.
    The user-supplied orchestrator_prompt provides routing rules.

    If only one instance is registered, or routing fails, falls back to that instance.
    """

    def __init__(
        self,
        registry: LLMRegistry,
        orchestrator_name: str,
        orchestrator_prompt: str = "",
    ):
        self.registry = registry
        self.orchestrator_name = orchestrator_name
        self.orchestrator_prompt = orchestrator_prompt

    async def route(self, task_description: str) -> str:
        """
        Returns the name of the instance that should handle this task.
        Falls back to orchestrator_name (or any available) on failure.
        """
        instances = self.registry.list_instances()
        if len(instances) <= 1:
            # No routing needed — return the only instance or the orchestrator
            if instances:
                return instances[0]["name"]
            return self.orchestrator_name

        # Ask the orchestrator LLM to decide
        try:
            orchestrator = self.registry.get(self.orchestrator_name)
        except KeyError:
            # Orchestrator not registered — fall back to any available
            fallback = self.registry.get_any()
            if fallback:
                return fallback.instance_name
            raise LLMError("No LLM instances registered", provider="orchestrator")

        instance_list = ", ".join(
            f'{i["name"]} ({i["provider"]}/{i["model"]})' for i in instances
        )
        system_content = ROUTE_PROMPT.format(
            instances=instance_list,
            user_prompt=self.orchestrator_prompt or "Route to the most capable instance for the task.",
        )

        try:
            response = await orchestrator.complete(
                [
                    ChatMessage(role="system", content=system_content),
                    ChatMessage(role="user", content=f"Task: {task_description}"),
                ],
                max_tokens=100,
                temperature=0.0,
            )
            parsed = json.loads(response.content)
            chosen = parsed["instance"]
            # Validate chosen name exists
            self.registry.get(chosen)
            return chosen
        except (LLMError, json.JSONDecodeError, KeyError, ValueError):
            # Routing failed — fall back to orchestrator itself
            return self.orchestrator_name

    async def complete(
        self,
        messages: list[ChatMessage],
        task_description: str = "",
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> CompletionResponse:
        """Route task → get target instance → call complete()."""
        target_name = await self.route(task_description or messages[-1].content)
        return await self.registry.get(target_name).complete(
            messages, max_tokens=max_tokens, temperature=temperature
        )
