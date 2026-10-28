from agent.core.agent_memory import AgentMemory


def test_step_counter_advances_once_per_orchestration_step() -> None:
    memory = AgentMemory()

    assert memory.begin_step() == 1
    memory.add_action({"action": "click", "selector": "ref:1"}, "clicked")
    memory.add_action({"action": "overlay_detected", "selector": "modal"}, "opened")

    assert memory.iteration == 1
    assert [item["step"] for item in memory.actions] == [1, 1]
