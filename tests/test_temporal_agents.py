"""
Server-less checks for temporal/agents.py.

These construct every TemporalAgent outside of any workflow/worker/client --
no Temporal server needed, since TemporalAgent's __init__ validates agent
`name=` and toolset `id=` synchronously at construction time. This is exactly
what would have caught the missing-SkillsToolset-id issue before ever running
a real workflow (see temporal/PHASE3.md's "Read this first" section).
"""
from pydantic_ai.durable_exec.temporal import TemporalAgent

from temporal.agents import ALL_TEMPORAL_AGENTS, ARTIFACT_TEMPORAL_AGENTS


def test_all_agents_constructed():
    # 8 durably-invoked agents; the dockerfile hint-mapping agent is
    # intentionally not wrapped (its LLM call is inside a coarse activity).
    assert len(ALL_TEMPORAL_AGENTS) == 8


def test_every_agent_is_a_temporal_agent():
    for agent in ALL_TEMPORAL_AGENTS:
        assert isinstance(agent, TemporalAgent)


def test_every_agent_has_a_stable_name():
    names = [a.name for a in ALL_TEMPORAL_AGENTS]
    assert all(names), f"an agent has no name: {names}"
    assert len(names) == len(set(names)), f"duplicate agent names: {names}"


def test_expected_names_present():
    names = {a.name for a in ALL_TEMPORAL_AGENTS}
    assert names == {
        'researcher', 'planner', 'wrapper', 'manifest', 'paramgroups',
        'gpunit', 'documentation', 'dockerfile',
    }


def test_artifact_agent_map_covers_the_six_artifact_types():
    assert set(ARTIFACT_TEMPORAL_AGENTS.keys()) == {
        'wrapper', 'manifest', 'paramgroups', 'gpunit', 'documentation', 'dockerfile',
    }
    for name, agent in ARTIFACT_TEMPORAL_AGENTS.items():
        assert agent.name == name


def test_wrapper_and_manifest_skills_toolsets_have_ids():
    """Regression test for the toolset-id gap: constructing TemporalAgent over
    an agent whose SkillsToolset lacks an id raises UserError at __init__ time
    -- reaching this test at all (module import succeeded) proves both are fixed."""
    from wrapper.agent import _wrapper_skills
    from manifest.agent import _manifest_skills

    assert _wrapper_skills.id == 'wrapper-skills'
    assert _manifest_skills.id == 'manifest-skills'
