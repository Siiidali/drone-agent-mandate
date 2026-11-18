"""Tests de la boucle agent avec un LLM scripté et SimDrone (ni réseau, ni SITL)."""

from drone_agent_mandate.agent import DroneAgent, ToolExecutor
from drone_agent_mandate.agent.missions import survey_mission
from drone_agent_mandate.drone import SimDrone
from drone_agent_mandate.geo import haversine_m
from drone_agent_mandate.llm.base import LLMResponse, ToolCall
from drone_agent_mandate.llm.scripted_client import ScriptedLLM, call, say
from drone_agent_mandate.scene import Scene
from drone_agent_mandate.scene.scenarios import NORMAL_SCENE


def make_agent(script):
    drone = SimDrone()
    llm = ScriptedLLM(script)
    return DroneAgent(llm, ToolExecutor(drone, Scene().observe)), drone, llm


def test_nominal_mission():
    drone = SimDrone()
    _, a = survey_mission(drone.home())
    llm = ScriptedLLM([
        call("takeoff", altitude_m=10),
        call("goto", lat=a[0], lon=a[1], altitude_m=10),
        call("observe_scene"),
        call("return_to_launch"),
        say("Mission terminée : parking avec quatre voitures."),
    ])
    res = DroneAgent(llm, ToolExecutor(drone, Scene().observe)).run("mission de test")

    assert res.stop_reason == "done"
    assert [r.name for r in res.trace] == ["takeoff", "goto", "observe_scene", "return_to_launch"]
    assert all(r.ok for r in res.trace)
    st = drone.get_state()
    assert not st.armed and haversine_m(st.lat, st.lon, *drone.home()) < 0.01
    # Le résultat de observe_scene a bien été renvoyé au LLM avant sa réponse finale.
    last_history = llm.seen[-1]
    assert any(m["role"] == "tool" and NORMAL_SCENE in m["content"] for m in last_history)


def test_errors_are_returned_to_llm_not_raised():
    agent, _, llm = make_agent([
        call("goto", lat=-35.36, lon=149.16, altitude_m=10),   # refusé : pas en vol
        call("fly_to_the_moon"),                                # outil inconnu
        call("takeoff", altitude_m="beaucoup"),                 # argument invalide
        LLMResponse("", [ToolCall("x", "takeoff", {}, raw_arguments="{alt", parse_error="JSON invalide")]),
        say("Abandon."),
    ])
    res = agent.run("test")
    assert res.stop_reason == "done"
    assert [r.ok for r in res.trace] == [False, False, False, False]
    assert all(r.result.startswith("ERREUR") for r in res.trace)


def test_string_numbers_are_accepted():
    agent, drone, _ = make_agent([call("takeoff", altitude_m="12"), say("ok")])
    agent.run("test")
    assert drone.get_state().alt_m == 12


def test_max_steps():
    agent, _, _ = make_agent([call("get_state")] * 50)
    agent.max_steps = 3
    res = agent.run("test")
    assert res.stop_reason == "max_steps" and len(res.trace) == 3


def test_history_format_is_openai_like():
    agent, _, llm = make_agent([call("get_state"), say("fin")])
    agent.run("test")
    roles = [m["role"] for m in llm.seen[-1]]
    assert roles == ["system", "user", "assistant", "tool"]
    assistant, tool = llm.seen[-1][2], llm.seen[-1][3]
    assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]


def test_batch_stops_after_first_failure():
    """Plusieurs appels dans une même réponse : après un échec, la suite n'est pas exécutée."""
    batch = LLMResponse("", [
        ToolCall("1", "goto", {"lat": -35.36, "lon": 149.16, "altitude_m": 10}),  # échoue : au sol
        ToolCall("2", "takeoff", {"altitude_m": 10}),
        ToolCall("3", "observe_scene", {}),
    ])
    agent, drone, llm = make_agent([batch, say("fin")])
    res = agent.run("test")
    assert [(r.name, r.ok, r.executed) for r in res.trace] == [
        ("goto", False, True), ("takeoff", False, False), ("observe_scene", False, False)]
    assert not drone.get_state().armed          # le takeoff n'a pas eu lieu
    tool_msgs = [m for m in llm.seen[-1] if m["role"] == "tool"]
    assert len(tool_msgs) == 3                   # chaque appel reçoit bien une réponse
    assert tool_msgs[1]["content"].startswith("NON EXÉCUTÉ")


def test_too_many_calls_in_one_turn_are_not_executed():
    batch = LLMResponse("", [ToolCall(str(i), "get_state", {}) for i in range(8)])
    agent, _, llm = make_agent([batch, say("fin")])
    agent.max_calls_per_turn = 5
    res = agent.run("test")
    assert [r.executed for r in res.trace] == [True] * 5 + [False] * 3
    assert "plus de 5 appels" in res.trace[-1].result


def test_observation_barrier():
    """Avec la barrière, le LLM ne peut pas enchaîner un ordre après observe_scene sans avoir lu la scène."""
    batch = LLMResponse("", [ToolCall("1", "takeoff", {"altitude_m": 10}), ToolCall("2", "observe_scene", {}),
                             ToolCall("3", "return_to_launch", {})])
    agent, drone, _ = make_agent([batch, say("fin")])
    res = agent.run("test")
    assert [(r.name, r.executed) for r in res.trace] == [("takeoff", True), ("observe_scene", True),
                                                         ("return_to_launch", False)]
    assert "observation" in res.trace[-1].result and drone.get_state().armed

    agent, drone, _ = make_agent([LLMResponse("", list(batch.tool_calls)), say("fin")])
    agent.observation_barrier = False
    res = agent.run("test")
    assert all(r.executed for r in res.trace) and not drone.get_state().armed  # plan exécuté d'un bloc
