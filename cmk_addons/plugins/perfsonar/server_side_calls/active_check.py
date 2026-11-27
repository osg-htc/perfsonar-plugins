"""How CheckMK calls the pscheduler diags active check for each host."""
from collections.abc import Iterator

from pydantic import BaseModel

from cmk.server_side_calls.v1 import ActiveCheckCommand, ActiveCheckConfig, HostConfig


class DiagsParams(BaseModel):
    limit: float | None = None
    quick: bool | None = None


def _commands(params: DiagsParams, host_config: HostConfig) -> Iterator[ActiveCheckCommand]:
    args = [host_config.name]
    if params.limit:
        args += ["--limit", str(int(params.limit))]
    if params.quick:
        args.append("--quick")
    yield ActiveCheckCommand(service_description="perfSONAR services: pscheduler diags", command_arguments=args)


active_check_perfsonar_diags = ActiveCheckConfig(
    name="perfsonar_diags",
    parameter_parser=DiagsParams.model_validate,
    commands_function=_commands,
)
