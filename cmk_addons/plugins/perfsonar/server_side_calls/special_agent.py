"""How CheckMK calls the perfSONAR special agent for each host."""
from collections.abc import Iterator

from pydantic import BaseModel

from cmk.server_side_calls.v1 import HostConfig, SpecialAgentCommand, SpecialAgentConfig


class PerfsonarParams(BaseModel):
    timeout: float | None = None


def _commands(params: PerfsonarParams, host_config: HostConfig) -> Iterator[SpecialAgentCommand]:
    args = [host_config.name]
    if params.timeout:
        args += ["--timeout", str(int(params.timeout))]
    yield SpecialAgentCommand(command_arguments=args)


special_agent_perfsonar = SpecialAgentConfig(
    name="perfsonar",
    parameter_parser=PerfsonarParams.model_validate,
    commands_function=_commands,
)
